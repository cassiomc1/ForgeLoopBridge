"""Live Execution Observer provider abstraction.

The observer is optional, lazy/opt-in, and read-only. It exposes a live
PTY view through an external provider (Phase 1: shell.online) without
becoming part of the ForgeLoopBridge coordination authority or the
ForgeLoop canonical engineering authority.

Terminal output is observational only and is never canonical evidence.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

# ─── Error codes (observer namespace; never ForgeLoop reason codes) ──────────

OBSERVER_EXECUTABLE_NOT_FOUND = "OBSERVER_EXECUTABLE_NOT_FOUND"
OBSERVER_START_FAILED = "OBSERVER_START_FAILED"
OBSERVER_JSON_INVALID = "OBSERVER_JSON_INVALID"
OBSERVER_UNSAFE_ACCESS_MODE = "OBSERVER_UNSAFE_ACCESS_MODE"
OBSERVER_ENCRYPTION_DISABLED = "OBSERVER_ENCRYPTION_DISABLED"
OBSERVER_URL_INVALID = "OBSERVER_URL_INVALID"
OBSERVER_STATUS_FAILED = "OBSERVER_STATUS_FAILED"
OBSERVER_STOP_FAILED = "OBSERVER_STOP_FAILED"
OBSERVER_SECURITY_VALIDATION_FAILED = "OBSERVER_SECURITY_VALIDATION_FAILED"

# ─── Configuration ───────────────────────────────────────────────────────────

ENV_PROVIDER = "FORGEBRIDGE_LIVE_OBSERVER"
ENV_COMMAND = "FORGEBRIDGE_LIVE_OBSERVER_COMMAND"

PROVIDER_NONE = "none"
PROVIDER_SHELL_ONLINE = "shell-online"

SUPPORTED_PROVIDERS = frozenset({PROVIDER_NONE, PROVIDER_SHELL_ONLINE})

# Phase 1 access mode is always read-only. No interactive option exists.
ACCESS_MODE = "READ_ONLY"

DEFAULT_SHELL_COMMAND = "shell"

# ─── Internal observer status model (non-canonical diagnostics only) ─────────

NOT_CONFIGURED = "NOT_CONFIGURED"
UNAVAILABLE = "UNAVAILABLE"
STARTING = "STARTING"
ONLINE = "ONLINE"
RECONNECTING = "RECONNECTING"
EXPIRED = "EXPIRED"
UNKNOWN = "UNKNOWN"
ENDED = "ENDED"
ERROR = "ERROR"

# Provider relay values reported by `shell list --json`.
PROVIDER_RELAY_VALUES = frozenset({"online", "reconnecting", "expired", "unknown"})

_RELAY_TO_INTERNAL = {
    "online": ONLINE,
    "reconnecting": RECONNECTING,
    "expired": EXPIRED,
    "unknown": UNKNOWN,
}

# ─── Share URL policy ────────────────────────────────────────────────────────

MAX_SHARE_URL_LENGTH = 2048
# Expected provider hosts for Phase 1. The fragment (e.g. `#salt=...`) is
# preserved because E2EE links require it; it is never fetched server-side.
ALLOWED_SHARE_HOSTS = frozenset({"shell.online"})


class ObserverError(Exception):
    """Observer diagnostic with a stable machine-readable code."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class ObserverSession:
    """Allow-listed, non-secret observer metadata safe for coordination."""

    provider: str
    session_id: str
    share_url: str
    read_only: bool
    encrypted: bool
    relay_status: str | None = None


class LiveObserverProvider(Protocol):
    """Minimal provider surface. No plugin framework beyond one adapter."""

    name: str

    def available(self) -> bool:
        """Return True when the provider executable passes bounded preflight."""
        ...

    def start(self, command: list[str]) -> ObserverSession:
        """Start an observed worker command and return safe metadata only."""
        ...

    def status(self, session_id: str) -> str:
        """Return the internal observer status for a session id."""
        ...

    def stop(self, session_id: str) -> None:
        """Stop only the session created by this helper."""
        ...


