#!/usr/bin/env python3
"""
Minimal example of how the Worker can monitor ForgeLoopBridge.
This script is a transport adapter. When an instruction is received, invoke
your Worker agent/harness (e.g. OpenCode, Cursor, or local agent). Typed
outbound messages are persisted without authentication material, replayed
before normal polling, and quarantined only when delivery is permanently
rejected. HTTP 408, 425, 429, and 5xx responses remain pending with bounded
backoff or `Retry-After` scheduling, so temporary Bridge backpressure is not a
project failure.

The Worker agent MUST use the current advertised ForgeLoop protocol/capability
workflow:
1. Prefer official ForgeLoop structured integration when exposed by the host;
   otherwise use the project-local ForgeLoop CLI.
2. Inspect `forgeloop protocol-info --json` and feature-detect capabilities;
   include `verificationExecutionIsolation` and `structuralQuality` when
   advertised; never infer capabilities from a package version alone or
   self-attest a trusted isolation boundary.
3. When `FORGELOOP_CONTEXT_COMMAND` is configured, read the canonical
   `task/context` projection through that host adapter; use its resolved profile
   and bounded policy, and never classify the task locally.
4. Discover existing tasks (`forgeloop task-list --json`) before creating a new one.
5. Treat canonical `forgeloop next` as the dispatcher after every meaningful
   protocol mutation. The example lifecycle is a happy-path illustration only.
6. Respect canonical action, approval, policy, diagnostic, and reconciliation
   guidance. `COMMIT_UNKNOWN` is a hard stop: do not retry the action.
7. Reach VALID completion with `forgeloop complete --task <task-id> --json` and
   verify terminal `nextAction: NONE` before posting a COMPLETE status;
   otherwise report the exact blocked/partial state.
8. Open PR and report structured Markdown status on ForgeLoopBridge.

Usage:
    python worker_poll.py [--auto-ack] [--start-mode pending|now|history]
                          [--run-mode daemon|once|bounded] [--max-idle-polls N]

`--auto-ack` posts an immediate receipt/status acknowledgement for each new
instruction. It never resolves approvals, grants authority, or changes
ForgeLoop state. Disabled by default to keep the board clean.

`--start-mode pending` is the default and hands off the latest existing
Engineer instruction on a first start. Use `--start-mode now` explicitly to
ignore messages already on the board, or `--start-mode history` to replay from
cursor zero.

Transport liveness and agent-turn liveness are separate concerns:

- `--run-mode daemon` (default, unchanged behavior) is the continuous transport
  adapter: poll, sleep, repeat until the process is terminated externally.
- `--run-mode once` performs exactly one cycle over the currently available
  Bridge delta and exits; it never sleeps.
- `--run-mode bounded` adds a short grace window and exits after
  `--max-idle-polls` consecutive polls that deliver no new Engineer
  instruction (default 2). A handled Engineer instruction resets the counter.

Use `once`/`bounded` for an ephemeral AI Worker turn launched by an Engineer or
orchestrator: consume the new coordination, do the currently actionable work,
report `STATUS_UPDATE(state="WAITING")` with `WAITING_FOR_ENGINEER` (or the
canonical blocker) and exit. Do not poll indefinitely inside a foreground
Worker process while the Engineer is blocked waiting for that process; that is
the orchestration deadlock this mode exists to prevent. A bounded exit is
coordination state only and never canonical ForgeLoop completion.
"""

import argparse
import json
import math
import os
import secrets
import shlex
import subprocess
import sys
import time
from pathlib import Path

import requests

# Keep the documented `python examples/worker_poll.py` entry point usable from
# a source checkout while still allowing the example to be imported normally.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bridge_protocol.forgeloop_context import (  # noqa: E402
    consume_task_context,
    unavailable_context,
)
from bridge_protocol.models import ContextUsageReport  # noqa: E402

