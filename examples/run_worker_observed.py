#!/usr/bin/env python3
"""Observed Worker launcher (Live Execution Observer, Phase 1).

Wraps one Worker command in an optional read-only shell.online observer
without changing Bridge/ForgeLoop authority:

- Disabled by default (`FORGEBRIDGE_LIVE_OBSERVER=none`).
- Opt-in via `--provider shell-online` or the environment.
- Read-only only; E2EE required; no `--interactive` option exists.
- The E2EE password is a local-only operator secret: it is written to an
  owner-only file under `/tmp/forgeloopbridge-observer/` and never posted
  to the Bridge, logged, or persisted.
- The Bridge receives at most one Markdown observer-start announcement per
  invocation over the existing message POST path (no schema/API change).
- The wrapper uses `--foreground` so the orchestrator still observes the
  real Worker exit. Without advertised `--foreground` support the helper
  runs the Worker directly (fail-open, observer unavailable) rather than
  detaching the Worker behind a background wrapper.
- Observer lifetime follows the Worker turn; cleanup targets only the
  session created by this helper (`shell kill <session-id>`, never `--all`).

Conceptual usage:

    python examples/run_worker_observed.py \\
        --provider shell-online \\
        --bridge-url http://localhost:8000 \\
        --worker-token-env WORKER_TOKEN \\
        --task-id taskvault-mvp \\
        -- codex exec --ephemeral ...

The Bridge records coordination, ForgeLoop owns engineering truth, and
shell.online may let an Engineer watch execution without becoming part of
either authority boundary. Terminal output is not canonical evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import select
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from examples.live_observer.base import (  # noqa: E402
    PROVIDER_NONE,
    PROVIDER_SHELL_ONLINE,
    build_observer_announcement,
    format_end_log,
    format_error_log,
    format_start_log,
    get_provider_name,
    get_shell_command,
    is_observer_enabled,
    remove_secret_file,
    write_secret_file,
)
from examples.live_observer.shell_online import (  # noqa: E402
    ObserverError,
    build_wrapper_argv,
    parse_start_output,
    preflight,
    stop_session,
)

BRIDGE_POST_TIMEOUT_SECONDS = 15
METADATA_WAIT_SECONDS = 15


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    default_provider = get_provider_name()
    if default_provider not in (PROVIDER_NONE, PROVIDER_SHELL_ONLINE):
        default_provider = PROVIDER_NONE
    parser = argparse.ArgumentParser(
        description="Run one Worker command with an optional read-only live observer."
    )
    parser.add_argument(
        "--provider",
        choices=[PROVIDER_NONE, PROVIDER_SHELL_ONLINE],
        default=default_provider,
        help="observer provider (default from FORGEBRIDGE_LIVE_OBSERVER, default: none)",
    )
    parser.add_argument(
        "--shell-command",
        default=None,
        help="provider executable override (default from FORGEBRIDGE_LIVE_OBSERVER_COMMAND)",
    )
    parser.add_argument(
        "--bridge-url",
        default=os.getenv("FORGEBRIDGE_BRIDGE_URL", "http://localhost:8000").rstrip("/") or "http://localhost:8000",
        help="ForgeLoopBridge base URL for the one observer announcement",
    )
    parser.add_argument(
        "--worker-token-env",
        default="WORKER_TOKEN",
        help="environment variable holding the Worker Bearer token",
    )
    parser.add_argument(
        "--task-id",
        default=None,
        help="optional task_id attached to the observer announcement",
    )
    parser.add_argument(
        "--metadata-timeout",
        type=float,
        default=METADATA_WAIT_SECONDS,
        help="seconds to wait for provider session metadata",
    )
    parser.add_argument("worker_command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    # `argparse.REMAINDER` keeps a leading `--` separator when callers use
    # the conventional `-- <worker...>` spelling; drop exactly one.
    if args.worker_command and args.worker_command[0] == "--":
        args.worker_command = args.worker_command[1:]
    return args


def _resolve_worker_command(args: argparse.Namespace) -> list[str]:
    command = [str(part) for part in (args.worker_command or []) if str(part) != ""]
    if not command:
        raise SystemExit("run_worker_observed: missing Worker command after `--`")
    return command


def _read_worker_token(env_name: str) -> str | None:
    if not env_name:
        return None
    token = os.getenv(env_name, "")
    return token.strip() or None


def post_observer_announcement(
    bridge_url: str,
    worker_token: str | None,
    content: str,
    task_id: str | None,
) -> bool:
    """Post one Markdown announcement over the existing message path.

    Best-effort: Bridge delivery failures are diagnostics only and never
    fail the Worker turn. Returns True on 2xx.
    """
    if not bridge_url or not worker_token:
        return False
    url = bridge_url.rstrip("/") + "/api/messages"
    body: dict = {"content": content}
    if task_id and str(task_id).strip():
        body["task_id"] = str(task_id).strip()
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {worker_token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=BRIDGE_POST_TIMEOUT_SECONDS) as response:
            return 200 <= int(getattr(response, "status", 200)) < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def run_direct(worker_command: list[str]) -> int:
    """Run the Worker command directly (no observer). Preserves exit result."""
    try:
        proc = subprocess.run(worker_command, check=False, shell=False)
    except FileNotFoundError:
        print("run_worker_observed: worker executable not found", file=sys.stderr)
        return 127
    except OSError as exc:
        print(f"run_worker_observed: worker failed to start ({exc.__class__.__name__})", file=sys.stderr)
        return 1
    return _normalize_exit_code(proc.returncode)


def _normalize_exit_code(returncode: int | None) -> int:
    if returncode is None:
        return 1
    if returncode < 0:
        # Killed by signal N -> conventional 128+N for the orchestrator.
        return 128 + (-returncode)
    return int(returncode)


def _read_first_stderr_chunk(pipe, timeout: float) -> str:
    """Wait up to `timeout` seconds for the first stderr bytes (POSIX select)."""
    try:
        ready, _, _ = select.select([pipe], [], [], max(0.0, float(timeout)))
    except (OSError, ValueError, TypeError):
        return ""
    if not ready:
        return ""
    try:
        # The provider prints one JSON line first; a single line is enough
        # for metadata while the forwarder thread keeps the pipe drained.
        line = pipe.readline()
    except (OSError, ValueError):
        return ""
    rest = ""
    try:
        import fcntl

        flags = fcntl.fcntl(pipe.fileno(), fcntl.F_GETFL)
        fcntl.fcntl(pipe.fileno(), fcntl.F_SETFL, flags | _os_nonblock())
        rest = pipe.read() or ""
    except Exception:
        rest = ""
    return (line or "") + (rest or "")


def _os_nonblock() -> int:
    import os as _os

    return getattr(_os, "O_NONBLOCK", 0) or 0


def _forward_stderr(pipe, target) -> None:
    try:
        for chunk in iter(lambda: pipe.read(65536), ""):
            if not chunk:
                break
            try:
                target.write(chunk)
                target.flush()
            except (OSError, ValueError):
                break
    except (OSError, ValueError):
        pass


def run_observed(
    worker_command: list[str],
    *,
    shell_cmd: str | None,
    bridge_url: str,
    worker_token: str | None,
    task_id: str | None,
    metadata_timeout: float,
) -> int:
    """Run the Worker wrapped in a read-only observer; return Worker exit."""
    try:
        probe = preflight(shell_cmd)
    except ObserverError as exc:
        print(format_error_log(exc.code))
        print("LIVE_OBSERVER_STATUS provider=shell.online relay=unavailable")
        print("observer unavailable; running Worker directly")
        return run_direct(worker_command)
    if not (probe.get("supports_read_only") and probe.get("supports_json")):
        print(format_error_log("OBSERVER_START_FAILED"))
        print("observer unavailable; running Worker directly")
        return run_direct(worker_command)

    try:
        wrapper_argv = build_wrapper_argv(
            worker_command, shell_cmd or get_shell_command(), foreground=True
        )
    except ObserverError as exc:
        print(format_error_log(exc.code))
        print("observer unavailable; running Worker directly")
        return run_direct(worker_command)

    try:
        proc = subprocess.Popen(
            wrapper_argv,
            stdout=None,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            shell=False,
        )
    except FileNotFoundError:
        print(format_error_log("OBSERVER_EXECUTABLE_NOT_FOUND"))
        print("observer unavailable; running Worker directly")
        return run_direct(worker_command)
    except OSError as exc:
        print(format_error_log("OBSERVER_START_FAILED"))
        print(f"observer start failed ({exc.__class__.__name__}); running Worker directly")
        return run_direct(worker_command)

    assert proc.stderr is not None
    stderr_head = _read_first_stderr_chunk(proc.stderr, metadata_timeout)
    forwarder = threading.Thread(
        target=_forward_stderr, args=(proc.stderr, sys.stderr), daemon=True
    )
    forwarder.start()

    session = None
    secret_path: Path | None = None
    try:
        parsed = parse_start_output("", stderr_head)
    except ObserverError as exc:
        # Fail closed on security properties (interactive/unencrypted/bad URL)
        # but fail open for availability (malformed/missing metadata): the
        # wrapped Worker is already running, so keep it and skip publishing.
        print(format_error_log(exc.code))
        if exc.code in ("OBSERVER_UNSAFE_ACCESS_MODE", "OBSERVER_ENCRYPTION_DISABLED", "OBSERVER_URL_INVALID"):
            print("unsafe observer session rejected; Worker continues without a published link")
            try:
                # Best-effort targeted cleanup of the rejected session is not
                # possible without a session id; the task-bound session ends
                # with the wrapped command.
                pass
            finally:
                pass
        else:
            print("observer unavailable; Worker continues without a published link")
        returncode = proc.wait()
        return _normalize_exit_code(returncode)

    session = parsed.session
    print(format_start_log(session), flush=True)
    if parsed.e2ee_password:
        try:
            secret_path = write_secret_file(
                session.session_id, session.share_url, parsed.e2ee_password
            )
        except OSError as exc:
            print(format_error_log("OBSERVER_START_FAILED"))
            print(f"local secret file failed ({exc.__class__.__name__}); link not published")
            secret_path = None
            session = None
            returncode = proc.wait()
            return _normalize_exit_code(returncode)
        print(
            "observer password stored locally only; "
            "share the share URL without the password on the Bridge",
        )

    if session is not None:
        try:
            announcement = build_observer_announcement(session, task_id=task_id)
        except ObserverError as exc:
            print(format_error_log(exc.code))
            announcement = None
        if announcement is not None:
            posted = post_observer_announcement(bridge_url, worker_token, announcement, task_id)
            if not posted:
                print("observer announcement not delivered; Worker continues")

    try:
        returncode = proc.wait()
    except KeyboardInterrupt:
        try:
            proc.terminate()
        except OSError:
            pass
        returncode = proc.wait()
    finally:
        if session is not None:
            try:
                stop_session(session.session_id, shell_cmd or get_shell_command())
            except ObserverError as exc:
                print(format_error_log(exc.code))
            print(format_end_log(session.session_id))
        remove_secret_file(secret_path)
    return _normalize_exit_code(returncode)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    worker_command = _resolve_worker_command(args)
    provider = (args.provider or PROVIDER_NONE).strip().lower()
    shell_cmd = (args.shell_command or get_shell_command()).strip() or get_shell_command()
    bridge_url = (args.bridge_url or "http://localhost:8000").rstrip("/") or "http://localhost:8000"
    worker_token = _read_worker_token(args.worker_token_env)
    task_id = (args.task_id or "").strip() or None

    if provider == PROVIDER_NONE or not is_observer_enabled_for(provider):
        return run_direct(worker_command)
    return run_observed(
        worker_command,
        shell_cmd=shell_cmd,
        bridge_url=bridge_url,
        worker_token=worker_token,
        task_id=task_id,
        metadata_timeout=max(1.0, float(args.metadata_timeout or METADATA_WAIT_SECONDS)),
    )


def is_observer_enabled_for(provider: str) -> bool:
    if (provider or "").strip().lower() == PROVIDER_SHELL_ONLINE:
        return True
    return is_observer_enabled()


if __name__ == "__main__":
    raise SystemExit(main())