# ─── Configuration helpers ───────────────────────────────────────────────────


def get_provider_name() -> str:
    """Return the normalized configured provider name (default: none)."""
    raw = os.getenv(ENV_PROVIDER, PROVIDER_NONE).strip().lower()
    if not raw:
        return PROVIDER_NONE
    return raw


def get_shell_command() -> str:
    """Return the configured provider executable name (default: shell)."""
    raw = os.getenv(ENV_COMMAND, DEFAULT_SHELL_COMMAND).strip()
    return raw or DEFAULT_SHELL_COMMAND


def is_observer_enabled() -> bool:
    """Observer is disabled by default and must be explicitly opted in."""
    return get_provider_name() == PROVIDER_SHELL_ONLINE


def is_supported_provider(name: str) -> bool:
    return name.strip().lower() in SUPPORTED_PROVIDERS


# ─── URL validation (no server-side fetch; prevents SSRF-adjacent misuse) ───


def validate_share_url(url: str) -> str:
    """Validate an observer share URL and return it unchanged.

    Requires HTTPS on the expected provider host, rejects embedded
    userinfo/credentials and unsafe schemes, and enforces a bounded length.
    The provider-required URL fragment (E2EE `#salt=...`) is preserved.
    """
    if not isinstance(url, str):
        raise ObserverError(OBSERVER_URL_INVALID, "share URL must be a string")
    if not url or len(url) > MAX_SHARE_URL_LENGTH:
        raise ObserverError(OBSERVER_URL_INVALID, "share URL has an invalid length")
    try:
        parsed = urlparse(url)
    except (ValueError, AttributeError) as exc:
        raise ObserverError(OBSERVER_URL_INVALID, "share URL is malformed") from exc
    if parsed.scheme != "https":
        raise ObserverError(OBSERVER_URL_INVALID, "share URL must use https")
    if not parsed.hostname:
        raise ObserverError(OBSERVER_URL_INVALID, "share URL must have a host")
    # Reject embedded userinfo/credentials such as `https://user:pass@host/`.
    if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
        raise ObserverError(OBSERVER_URL_INVALID, "share URL must not embed credentials")
    hostname = parsed.hostname.lower()
    if hostname != "shell.online" and not hostname.endswith(".shell.online"):
        raise ObserverError(OBSERVER_URL_INVALID, "share URL host is not the provider host")
    return url


# ─── Sanitized provider projection ───────────────────────────────────────────