BASE_URL = "http://localhost:8000"          # change to your ForgeLoopBridge URL
WORKER_TOKEN = "change-me"                  # same token configured on the server
POLL_INTERVAL = 10                          # seconds
STATE_FILE = Path(__file__).parent / ".worker_last_seen"  # survives restarts
OUTBOX_FILE = Path(__file__).parent / ".worker_typed_outbox.json"  # survives uncertain POSTs
MAX_OUTBOX_ENTRIES = 100
MAX_OUTBOX_BYTES = 1024 * 1024
TRANSIENT_HTTP_STATUSES = frozenset({
    408,  # Request Timeout
    425,  # Too Early
    429,  # Too Many Requests
})
MAX_RETRY_AFTER_SECONDS = 300.0
MAX_RETRY_BACKOFF_SECONDS = 60.0
MIN_RETRY_DELAY_SECONDS = 1.0
OUTBOX_FORBIDDEN_KEYS = frozenset(
    {
        "accesstoken",
        "authorization",
        "bearer",
        "clientsecret",
        "cookie",
        "engineertoken",
        "oidctoken",
        "privatekey",
        "signingkey",
        "sseticket",
        "ticket",
        "token",
        "workertoken",
    }
)
COMMIT_UNKNOWN_REASON_CODES = frozenset({"COMMIT_UNKNOWN", "E_ACTION_COMMIT_UNKNOWN"})
VERIFICATION_ISOLATION_REASON_CODES = frozenset(
    {
        "E_VERIFICATION_ISOLATION_UNAVAILABLE",
        "E_VERIFICATION_EXECUTION_INVALID",
    }
)
WORKSPACE_BOUNDARY_REASON_CODES = frozenset(
    {
        "E_WORKSPACE_IDENTITY_UNAVAILABLE",
        "E_WORKSPACE_BINDING_INVALID",
        "E_WORKSPACE_BINDING_MISMATCH",
    }
)
HANDOFF_REASON_CODES = frozenset(
    {
        "E_HANDOFF_INVALID",
        "E_HANDOFF_STATE_UNAVAILABLE",
        "E_HANDOFF_TAMPERED",
        "E_HANDOFF_NOT_FOUND",
        "E_HANDOFF_ACCEPTANCE_UNBOUND",
        "E_HANDOFF_STALE",
        "E_HANDOFF_ALREADY_ACCEPTED",
        "E_HANDOFF_ACCEPTANCE_INCONSISTENT",
    }
)
RESPONSIBILITY_BOUNDARY_REASON_CODES = frozenset(
    {
        "E_RESPONSIBILITY_INVALID",
        "E_RESPONSIBILITY_SCOPE_VIOLATION",
        "E_RESPONSIBILITY_FROZEN_INPUT_DRIFT",
        "E_RESPONSIBILITY_REQUIRED_CHECK_MISSING",
    }
)
VERIFICATION_SCOPE_REASON_CODES = frozenset(
    {
        "E_VERIFICATION_SCOPE_INVALID",
        "E_VERIFICATION_SCOPE_STALE",
        "E_VERIFICATION_SCOPE_UNRESOLVED",
    }
)
REVISION_PROVIDER_REASON_CODES = frozenset(
    {
        "E_REVISION_PROVIDER_UNAVAILABLE",
        "E_REVISION_PROVIDER_AMBIGUOUS",
        "E_REVISION_PROVIDER_INVALID",
        "E_REVISION_NOT_FOUND",
        "E_REVISION_CONTENT_UNAVAILABLE",
    }
)
ATTESTATION_BOUNDARY_REASON_CODES = frozenset(
    {
        "E_ATTESTATION_DISABLED",
        "E_ATTESTATION_GIT_REQUIRED",
        "E_ATTESTATION_MANIFEST_MISSING",
        "E_ATTESTATION_MANIFEST_INVALID",
        "E_ATTESTATION_MANIFEST_STALE",
        "E_ATTESTATION_CONTENT_MISMATCH",
        "E_ATTESTATION_SCOPE_MISMATCH",
        "E_ATTESTATION_COVERAGE_GAP",
        "E_ATTESTATION_COVERAGE_CONFLICT",
        "E_ATTESTATION_STATEMENT_MISSING",
        "E_ATTESTATION_STATEMENT_INVALID",
        "E_ATTESTATION_SUBJECT_MISMATCH",
        "E_ATTESTATION_RECEIPT_MISMATCH",
        "E_ATTESTATION_STATE_MISMATCH",
        "E_ATTESTATION_ROUTE_MISMATCH",
        "E_ATTESTATION_CONTRACT_MISMATCH",
        "E_ATTESTATION_LEDGER_MISMATCH",
        "E_ATTESTATION_UNSIGNED",
        "E_ATTESTATION_SIGNATURE_INVALID",
        "E_ATTESTATION_SIGNER_UNAVAILABLE",
        "E_ATTESTATION_IDENTITY_UNTRUSTED",
        "E_ATTESTATION_ISSUER_UNTRUSTED",
        "E_ATTESTATION_TARGET_REF_INVALID",
        "E_ATTESTATION_INVALID",
        "E_ATTESTATION_CONFIGURATION_INVALID",
    }
)
TYPED_MESSAGE_KINDS = frozenset(
    {
        "TASK_REQUEST",
        "STATUS_UPDATE",
        "DECISION_REQUEST",
        "DECISION_RESPONSE",
        "DECISION_NOTICE",
        "BLOCKER",
        "REVIEW_RESULT",
        "CONTROL_NOTICE",
        "HANDOFF_NOTICE",
        "VERIFICATION_REPORT",
        "ATTESTATION_REPORT",
    }
)
SUPPORTED_TYPED_SCHEMA_VERSIONS = frozenset({1})
START_MODES = ("pending", "now", "history")
RUN_MODES = ("daemon", "once", "bounded")
DEFAULT_MAX_IDLE_POLLS = 2
LATEST_PAGE_SIZE = 200
FORGELOOP_JSON_TIMEOUT_SECONDS = 30


FORGELOOP_BOOTSTRAP = """
Before creating or resuming protocol state, inspect:
  forgeloop protocol-info --json
Then use the advertised feature set for diagnostics, durableActions,
capabilityPolicy, durableApprovals, verificationExecutionIsolation,
workspaceBinding, canonicalHandoffs v2, advisoryContextProviders v1,
responsibilityConstraints,
differentialVerificationScope, codeAttestation, and structuralQuality.
When structuralQuality is advertised, preserve the canonical
`task/structural-quality` resource and treat `quality-status` as read-only.
Invoke `quality-baseline` or `quality-verify` only through the authorized
canonical ForgeLoop execution boundary. Package version is informational only;
feature support must be advertised by the canonical
protocol-info or structured integration result.

When workspaceBinding is supported, use only canonical workspace-bind and
workspace-status operations. A path, cwd, branch, copied checkout, or Bridge
message is not workspace identity proof. When canonicalHandoffs is supported,
use handoff-create/list/show for continuity, and use handoff-accept only by the
receiving harness when it actually consumes the handoff. A HANDOFF_NOTICE is not handoff acceptance.
Receiving a Bridge message, opening the Bridge UI, or
advancing a Bridge cursor must never append HANDOFF_ACCEPTED. A canonical
acceptance is an OPERATIONAL_RECEIPT_ONLY with no evidence and no claims
transferred; a handoff is not delegation, identity, approval, completion, or
verification evidence. When
responsibilityConstraints is supported, use responsibility-set/status and stop
on canonical scope or frozen-input violations. Never infer responsibility from
Markdown.

When advisoryContextProviders is advertised, treat it as optional, lazy,
opt-in, provider-neutral, Integration API-only context. Bridge never creates a
provider, recalls context because a message arrived, turns a message into a
provider result, or persists raw provider output as ForgeLoop state. A bounded
host-produced summary remains ordinary, non-authoritative coordination text;
it is non-evidence and non-executable.

When available, use `forgeloop reconcile-continuity --task <id> --json` as a
read-only resume diagnostic. Lint warnings are operational context only: they
are not a Bridge blocker, verification failure, or completion failure. Follow
canonical `forgeloop next` for the actual lifecycle action.

When differentialVerificationScope is supported, ask ForgeLoop for verify-scope
with AUTO and use its returned scope only through the trusted scoped checker.
AUTO falls back to FULL without that capability; explicit CHANGED/CLAIMED must
fail closed. Never calculate changed, claimed, or impacted paths locally.

Bridge typed messages are coordination records only. A persisted typed row with
`typed_integrity: INVALID` is a hard stop: keep its Markdown visible, do not
interpret the Markdown as a command, and do not advance the polling cursor.
Typed outbound retries keep the exact original request and `message_key`; the
local outbox never stores tokens, cookies, SSE tickets, signing credentials, or
OIDC material.

When codeAttestation is supported or required, use the canonical attestation
commands and canonical revision-provider results. Distinguish NOT_VERIFIED,
VERIFIED, and ATTESTED; an external validated signature is required for
ATTESTED. Never self-sign, promote trust, or store signing credentials in the
Bridge.

Query `forgeloop next --task <task-id> --json` after every meaningful mutation;
its nextAction, reasonCodes, authorityRequired, approvalRequired,
capabilityDecision, hostActionRequired, and reconciliationAuthorityRequired
fields take precedence over examples.

For profile-aware host integration, configure `FORGELOOP_CONTEXT_COMMAND` with
a local command that accepts `--task <id> --path <project> --json` and returns
the canonical `task/context` object (or an object containing it under `data`).
The poller invokes `forgeloop protocol-info --json` first, then consumes the
projection only when `adaptiveExecutionProfiles`, `executionProfileContext`,
and `task/context` are advertised. Older ForgeLoop installations use explicit
balanced compatibility behavior; an advertised but unavailable or malformed
projection is reported as unavailable and never replaced with a guessed light
profile. Set `FORGELOOP_PROJECT_PATH` when the worker process is not running in
the target project. Set `FORGELOOP_CLI` only to the executable plus fixed
arguments needed to invoke the project-local CLI; commands are never run
through a shell.
""".strip()


