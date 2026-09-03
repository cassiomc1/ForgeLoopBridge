"""shell.online adapter for the Live Execution Observer (Phase 1).

Read-only only. E2EE required. Optional and fail-open: when the provider
is missing or unusable, the Worker continues without an observer.
Terminal output is observational only and is never canonical evidence.

Upstream boundary (re-verified against https://shell.online/ and
https://github.com/TeoSlayer/shell.online before coding):

- `shell --read-only --json -- <command>` prints a single stderr JSON line
  with `share_url`, `e2ee_password`, `session_id`, `read_only`,
  `encrypted`, and `background`.
- `shell list --json` exposes `relay_status` (`online`, `reconnecting`,
  `expired`, `unknown`) plus the owner-only `e2ee_password`.
- `shell kill <session-id>` stops one session; task-bound sessions also
  close automatically when the wrapped command exits.
- Sessions detach (background) by default; `--foreground` mirrors the
  process locally. The observer helper requires `--foreground` so the
  orchestrator still observes the real Worker exit; without it the wrapper
  could exit while the Worker continues detached.
- `--read-only` is fixed at creation and enforced server-side.
- E2EE is default; `--no-e2ee` is never invoked by this integration.

All subprocesses use argv arrays, `shell=False`, and bounded timeouts.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass

from examples.live_observer.base import (
    ENDED,
    EXPIRED,
    OBSERVER_EXECUTABLE_NOT_FOUND,
    OBSERVER_JSON_INVALID,
    OBSERVER_START_FAILED,
    OBSERVER_STATUS_FAILED,
    OBSERVER_STOP_FAILED,
    ONLINE,
    RECONNECTING,
    UNKNOWN,
    ObserverError,
    ObserverSession,
    ParsedObserverStart,
    get_shell_command,
    map_relay_status,
    project_safe_metadata,
)

PROVIDER_NAME = "shell-online"
PROVIDER_LABEL = "shell.online"

PREFLIGHT_TIMEOUT_SECONDS = 10
START_METADATA_TIMEOUT_SECONDS = 15
STATUS_TIMEOUT_SECONDS = 10
STOP_TIMEOUT_SECONDS = 10

MAX_START_OUTPUT_BYTES = 256 * 1024

# Cached feature-probe results for the process lifetime.
_probe_cache: dict[str, dict] = {}


def _run_bounded(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """Run argv without a shell and with a bounded timeout."""
    return subprocess.run(
        argv,
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout,
        shell=False,
    )


def resolve_executable(shell_cmd: str | None = None) -> str:
    cmd = (shell_cmd or get_shell_command()).strip() or "shell"
    # `shutil.which` handles bare names and absolute paths without a shell.
    resolved = shutil.which(cmd) if "/" not in cmd else (cmd if Path_exists(cmd) else None)
    if resolved is None:
        raise ObserverError(
            OBSERVER_EXECUTABLE_NOT_FOUND,
            f"observer executable not found: {cmd}",
        )
    return resolved


def Path_exists(path: str) -> bool:
    import os as _os

    return _os.path.exists(path) and _os.access(path, _os.X_OK)


def preflight(shell_cmd: str | None = None) -> dict:
    """Bounded feature probing: executable, --version, and help reference.

    Results are cached for the process lifetime. Prefers feature detection
    over version inference.
    """
    cmd = (shell_cmd or get_shell_command()).strip() or "shell"
    if cmd in _probe_cache:
        return _probe_cache[cmd]
    executable = resolve_executable(cmd)
    try:
        version_proc = _run_bounded([executable, "--version"], PREFLIGHT_TIMEOUT_SECONDS)
        help_proc = _run_bounded(
            [executable, "help", "reference"], PREFLIGHT_TIMEOUT_SECONDS
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ObserverError(
            OBSERVER_EXECUTABLE_NOT_FOUND,
            f"observer preflight did not complete: {exc.__class__.__name__}",
        ) from exc
    version_text = (version_proc.stdout or "") + (version_proc.stderr or "")
    help_text = (help_proc.stdout or "") + (help_proc.stderr or "")
    probe = {
        "executable": executable,
        "version_output": version_text[:4000],
        "help_output": help_text[:16000],
        "supports_read_only": "--read-only" in help_text,
        "supports_json": "--json" in help_text,
        "supports_list_json": "list" in help_text and "--json" in help_text,
        "supports_foreground": "--foreground" in help_text,
        "supports_kill": "kill" in help_text,
    }
    _probe_cache[cmd] = probe
    return probe


def clear_probe_cache() -> None:
    _probe_cache.clear()


def available(shell_cmd: str | None = None) -> bool:
    """Return True when the provider passes bounded preflight (fail-open)."""
    try:
        probe = preflight(shell_cmd)
    except ObserverError:
        return False
    return bool(probe.get("supports_read_only") and probe.get("supports_json"))


def build_wrapper_argv(
    worker_command: list[str],
    shell_cmd: str | None = None,
    *,
    foreground: bool = True,
) -> list[str]:
    """Construct the safe wrapper argv for an observed Worker command.

    Uses `--read-only --json` always, plus `--foreground` when requested and
    advertised (required to preserve Worker exit semantics). Never uses
    `--no-e2ee`, `--persistent`, `--interactive`, or `kill --all`.
    """
    if not worker_command:
        raise ObserverError(OBSERVER_START_FAILED, "worker command must not be empty")
    executable = resolve_executable(shell_cmd)
    argv = [executable]
    # Read-only is a Phase 1 invariant; E2EE stays default (no --no-e2ee).
    argv += ["--read-only", "--json"]
    if foreground:
        try:
            probe = preflight(shell_cmd)
        except ObserverError:
            probe = {}
        if probe.get("supports_foreground", True):
            argv += ["--foreground"]
        else:
            raise ObserverError(
                OBSERVER_START_FAILED,
                "provider does not advertise --foreground; refusing unsafe background wrap",
            )
    argv += ["--", *worker_command]
    return argv


def _iter_candidate_json_blobs(text: str):
    """Yield candidate JSON objects: whole text first, then line by line."""
    stripped = (text or "").strip()
    if not stripped:
        return
    if len(stripped.encode("utf-8")) > MAX_START_OUTPUT_BYTES:
        stripped = stripped[:MAX_START_OUTPUT_BYTES]
    # Whole-text attempt (covers single-object stdout/stderr).
    try:
        value = json.loads(stripped)
        if isinstance(value, dict):
            yield value
            return
    except (json.JSONDecodeError, ValueError):
        pass
    for line in stripped.splitlines():
        line = line.strip()
        if not line.startswith("{") or not line.endswith("}"):
            continue
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(value, dict):
            yield value


def parse_start_output(stdout: str, stderr: str) -> ParsedObserverStart:
    """Parse provider start metadata from stdout/stderr text.

    Upstream prints the session JSON on a single stderr line; this parser
    accepts it from either stream for robustness. Raises OBSERVER_JSON_INVALID
    on malformed output, and fail-closed read-only/E2EE/URL errors otherwise.
    """
    combined_candidates: list[dict] = []
    for chunk in (stderr or "", stdout or ""):
        combined_candidates.extend(_iter_candidate_json_blobs(chunk))
    if not combined_candidates:
        raise ObserverError(OBSERVER_JSON_INVALID, "provider produced no JSON metadata")
    # Prefer a candidate that carries a session identity.
    for candidate in combined_candidates:
        if candidate.get("session_id") and candidate.get("share_url"):
            return project_safe_metadata(candidate)
    return project_safe_metadata(combined_candidates[0])


def parse_list_output(stdout: str) -> list[dict]:
    """Parse `shell list --json` output into a list of session dicts."""
    text = (stdout or "").strip()
    if not text:
        return []
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ObserverError(OBSERVER_STATUS_FAILED, "provider status output is invalid") from exc
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("sessions", "items", "data"):
            nested = value.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
        return [value]
    raise ObserverError(OBSERVER_STATUS_FAILED, "provider status output is invalid")


def find_session_entry(entries: list[dict], session_id: str) -> dict | None:
    """Find one session entry by opaque id or id prefix (no interpretation)."""
    wanted = str(session_id).strip()
    if not wanted:
        return None
    for entry in entries:
        for key in ("session_id", "sessionId", "id", "session"):
            candidate = entry.get(key)
            if isinstance(candidate, str) and candidate.strip() == wanted:
                return entry
    # Upstream `attach`/`kill` accept an id prefix; status lookup tolerates
    # the same spelling without treating the prefix as a different session.
    for entry in entries:
        for key in ("session_id", "sessionId", "id", "session"):
            candidate = entry.get(key)
            if isinstance(candidate, str) and candidate.strip().startswith(wanted):
                return entry
    return None


def query_status(session_id: str, shell_cmd: str | None = None) -> str:
    """Reconcile one session via `shell list --json` (diagnostic only).

    Returns the internal non-canonical status (ONLINE/RECONNECTING/EXPIRED/
    UNKNOWN). Never maps to Bridge task state, ForgeLoop lifecycle, Worker
    completion, or verification status.
    """
    wanted = str(session_id or "").strip()
    if not wanted:
        raise ObserverError(OBSERVER_STATUS_FAILED, "session id must not be empty")
    executable = resolve_executable(shell_cmd)
    try:
        proc = _run_bounded(
            [executable, "list", "--json"], STATUS_TIMEOUT_SECONDS
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ObserverError(
            OBSERVER_STATUS_FAILED,
            f"provider status query did not complete: {exc.__class__.__name__}",
        ) from exc
    if proc.returncode != 0:
        raise ObserverError(OBSERVER_STATUS_FAILED, "provider status query failed")
    entries = parse_list_output(proc.stdout or "")
    entry = find_session_entry(entries, wanted)
    if entry is None:
        return UNKNOWN
    relay = entry.get("relay_status", entry.get("relay"))
    if not isinstance(relay, str) or not relay.strip():
        return UNKNOWN
    return map_relay_status(relay)


def stop_session(session_id: str, shell_cmd: str | None = None) -> None:
    """Stop only the session created by this helper (`shell kill <id>`)."""
    wanted = str(session_id or "").strip()
    if not wanted:
        raise ObserverError(OBSERVER_STOP_FAILED, "session id must not be empty")
    if wanted in ("--all", "-a") or wanted.startswith("-"):
        raise ObserverError(OBSERVER_STOP_FAILED, "refusing unsafe session selector")
    executable = resolve_executable(shell_cmd)
    try:
        proc = _run_bounded([executable, "kill", wanted], STOP_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ObserverError(
            OBSERVER_STOP_FAILED,
            f"provider stop did not complete: {exc.__class__.__name__}",
        ) from exc
    if proc.returncode != 0:
        raise ObserverError(OBSERVER_STOP_FAILED, "provider stop failed")


@dataclass(frozen=True)
class ShellOnlineProvider:
    """Thin adapter object wrapping the module functions."""

    shell_cmd: str | None = None
    name: str = PROVIDER_NAME

    def available(self) -> bool:
        return available(self.shell_cmd)

    def start_argv(self, command: list[str]) -> list[str]:
        return build_wrapper_argv(command, self.shell_cmd)

    def parse_start(self, stdout: str, stderr: str) -> ParsedObserverStart:
        return parse_start_output(stdout, stderr)

    def status(self, session_id: str) -> str:
        return query_status(session_id, self.shell_cmd)

    def stop(self, session_id: str) -> None:
        return stop_session(session_id, self.shell_cmd)


# Re-export relay mappings used by helper/tests without raw JSON retention.
__all__ = [
    "PROVIDER_NAME",
    "PROVIDER_LABEL",
    "ShellOnlineProvider",
    "available",
    "build_wrapper_argv",
    "clear_probe_cache",
    "find_session_entry",
    "parse_list_output",
    "parse_start_output",
    "preflight",
    "query_status",
    "resolve_executable",
    "stop_session",
    "ONLINE",
    "RECONNECTING",
    "EXPIRED",
    "UNKNOWN",
    "ENDED",
    "ObserverError",
    "ObserverSession",
]