def _require_non_empty_str(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ObserverError(OBSERVER_JSON_INVALID, f"provider field {field!r} is missing")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ObserverError(OBSERVER_JSON_INVALID, f"provider field {field!r} is malformed")
    return value.strip()


def extract_session_id(raw: object) -> str | None:
    """Extract the opaque provider session id without validating anything else.

    Used for targeted cleanup (`shell kill <session-id>`) before or without
    full security validation. The id is treated as opaque: it is never
    interpreted, never guessed, and never trusted semantically. Returns None
    when no usable id is present.
    """
    if not isinstance(raw, dict):
        return None
    value = raw.get("session_id")
    if not isinstance(value, str) or not value.strip():
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    return value.strip()


def project_safe_metadata(raw: dict) -> ObserverSession:
    """Project raw provider JSON onto the allow-list and discard the secret.

    Enforces read-only + E2EE (fail closed) and validates the share URL.
    The E2EE password is never read into Bridge state: it is discarded
    immediately and never reaches logs, messages, or persistence. shell.online
    retains its own owner-side session record; operators retrieve the
    password locally with `shell list`.
    """
    if not isinstance(raw, dict):
        raise ObserverError(OBSERVER_JSON_INVALID, "provider output must be a JSON object")
    read_only = raw.get("read_only")
    encrypted = raw.get("encrypted")
    if read_only is not True:
        raise ObserverError(
            OBSERVER_UNSAFE_ACCESS_MODE,
            "observer session is not read-only; refusing to publish",
        )
    if encrypted is not True:
        raise ObserverError(
            OBSERVER_ENCRYPTION_DISABLED,
            "observer session is not end-to-end encrypted; refusing to publish",
        )
    session_id = _require_non_empty_str(raw.get("session_id"), "session_id")
    share_url = _require_non_empty_str(raw.get("share_url"), "share_url")
    validate_share_url(share_url)
    relay_status = raw.get("relay_status")
    if relay_status is not None and (
        not isinstance(relay_status, str) or not relay_status.strip()
    ):
        raise ObserverError(OBSERVER_JSON_INVALID, "provider field 'relay_status' is malformed")
    normalized_relay = relay_status.strip().lower() if isinstance(relay_status, str) else None
    return ObserverSession(
        provider="shell.online",
        session_id=session_id,
        share_url=share_url,
        read_only=True,
        encrypted=True,
        relay_status=normalized_relay,
    )


def map_relay_status(relay: str | None) -> str:
    """Map a provider relay value to the internal non-canonical status model.

    Provider states are observer diagnostics only: they never map to Bridge
    task state, ForgeLoop lifecycle, Worker completion, or verification.
    """
    if relay is None:
        return UNKNOWN
    normalized = str(relay).strip().lower()
    return _RELAY_TO_INTERNAL.get(normalized, UNKNOWN)


# ─── Bridge message convention (Markdown coordination, Schema v1 unchanged) ──


def build_observer_announcement(
    session: ObserverSession,
    *,
    task_id: str | None = None,
) -> str:
    """Build the single per-invocation observer-start Markdown announcement.

    The announcement carries the validated share URL only. It never includes
    the E2EE password and never claims canonical authority or evidence.
    """
    if not session.read_only or not session.encrypted:
        raise ObserverError(
            OBSERVER_UNSAFE_ACCESS_MODE,
            "refusing to announce an observer session that is not read-only + E2EE",
        )
    validate_share_url(session.share_url)
    lines = [
        "### Live Execution Observer",
        "",
        "The current Worker turn can be observed live.",
        "",
        "- Provider: `shell.online`",
        "- Access: `READ_ONLY`",
        "- Encryption: `E2EE`",
        f"- Session: `{session.session_id}`",
        f"- [Open live terminal]({session.share_url})",
        "",
        "This terminal is observational only.",
        "",
        "Use ForgeLoopBridge for Engineer \u2194 Worker instructions.",
        "Terminal output is not canonical ForgeLoop evidence.",
    ]
    if task_id:
        cleaned = str(task_id).strip()
        if cleaned:
            lines.insert(3, f"- Task: `{cleaned}`")
    return "\n".join(lines) + "\n"


# ─── Safe structured logging (never secrets or raw provider JSON) ────────────


def format_start_log(session: ObserverSession) -> str:
    return (
        f"LIVE_OBSERVER_START provider=shell.online "
        f"session={session.session_id} mode=READ_ONLY encrypted=true"
    )


def format_status_log(session_id: str, relay: str) -> str:
    safe_relay = "".join(
        char if char.isalnum() or char in ("-", "_") else "_" for char in str(relay)
    )[:64] or "unknown"
    safe_session = "".join(
        char if char.isalnum() or char in ("-", "_") else "_" for char in str(session_id)
    )[:128] or "session"
    return f"LIVE_OBSERVER_STATUS provider=shell.online session={safe_session} relay={safe_relay}"


def format_end_log(session_id: str) -> str:
    safe_session = "".join(
        char if char.isalnum() or char in ("-", "_") else "_" for char in str(session_id)
    )[:128] or "session"
    return f"LIVE_OBSERVER_END provider=shell.online session={safe_session}"


def format_error_log(code: str) -> str:
    safe_code = "".join(
        char if char.isalnum() or char in ("-", "_") else "_" for char in str(code)
    )[:64] or "UNKNOWN"
    return f"LIVE_OBSERVER_ERROR type=ObserverError code={safe_code}"