class UnsupportedTypedMessageVersion(ValueError):
    """Raised when a Worker cannot safely interpret a typed message schema."""


class InvalidTypedMessageIntegrity(ValueError):
    """Raised when persisted typed data cannot be trusted for dispatch."""


class OutboxLimitError(ValueError):
    """Raised when pending outbound coordination exceeds local safety limits."""


class OutboxKeyConflict(ValueError):
    """Raised when a local message key is reused for a different request."""


class OutboxSecurityError(ValueError):
    """Raised when an outbox value contains an authentication secret field."""


def _configured_command(name: str) -> list[str] | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        command = shlex.split(raw)
    except ValueError as exc:
        raise ValueError(f"{name} is not valid shell-style argument text") from exc
    if not command:
        raise ValueError(f"{name} must contain an executable")
    return command


def _run_json_command(command: list[str], arguments: list[str], project_root: Path) -> object:
    """Run a configured host adapter without shell expansion or secret logging."""
    try:
        result = subprocess.run(
            [*command, *arguments],
            cwd=project_root,
            capture_output=True,
            check=False,
            text=True,
            timeout=FORGELOOP_JSON_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"configured ForgeLoop adapter did not complete ({exc.__class__.__name__})") from exc
    if result.returncode != 0:
        raise RuntimeError(f"configured ForgeLoop adapter exited with status {result.returncode}")
    try:
        value = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("configured ForgeLoop adapter returned invalid JSON") from exc
    return value


