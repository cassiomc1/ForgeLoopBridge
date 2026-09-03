#!/usr/bin/env python3
"""Observed Worker launcher (Live Execution Observer, Phase 1).

Wraps one Worker command in an optional read-only shell.online observer
without changing Bridge/ForgeLoop authority:

- Disabled by default (`FORGEBRIDGE_LIVE_OBSERVER=none`).
- Opt-in via `--provider shell-online` or the environment.
- Read-only only; E2EE required; no interactive option exists.
- ForgeLoopBridge never stores the E2EE password: provider metadata is
  parsed, the share URL is kept, and the password is discarded immediately.
  shell.online retains its own owner-side session record; operators retrieve
  the password locally with `shell list`.
- The Bridge receives at most one Markdown observer-start announcement per
  invocation over the existing message POST path (no schema/API change).
- The wrapper uses `--foreground` so the orchestrator still observes the
  real Worker exit. Without advertised `--foreground` support the helper
  runs the Worker directly (fail-open, observer unavailable) rather than
  detaching the Worker behind a background wrapper.
- Pre-start provider failure fails open (Worker runs directly, exactly
  once). Post-start security violation fails closed: targeted
  `shell kill <session-id>`, bounded termination, non-zero exit, and the
  Worker command is never executed a second time.
- Observer lifetime follows the Worker turn; cleanup targets only the
  session created by this helper (never `--all`).

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
import queue
import signal
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
    OBSERVER_SECURITY_VALIDATION_FAILED,
    PROVIDER_NONE,
    PROVIDER_SHELL_ONLINE,
    build_observer_announcement,
    extract_session_id,
    format_end_log,
    format_error_log,
    format_start_log,
    get_provider_name,
    get_shell_command,
    is_observer_enabled,
    project_safe_metadata,
)
from examples.live_observer.shell_online import (  # noqa: E402
    ObserverError,
    build_wrapper_argv,
    is_provider_metadata_dict,
    preflight,
    stop_session,
    try_parse_json_line,
)

BRIDGE_POST_TIMEOUT_SECONDS = 15
METADATA_WAIT_SECONDS = 15
METADATA_SCAN_LINES = 200
TERMINATE_GRACE_SECONDS = 10


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


class _StderrPump(threading.Thread):
    """Sole consumer of provider stderr.

    A single blocking reader thread scans a bounded number of initial lines
    for the provider metadata object, delivers it to the main thread through
    a queue, and forwards every other line to the terminal. The raw metadata
    line is never forwarded because it carries the E2EE password. The file
    descriptor is never switched to nonblocking.
    """

    def __init__(self, pipe, metadata_queue: queue.Queue, target) -> None:
        super().__init__(daemon=True)
        self._pipe = pipe
        self._metadata_queue = metadata_queue
        self._target = target

    def run(self) -> None:
        found = False
        scanned = 0
        try:
            for line in self._pipe:
                if not found and scanned < METADATA_SCAN_LINES:
                    scanned += 1
                    candidate = try_parse_json_line(line)
                    if candidate is not None and is_provider_metadata_dict(candidate):
                        found = True
                        try:
                            self._metadata_queue.put_nowait(line)
                        except queue.Full:
                            pass
                        continue
                try:
                    self._target.write(line)
                    self._target.flush()
                except (OSError, ValueError):
                    break
        except (OSError, ValueError):
            pass
        finally:
            if not found:
                try:
                    self._metadata_queue.put_nowait("")
                except queue.Full:
                    pass


def _terminate_bounded(proc, grace: float = TERMINATE_GRACE_SECONDS):
    """Terminate a started wrapper/Worker with a bounded grace period.

    Returns the process return code, or None when it refuses to exit.
    Targets only this invocation's process; never anything else.
    """
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        return proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    except (OSError, KeyboardInterrupt):
        pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        return proc.wait(timeout=grace)
    except (subprocess.TimeoutExpired, OSError, KeyboardInterrupt):
        return None


def _wait_for_worker(proc):
    """Wait for the wrapped Worker; terminate it boundedly on interrupt.

    A KeyboardInterrupt is the helper's interruption reason, not the child's
    exit result: always report 130 so Ctrl-C can never look like success.
    """
    try:
        return proc.wait()
    except KeyboardInterrupt:
        print("run_worker_observed: interrupted; terminating observed Worker invocation")
        _terminate_bounded(proc)
        return 130


def _install_sigterm_forward(proc, cleanup_flag):
    """Forward SIGTERM to the wrapped process, then exit 143 via SystemExit.

    Marks exceptional cleanup so the surrounding finally block performs
    targeted session cleanup. The flag is a plain dict so the handler can
    set it without touching process state. Returns the previous SIGTERM
    disposition for restoration.
    """
    previous = signal.getsignal(signal.SIGTERM)

    def _handler(signum, frame):
        cleanup_flag["required"] = True
        try:
            proc.terminate()
        except OSError:
            pass
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, _handler)
    return previous


def _fail_closed(proc, session_id: str | None, shell_cmd: str | None, reason_code: str) -> int:
    """Handle a post-start observer security violation without rerunning work.

    Stops the unsafe session when its id is known, terminates the started
    invocation boundedly, and returns non-zero. The Worker command is never
    executed a second time: it may already have modified the target project.
    """
    print(format_error_log(reason_code))
    print(format_error_log(OBSERVER_SECURITY_VALIDATION_FAILED))
    if session_id:
        print(f"unsafe observer session rejected; stopping session {session_id}")
        try:
            stop_session(session_id, shell_cmd)
        except ObserverError as exc:
            print(format_error_log(exc.code))
        print(format_end_log(session_id))
    else:
        print(
            "unsafe observer metadata has no usable session id; "
            "stopping the invocation without provider cleanup"
        )
    _terminate_bounded(proc)
    return 1


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
    metadata_queue: queue.Queue = queue.Queue(maxsize=1)
    pump = _StderrPump(proc.stderr, metadata_queue, sys.stderr)
    pump.start()
    # Exceptional cleanup is opt-in: task-bound observer sessions normally
    # close with the Worker process, so a normal exit performs no kill.
    # Interrupts, signals, and abnormal termination set this flag to request
    # targeted `shell kill <session-id>` for exactly this session.
    cleanup_flag = {"required": False}
    previous_sigterm = _install_sigterm_forward(proc, cleanup_flag)
    # Set only on the fully validated success path. The finally block emits
    # LIVE_OBSERVER_END for exactly that session and stops it only when
    # exceptional cleanup was requested. Fail-closed paths stop their
    # session directly.
    published_session = None
    exit_code = 1
    try:
        try:
            metadata_text = metadata_queue.get(timeout=metadata_timeout)
        except queue.Empty:
            metadata_text = None
        if not metadata_text:
            # Availability failure (late or absent metadata): the wrapped
            # Worker is already running, so keep it, drain stderr via the
            # pump, and return the real Worker result.
            print(format_error_log("OBSERVER_JSON_INVALID"))
            print("observer unavailable; Worker continues without a published link")
            return _normalize_exit_code(_wait_for_worker(proc))

        raw_metadata = try_parse_json_line(metadata_text)
        if raw_metadata is None or not is_provider_metadata_dict(raw_metadata):
            print(format_error_log("OBSERVER_JSON_INVALID"))
            print("observer unavailable; Worker continues without a published link")
            return _normalize_exit_code(_wait_for_worker(proc))

        # Identity extraction is separate from security validation: the id is
        # opaque and used only for targeted cleanup of this session.
        session_id = extract_session_id(raw_metadata)
        # Discard the password immediately: projection keeps the URL only.
        raw_metadata.pop("e2ee_password", None)
        try:
            session = project_safe_metadata(raw_metadata)
        except ObserverError as exc:
            return _fail_closed(proc, session_id, shell_cmd or get_shell_command(), exc.code)
        finally:
            raw_metadata.clear()

        published_session = session
        print(format_start_log(session), flush=True)
        print(
            "Live observer started.\n"
            "Browser password is managed by shell.online.\n"
            "Retrieve it locally with:\n"
            "    shell list"
        )
        try:
            announcement = build_observer_announcement(session, task_id=task_id)
        except ObserverError as exc:
            return _fail_closed(
                proc, session.session_id, shell_cmd or get_shell_command(), exc.code
            )
        posted = post_observer_announcement(bridge_url, worker_token, announcement, task_id)
        if not posted:
            print("observer announcement not delivered; Worker continues")
        try:
            returncode = proc.wait()
        except KeyboardInterrupt:
            print("run_worker_observed: interrupted; terminating observed Worker invocation")
            cleanup_flag["required"] = True
            _terminate_bounded(proc)
            # 130 is the helper's interruption reason; the child's cleanup
            # exit code must never overwrite it into a false success.
            returncode = 130
        if returncode is not None and returncode < 0:
            # Killed by a signal: abnormal termination where the provider
            # session may remain active, so request targeted cleanup.
            cleanup_flag["required"] = True
        exit_code = _normalize_exit_code(returncode)
        return exit_code
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        if published_session is not None:
            if cleanup_flag["required"]:
                try:
                    stop_session(published_session.session_id, shell_cmd or get_shell_command())
                except ObserverError as exc:
                    print(format_error_log(exc.code))
            print(format_end_log(published_session.session_id))
    return exit_code


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