def read_forgeloop_context(task_id: str | None) -> dict | None:
    """Read and validate canonical task/context when a host adapter is configured.

    A missing adapter is deliberately distinct from a missing canonical
    projection. The former leaves the example unconfigured; the latter is
    reported as unavailable and never becomes a locally guessed profile.
    """
    if not task_id:
        return None
    context_command = _configured_command("FORGELOOP_CONTEXT_COMMAND")
    if context_command is None:
        return None

    cli_command = _configured_command("FORGELOOP_CLI") or ["forgeloop"]
    project_root = Path(os.getenv("FORGELOOP_PROJECT_PATH") or os.getcwd()).resolve()
    try:
        protocol_info = _run_json_command(
            cli_command,
            ["protocol-info", "--json", "--path", str(project_root)],
            project_root,
        )
        raw_context = _run_json_command(
            context_command,
            ["--task", task_id, "--path", str(project_root), "--json"],
            project_root,
        )
        if isinstance(raw_context, dict) and isinstance(raw_context.get("data"), dict):
            raw_context = raw_context["data"]
        return consume_task_context(
            protocol_info if isinstance(protocol_info, dict) else None,
            raw_context if isinstance(raw_context, dict) else None,
            expected_task_id=task_id,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return unavailable_context(f"ForgeLoop context adapter failed: {exc}")


def build_context_status_payload(context: dict | None, usage: dict | None = None) -> dict:
    """Build typed status fields from canonical projection and host telemetry.

    The usage argument is copied only when the host supplies actual values.
    Missing values remain null; this helper never estimates totals or item
    counts and never accepts a Bridge message as canonical context.
    """
    if not isinstance(context, dict) or context.get("status") not in {
        "CANONICAL",
        "COMPATIBILITY_FALLBACK",
    }:
        return {}

    payload = {
        "execution_profile": context["execution_profile"],
        "context_policy": context["context_policy"],
    }
    if usage is None:
        return payload

    candidate = usage.get("contextUsage", usage) if isinstance(usage, dict) else {}
    raw_items = candidate.get("items", {}) if isinstance(candidate, dict) else {}
    if not isinstance(raw_items, dict):
        raw_items = {}
    item_names = {
        "taskContext": "task_context",
        "task_context": "task_context",
        "guides": "guides",
        "history": "history",
        "protocolInstructions": "protocol_instructions",
        "protocol_instructions": "protocol_instructions",
        "repositoryContext": "repository_context",
        "repository_context": "repository_context",
        "other": "other",
    }
    items = {
        field: raw_items.get(source)
        for source, field in item_names.items()
        if source in raw_items
    }
    normalized = ContextUsageReport.model_validate(
        {
            "source": candidate.get("source", "UNKNOWN") if isinstance(candidate, dict) else "UNKNOWN",
            "profile": (
                candidate.get("profile", context["execution_profile"].get("resolved"))
                if isinstance(candidate, dict)
                else context["execution_profile"].get("resolved")
            ),
            "items": items,
        }
    )
    payload["context_usage"] = normalized.model_dump(mode="json")
    return payload


def classify_reason_code(message: dict) -> str | None:
    """Classify only explicit top-level reason_code metadata."""
    reason_code = message.get("reason_code")
    if reason_code in WORKSPACE_BOUNDARY_REASON_CODES:
        return "workspace"
    if reason_code in HANDOFF_REASON_CODES:
        return "handoff"
    if reason_code in RESPONSIBILITY_BOUNDARY_REASON_CODES:
        return "responsibility"
    if reason_code in VERIFICATION_SCOPE_REASON_CODES:
        return "verification_scope"
    if reason_code in ATTESTATION_BOUNDARY_REASON_CODES:
        return "attestation"
    if reason_code in REVISION_PROVIDER_REASON_CODES:
        return "revision_provider"
    if reason_code in VERIFICATION_ISOLATION_REASON_CODES:
        return "verification_isolation"
    return None


def handle_task_request(message: dict) -> None:
    print("TYPED TASK_REQUEST: hand off the coordination request to the Worker agent.")


def handle_decision_response(message: dict) -> None:
    print("TYPED DECISION_RESPONSE: record the project decision; it is not ForgeLoop approval.")


def handle_decision_notice(message: dict) -> None:
    print("TYPED DECISION_NOTICE: record the unilateral project decision; it is not ForgeLoop approval.")


def handle_control_notice(message: dict) -> None:
    print("TYPED CONTROL_NOTICE: verify copied ForgeLoop guidance canonically before acting.")


def handle_review_result(message: dict) -> None:
    print("TYPED REVIEW_RESULT: treat the review as a project decision only.")


def handle_unknown_typed_message(message: dict) -> None:
    typed = message.get("typed") or {}
    print(
        "UNKNOWN TYPED MESSAGE: keep the Markdown visible and do not execute an "
        f"unrecognized kind ({typed.get('kind')!r})."
    )


def dispatch_typed_message(message: dict) -> str | None:
    """Dispatch typed messages without deriving commands from Markdown."""
    if message.get("typed_integrity") == "INVALID":
        error = message.get("typed_error") or {}
        raise InvalidTypedMessageIntegrity(
            "Persisted typed message is invalid; "
            f"do not fall back to Markdown ({error.get('code', 'unknown error')})."
        )
    if "typed" not in message or message.get("typed") is None:
        return None
    typed = message["typed"]
    if not isinstance(typed, dict):
        raise ValueError("typed message must be an object")

    schema_version = typed.get("schema_version")
    if schema_version not in SUPPORTED_TYPED_SCHEMA_VERSIONS:
        raise UnsupportedTypedMessageVersion(
            f"Unsupported typed message schema version: {schema_version!r}"
        )

    kind = typed.get("kind")
    payload = typed.get("payload")
    if not isinstance(payload, dict) or payload.get("kind") != kind:
        raise ValueError("typed envelope and payload kinds must match")

    handlers = {
        "TASK_REQUEST": handle_task_request,
        "DECISION_RESPONSE": handle_decision_response,
        "DECISION_NOTICE": handle_decision_notice,
        "CONTROL_NOTICE": handle_control_notice,
        "REVIEW_RESULT": handle_review_result,
    }
    handler = handlers.get(kind, handle_unknown_typed_message)
    handler(message)
    return kind


def _failed_outbox_file() -> Path:
    return OUTBOX_FILE.with_name(f"{OUTBOX_FILE.stem}_failed{OUTBOX_FILE.suffix}")


def _quarantine_file(path: Path, label: str) -> Path | None:
    if not path.exists():
        return None
    quarantine = path.with_name(
        f"{path.stem}.{label}.{time.time_ns()}-{secrets.token_hex(4)}{path.suffix}"
    )
    try:
        path.replace(quarantine)
    except OSError as exc:
        print(f"[outbox] unable to quarantine {path}: {exc.__class__.__name__}")
        return None
    print(f"[outbox] quarantined {path.name} as {quarantine.name}")
    return quarantine


def _contains_forbidden_outbox_key(value) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized_key = "".join(char for char in str(key).lower() if char.isalnum())
            if normalized_key in OUTBOX_FORBIDDEN_KEYS or any(
                marker in normalized_key
                for marker in ("authorization", "bearer", "cookie", "oidc", "signing", "secret")
            ) or normalized_key.endswith(("token", "privatekey")):
                return True
            if _contains_forbidden_outbox_key(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_forbidden_outbox_key(item) for item in value)
    return False


def _contains_forbidden_outbox_value(value) -> bool:
    secret_values = {secret for secret in (WORKER_TOKEN, os.getenv("ENGINEER_TOKEN")) if secret}
    if isinstance(value, str):
        return any(secret in value for secret in secret_values)
    if isinstance(value, dict):
        return any(
            (isinstance(key, str) and any(secret in key for secret in secret_values))
            or _contains_forbidden_outbox_value(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_outbox_value(item) for item in value)
    return False


def _contains_forbidden_outbox_data(value) -> bool:
    return _contains_forbidden_outbox_key(value) or _contains_forbidden_outbox_value(value)


def classify_delivery_status(status_code: int) -> str:
    """Classify an HTTP delivery result without collapsing transient 4xx responses."""
    if status_code in TRANSIENT_HTTP_STATUSES or 500 <= status_code <= 599:
        return "TRANSIENT"
    if 300 <= status_code <= 499:
        return "PERMANENT"
    return "UNKNOWN"


def parse_retry_after(response) -> float | None:
    """Parse a bounded delta-seconds Retry-After response header."""
    headers = getattr(response, "headers", {}) or {}
    raw = headers.get("Retry-After")
    if raw is None:
        raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


def _retry_delay(response, attempts: int) -> float:
    retry_after = parse_retry_after(response)
    if retry_after is not None:
        # A zero header is valid, but keep the polling loop from becoming a hot retry loop.
        return max(MIN_RETRY_DELAY_SECONDS, retry_after)
    return min(
        MAX_RETRY_BACKOFF_SECONDS,
        max(MIN_RETRY_DELAY_SECONDS, float(2 ** min(max(attempts, 0), 6))),
    )


def _validate_outbox_entries(raw: dict) -> bool:
    for message_key, entry in raw.items():
        if not isinstance(message_key, str) or not isinstance(entry, dict):
            return False
        request = entry.get("request")
        typed = request.get("typed") if isinstance(request, dict) else None
        if not isinstance(request, dict) or not isinstance(typed, dict):
            return False
        if typed.get("message_key") != message_key:
            return False
        attempts = entry.get("attempts", 0)
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
            return False
        next_attempt_at = entry.get("next_attempt_at")
        if next_attempt_at is not None and (
            isinstance(next_attempt_at, bool)
            or not isinstance(next_attempt_at, (int, float))
            or not math.isfinite(next_attempt_at)
            or next_attempt_at < 0
        ):
            return False
        if _contains_forbidden_outbox_data(entry):
            return False
    return True


def _load_outbox_file(path: Path, label: str) -> dict[str, dict]:
    try:
        if path.stat().st_size > MAX_OUTBOX_BYTES:
            _quarantine_file(path, f"{label}-oversized")
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
        _quarantine_file(path, label)
        return {}
    if not isinstance(raw, dict) or len(raw) > MAX_OUTBOX_ENTRIES or not _validate_outbox_entries(raw):
        _quarantine_file(path, label)
        return {}
    return raw


def _load_typed_outbox() -> dict[str, dict]:
    return _load_outbox_file(OUTBOX_FILE, "corrupt")


def _load_failed_outbox() -> dict[str, dict]:
    return _load_outbox_file(_failed_outbox_file(), "failed-corrupt")


def _restrict_file_permissions(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        # Windows and some mounted filesystems do not expose POSIX modes.
        pass


def _serialize_outbox(outbox: dict[str, dict]) -> str:
    if not isinstance(outbox, dict) or not _validate_outbox_entries(outbox):
        raise OutboxSecurityError("typed outbox schema is invalid")
    if len(outbox) > MAX_OUTBOX_ENTRIES:
        raise OutboxLimitError(
            f"typed outbox cannot contain more than {MAX_OUTBOX_ENTRIES} entries"
        )
    if _contains_forbidden_outbox_data(outbox):
        raise OutboxSecurityError("typed outbox cannot contain authentication secret fields")
    serialized = json.dumps(outbox, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > MAX_OUTBOX_BYTES:
        raise OutboxLimitError(
            f"typed outbox cannot exceed {MAX_OUTBOX_BYTES} UTF-8 bytes"
        )
    return serialized


def _save_typed_outbox(outbox: dict[str, dict]) -> None:
    serialized = _serialize_outbox(outbox)
    OUTBOX_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = OUTBOX_FILE.with_name(f"{OUTBOX_FILE.name}.tmp")
    temporary_file.write_text(serialized, encoding="utf-8")
    _restrict_file_permissions(temporary_file)
    temporary_file.replace(OUTBOX_FILE)
    _restrict_file_permissions(OUTBOX_FILE)


def _save_failed_outbox(outbox: dict[str, dict]) -> None:
    serialized = _serialize_outbox(outbox)
    failed_file = _failed_outbox_file()
    failed_file.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = failed_file.with_name(f"{failed_file.name}.tmp")
    temporary_file.write_text(serialized, encoding="utf-8")
    _restrict_file_permissions(temporary_file)
    temporary_file.replace(failed_file)
    _restrict_file_permissions(failed_file)


def _new_outbox_entry(request: dict) -> dict:
    return {
        "request": request,
        "attempts": 0,
        "created_at": time.time(),
        "last_attempt_at": None,
        "next_attempt_at": 0.0,
        "last_error": None,
    }


def _quarantine_outbox_entry(
    outbox: dict[str, dict], message_key: str, entry: dict, reason: str
) -> None:
    failed = _load_failed_outbox()
    failed_entry = dict(entry)
    failed_entry["last_error"] = reason
    failed_entry["failed_at"] = time.time()
    failed[message_key] = failed_entry
    _save_failed_outbox(failed)
    outbox.pop(message_key, None)
    _save_typed_outbox(outbox)
    print(f"[outbox] permanently failed key {message_key}: {reason}")


def _record_outbox_error(
    outbox: dict[str, dict], message_key: str, error: str, *, next_attempt_at: float | None = None
) -> None:
    entry = outbox[message_key]
    entry["last_error"] = error
    if next_attempt_at is not None:
        entry["next_attempt_at"] = next_attempt_at
    _save_typed_outbox(outbox)


def _schedule_transient_retry(
    outbox: dict[str, dict], message_key: str, error: str, response=None
) -> float:
    entry = outbox[message_key]
    delay = _retry_delay(response, int(entry.get("attempts", 0)))
    next_attempt_at = time.time() + delay
    _record_outbox_error(
        outbox,
        message_key,
        error,
        next_attempt_at=next_attempt_at,
    )
    return next_attempt_at


def _deliver_outbox_entry(message_key: str, outbox: dict[str, dict]) -> dict:
    entry = outbox[message_key]
    request = entry["request"]
    entry["attempts"] = int(entry.get("attempts", 0)) + 1
    entry["last_attempt_at"] = time.time()
    entry["next_attempt_at"] = None
    entry["last_error"] = None
    _save_typed_outbox(outbox)

    try:
        response = requests.post(
            f"{BASE_URL}/api/messages",
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
            json=request,
            timeout=15,
        )
    except requests.RequestException as exc:
        _schedule_transient_retry(
            outbox,
            message_key,
            f"network failure: {exc.__class__.__name__}",
        )
        raise

    status_code = response.status_code
    if 200 <= status_code < 300:
        try:
            result = response.json()
        except (ValueError, requests.RequestException) as exc:
            _schedule_transient_retry(
                outbox,
                message_key,
                f"invalid success response: {exc.__class__.__name__}",
                response,
            )
            raise
        if not isinstance(result, dict):
            _schedule_transient_retry(
                outbox,
                message_key,
                "invalid success response: object expected",
                response,
            )
            raise ValueError("typed outbox success response must be a JSON object")
        outbox.pop(message_key, None)
        _save_typed_outbox(outbox)
        return result

    error = f"HTTP {status_code}"
    try:
        response_body = response.json()
    except (ValueError, requests.RequestException):
        response_body = None
    response_code = (
        response_body.get("error", {}).get("code")
        if isinstance(response_body, dict) and isinstance(response_body.get("error"), dict)
        else None
    )
    if response_code:
        error = f"{error}: {response_code}"
    classification = classify_delivery_status(status_code)
    if classification == "PERMANENT":
        _quarantine_outbox_entry(outbox, message_key, entry, error)
        raise requests.HTTPError(f"Permanent typed outbox delivery failure: {error}", response=response)

    _schedule_transient_retry(outbox, message_key, error, response)
    failure_class = "Transient" if classification == "TRANSIENT" else "Unknown"
    raise requests.HTTPError(f"{failure_class} typed outbox delivery failure: {error}", response=response)


def retry_pending_typed_messages() -> int:
    """Replay pending requests with their original typed message keys."""
    outbox = _load_typed_outbox()
    delivered = 0
    for message_key in list(outbox):
        next_attempt_at = outbox[message_key].get("next_attempt_at")
        if next_attempt_at is not None and next_attempt_at > time.time():
            continue
        try:
            _deliver_outbox_entry(message_key, outbox)
        except (requests.RequestException, OutboxLimitError, ValueError) as exc:
            print(f"[outbox] retry pending for {message_key}: {exc}")
        else:
            delivered += 1
    return delivered


def post_typed_message(
    content: str,
    kind: str,
    payload: dict,
    *,
    task_id: str | None = None,
    message_type: str | None = None,
    action_id: str | None = None,
    approval_id: str | None = None,
    next_action: str | None = None,
    reason_code: str | None = None,
    message_key: str | None = None,
    correlation_id: str | None = None,
    reply_to_id: int | None = None,
    expects_reply: bool | None = None,
    canonical_refs: list[dict] | None = None,
) -> dict:
    """Post a typed message and retain its key across uncertain retries."""
    if kind not in TYPED_MESSAGE_KINDS:
        raise ValueError(f"Unsupported typed message kind: {kind!r}")
    if not isinstance(payload, dict):
        raise ValueError("typed payload must be an object")
    if "kind" in payload and payload["kind"] != kind:
        raise ValueError("typed payload kind must match the envelope kind")
    if expects_reply is None:
        expects_reply = kind == "DECISION_REQUEST"

    stable_key = message_key or f"worker-{secrets.token_urlsafe(18)}"
    typed = {
        "schema_version": 1,
        "kind": kind,
        "message_key": stable_key,
        "correlation_id": correlation_id,
        "reply_to_id": reply_to_id,
        "expects_reply": expects_reply,
        "payload": {"kind": kind, **payload},
        "canonical_refs": canonical_refs or [],
    }
    body = {"content": content, "typed": typed}
    for key, value in (
        ("task_id", task_id),
        ("message_type", message_type),
        ("action_id", action_id),
        ("approval_id", approval_id),
        ("next_action", next_action),
        ("reason_code", reason_code),
    ):
        if value:
            body[key] = value

    outbox = _load_typed_outbox()
    if stable_key in outbox and outbox[stable_key].get("request") != body:
        raise OutboxKeyConflict(f"typed message key is already pending with different content: {stable_key}")
    outbox.setdefault(stable_key, _new_outbox_entry(body))
    _save_typed_outbox(outbox)
    return _deliver_outbox_entry(stable_key, outbox)


def post_status(
    content: str,
    task_id: str | None = None,
    message_type: str = "STATUS",
    action_id: str | None = None,
    approval_id: str | None = None,
    next_action: str | None = None,
    reason_code: str | None = None,
):
    payload: dict = {
        "content": content,
        "message_type": message_type,
    }
    for key, value in (
        ("task_id", task_id),
        ("action_id", action_id),
        ("approval_id", approval_id),
        ("next_action", next_action),
        ("reason_code", reason_code),
    ):
        if value:
            payload[key] = value

    r = requests.post(
        f"{BASE_URL}/api/messages",
        headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        json=payload,
        timeout=15,
    )
    r.raise_for_status()
    print("Status posted successfully.")


def fetch_messages_page(before_id: int | None = None, limit: int = LATEST_PAGE_SIZE) -> list[dict]:
    params: dict = {"limit": limit}
    if before_id is not None:
        params["before_id"] = before_id
    else:
        params["latest"] = "true"
    r = requests.get(
        f"{BASE_URL}/api/messages",
        params=params,
        headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def fetch_latest_messages(limit: int = LATEST_PAGE_SIZE) -> list[dict]:
    return fetch_messages_page(before_id=None, limit=limit)


def fetch_latest_message_id() -> int:
    messages = fetch_latest_messages(limit=1)
    return int(messages[-1]["id"]) if messages else 0


def fetch_latest_engineer_message(page_size: int = LATEST_PAGE_SIZE) -> dict | None:
    before_id = None
    while True:
        if before_id is None:
            messages = fetch_latest_messages(limit=page_size)
        else:
            messages = fetch_messages_page(before_id=before_id, limit=page_size)

        if not messages:
            return None

        for message in reversed(messages):
            if message.get("role") == "engineer":
                return message

        if len(messages) < page_size:
            return None

        before_id = int(messages[0]["id"])


def load_last_seen() -> int:
    try:
        return int(STATE_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0


def save_last_seen(message_id: int):
    temporary_file = STATE_FILE.with_name(f"{STATE_FILE.name}.tmp")
    temporary_file.write_text(str(message_id), encoding="utf-8")
    temporary_file.replace(STATE_FILE)


def reports_commit_unknown_control_event(message: dict) -> bool:
    """Detect an explicit Bridge-reported reconciliation event.

    The Bridge transports coordination metadata; it must not infer canonical
    ForgeLoop state from free-form Markdown content.
    """
    return (
        message.get("message_type") == "ACTION_RECONCILIATION_REQUIRED"
        or message.get("next_action") == "RECONCILE_ACTION"
        or message.get("reason_code") in COMMIT_UNKNOWN_REASON_CODES
    )


def reports_verification_isolation_block(message: dict) -> bool:
    """Detect an explicit canonical verification-isolation blocker."""
    return message.get("reason_code") in VERIFICATION_ISOLATION_REASON_CODES


def print_control_event(message: dict):
    """Print copied canonical guidance for the human/operator-facing worker log."""
    fields = {
        "Task": message.get("task_id"),
        "Type": message.get("message_type"),
        "Action": message.get("action_id"),
        "Approval": message.get("approval_id"),
        "Next": message.get("next_action"),
        "Reason": message.get("reason_code"),
    }
    if any(value for value in fields.values()):
        print("FORGELOOP CONTROL EVENT")
        for label, value in fields.items():
            if value:
                print(f"{label}: {value}")
        print("This is a coordination event, not ForgeLoop authority.")

    if reports_commit_unknown_control_event(message):
        print("HARD STOP: COMMIT_UNKNOWN; do not retry this action.")
        print("Record external observation through canonical ForgeLoop reconciliation.")

    if reports_verification_isolation_block(message):
        print("HARD STOP: verification isolation is unavailable or invalid.")
        print("Do not downgrade the required isolation or fabricate execution evidence.")
        print("Follow canonical ForgeLoop next/recovery guidance.")

    boundary = classify_reason_code(message)
    if boundary in {"workspace", "responsibility", "verification_scope", "handoff"}:
        print(f"HARD STOP: canonical {boundary} boundary reported by ForgeLoop.")
        print("Preserve the exact reason_code and follow canonical ForgeLoop next guidance.")
    elif boundary in {"attestation", "revision_provider"}:
        print(f"CANONICAL {boundary.upper()} STATUS: report the exact reason_code.")
        print("Do not promote trust or add Bridge-side recovery behavior.")

    typed = message.get("typed")
    if isinstance(typed, dict) and typed.get("kind") == "ATTESTATION_REPORT":
        print("ATTESTATION_REPORT is a copied canonical projection, not independent proof.")


def handoff_message(message: dict, auto_ack: bool = False) -> None:
    """Display and optionally acknowledge one Engineer instruction."""
    typed_kind = dispatch_typed_message(message)
    task_id = message.get("task_id")
    msg_type = message.get("message_type")
    context = read_forgeloop_context(task_id)

    print("\n" + "=" * 60)
    print("NEW INSTRUCTION FROM ENGINEER")
    if task_id:
        print(f"Task: {task_id}")
    if msg_type:
        print(f"Type: {msg_type}")
    if typed_kind:
        print(f"Typed kind: {typed_kind}")
    print("=" * 60)
    print(message["content"])
    print("=" * 60 + "\n")
    print_control_event(message)
    if context is not None:
        print("FORGELOOP TASK/CONTEXT PROJECTION")
        print(json.dumps(context, sort_keys=True))
        if context.get("status") == "UNAVAILABLE":
            print("HARD STOP: canonical task/context is unavailable; do not infer a profile.")
        elif context.get("status") == "COMPATIBILITY_FALLBACK":
            print("Compatibility mode: using the explicit balanced ForgeLoop fallback.")
        else:
            resolved = context.get("execution_profile", {}).get("resolved")
            print(f"Canonical resolved execution profile: {resolved}")

    # ────────────────────────────────────────────────────────────────
    # HERE you hand off the instruction to your Worker agent/harness
    # (OpenCode, Cursor, script, etc.). The agent follows advertised
    # ForgeLoop protocol operations.
    # ────────────────────────────────────────────────────────────────

    if auto_ack:
        ack_content = (
            "### Receipt\n"
            f"Source message type: `{msg_type or 'unspecified'}`\n\n"
            "Received coordination event; processing with the advertised "
            "ForgeLoop protocol/capabilities.\n\n"
            "This acknowledgement is informational only and does not grant "
            "approval or host authority."
        )
        if reports_commit_unknown_control_event(message):
            ack_content += (
                "\n\n**Hard stop:** `COMMIT_UNKNOWN` is unresolved. Do not retry; "
                "follow canonical action reconciliation."
            )
        if reports_verification_isolation_block(message):
            ack_content += (
                "\n\n**Hard stop:** canonical verification isolation is unavailable "
                "or invalid. No weaker-isolation retry or synthetic evidence will "
                "be attempted."
            )
        context_payload = build_context_status_payload(context)
        typed = message.get("typed")
        if isinstance(typed, dict):
            post_typed_message(
                content=ack_content,
                kind="STATUS_UPDATE",
                payload={
                    "state": "RECEIVED",
                    "summary": "Coordination event received; canonical processing is in progress.",
                    **context_payload,
                },
                task_id=task_id,
                message_type="STATUS",
                action_id=message.get("action_id"),
                approval_id=message.get("approval_id"),
                next_action=message.get("next_action"),
                reason_code=message.get("reason_code"),
                correlation_id=typed.get("correlation_id"),
                reply_to_id=message.get("id"),
            )
        else:
            post_status(
                content=ack_content,
                task_id=task_id,
                message_type="STATUS",
                action_id=message.get("action_id"),
                approval_id=message.get("approval_id"),
                next_action=message.get("next_action"),
                reason_code=message.get("reason_code"),
            )


def process_polled_messages(messages: list[dict], last_seen: int, auto_ack: bool = False) -> int:
    """Handle a batch and persist each cursor only after safe handling.

    Engineer instructions are intentionally at-least-once: a failure during
    handoff or acknowledgement leaves that message eligible for redelivery.
    """
    for message in messages:
        message_id = int(message["id"])

        if message["role"] != "engineer":
            last_seen = max(last_seen, message_id)
            save_last_seen(last_seen)
            continue

        handoff_message(message, auto_ack=auto_ack)
        last_seen = max(last_seen, message_id)
        save_last_seen(last_seen)

    return last_seen


def initialize_first_start_cycle(start_mode: str, auto_ack: bool = False) -> tuple[int, int]:
    """Initialize a missing cursor and report (cursor, instructions_handed_off).

    A fresh Worker turn has no cursor file, so the `pending` bootstrap is the
    step that delivers the currently open Engineer instruction. Returning that
    count lets a bounded turn report the work it actually performed instead of
    looking idle to the launching orchestrator.
    """
    if start_mode not in START_MODES:
        raise ValueError(f"Unsupported start mode: {start_mode}")
    if start_mode == "history":
        return 0, 0

    if start_mode == "now":
        last_seen = fetch_latest_message_id()
        save_last_seen(last_seen)
        return last_seen, 0

    latest_engineer = fetch_latest_engineer_message()
    if latest_engineer is not None:
        last_seen = process_polled_messages([latest_engineer], last_seen=0, auto_ack=auto_ack)
        return last_seen, 1

    last_seen = fetch_latest_message_id()
    save_last_seen(last_seen)
    return last_seen, 0


def initialize_first_start(start_mode: str, auto_ack: bool = False) -> int:
    """Initialize a missing cursor without silently losing the current task."""
    last_seen, _handled = initialize_first_start_cycle(start_mode, auto_ack=auto_ack)
    return last_seen


def poll_once(
    last_seen: int,
    *,
    auto_ack: bool = False,
) -> tuple[int, int]:
    """Poll once and return (new_last_seen, engineer_messages_handled).

    One cycle never sleeps and never waits for future coordination: it replays
    the typed outbox, reads the Bridge delta after the persisted cursor, and
    hands off the Engineer instructions that are actionable right now. The
    returned count reports only Engineer-authored instructions that were safely
    handed off during this cycle, so an unsafe batch raises instead of reporting
    progress.
    """
    retry_pending_typed_messages()

    response = requests.get(
        f"{BASE_URL}/api/messages",
        params={"after_id": last_seen},
        headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        timeout=15,
    )
    response.raise_for_status()
    messages = response.json()

    new_last_seen = process_polled_messages(
        messages,
        last_seen,
        auto_ack=auto_ack,
    )
    engineer_count = sum(1 for message in messages if message.get("role") == "engineer")
    return new_last_seen, engineer_count


def print_exit_reason(reason: str, last_seen: int, handled: int) -> None:
    """Print a stable bounded-exit marker for the launching agent/orchestrator.

    The marker only reports Bridge transport coordination: it never claims
    canonical ForgeLoop completion and never includes authentication material.
    """
    print(f"WORKER_POLL_EXIT reason={reason} last_seen={last_seen} handled={handled}")


def print_exit_error(exception: BaseException) -> None:
    """Print a stable bounded-failure marker without leaking credentials."""
    print(f"[error] {exception}")
    print(f"WORKER_POLL_ERROR type={exception.__class__.__name__}")


def main() -> int:
    parser = argparse.ArgumentParser(description="ForgeLoopBridge worker poller")
    parser.add_argument(
        "--auto-ack",
        action="store_true",
        help="post an immediate receipt/status acknowledgement per instruction",
    )
    parser.add_argument(
        "--start-mode",
        choices=START_MODES,
        default="pending",
        help="first-start policy: deliver latest Engineer instruction, skip board, or replay history",
    )
    parser.add_argument(
        "--run-mode",
        choices=RUN_MODES,
        default="daemon",
        help=(
            "daemon polls continuously; once processes the currently available "
            "Bridge delta and exits; bounded adds a finite idle grace window "
            "before exiting"
        ),
    )
    parser.add_argument(
        "--max-idle-polls",
        type=int,
        default=DEFAULT_MAX_IDLE_POLLS,
        help=(
            "bounded run mode only: exit after this many consecutive polls that "
            "deliver no new Engineer instruction"
        ),
    )
    args = parser.parse_args()
    if args.run_mode == "bounded" and args.max_idle_polls < 1:
        parser.error("--max-idle-polls must be at least 1 for the bounded run mode")

    last_seen = load_last_seen()
    retry_pending_typed_messages()
    bootstrap_handled = 0
    if not STATE_FILE.exists():
        last_seen, bootstrap_handled = initialize_first_start_cycle(
            args.start_mode, auto_ack=args.auto_ack
        )
        print(f"First run ({args.start_mode}): starting from message id {last_seen}")
    else:
        print(f"Resuming from message id {last_seen}")

    if args.run_mode == "once":
        print(f"Bounded single cycle against {BASE_URL} ...")
        try:
            last_seen, handled = poll_once(last_seen, auto_ack=args.auto_ack)
        except Exception as exc:
            print_exit_error(exc)
            return 1

        print_exit_reason("ONE_SHOT_COMPLETE", last_seen, bootstrap_handled + handled)
        return 0

    if args.run_mode == "bounded":
        print(
            f"Bounded turn against {BASE_URL}: exiting after "
            f"{args.max_idle_polls} consecutive idle poll(s) ..."
        )
        handled_total = bootstrap_handled
        idle_polls = 0
        while idle_polls < args.max_idle_polls:
            try:
                last_seen, handled = poll_once(last_seen, auto_ack=args.auto_ack)
            except Exception as exc:
                print_exit_error(exc)
                return 1

            if handled > 0:
                # New Engineer input is real progress, so the grace window restarts.
                handled_total += handled
                idle_polls = 0
                continue

            idle_polls += 1
            if idle_polls < args.max_idle_polls:
                time.sleep(POLL_INTERVAL)

        print_exit_reason("IDLE_BOUND_REACHED", last_seen, handled_total)
        return 0

    print(f"Monitoring {BASE_URL} every {POLL_INTERVAL}s ...")

    while True:
        try:
            last_seen, _handled = poll_once(last_seen, auto_ack=args.auto_ack)
        except Exception as e:
            print(f"[error] {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    raise SystemExit(main())
