#!/usr/bin/env python3
"""
Minimal example of how the Worker can monitor ForgeLoopBridge.
This script is a transport adapter. When an instruction is received, invoke
your Worker agent/harness (e.g. OpenCode, Cursor, or local agent).

The Worker agent MUST use the current advertised ForgeLoop protocol/capability
workflow:
1. Prefer official ForgeLoop structured integration when exposed by the host;
   otherwise use the project-local ForgeLoop CLI.
2. Inspect `forgeloop protocol-info --json` and feature-detect capabilities;
   include `verificationExecutionIsolation`; never infer capabilities from a
   package version alone or self-attest a trusted isolation boundary.
3. Discover existing tasks (`forgeloop task-list --json`) before creating a new one.
4. Treat canonical `forgeloop next` as the dispatcher after every meaningful
   protocol mutation. The example lifecycle is a happy-path illustration only.
5. Respect canonical action, approval, policy, diagnostic, and reconciliation
   guidance. `COMMIT_UNKNOWN` is a hard stop: do not retry the action.
6. Reach VALID completion with `forgeloop complete --task <task-id> --json` and
   verify terminal `nextAction: NONE` before posting a COMPLETE status;
   otherwise report the exact blocked/partial state.
7. Open PR and report structured Markdown status on ForgeLoopBridge.

Usage:
    python worker_poll.py [--auto-ack] [--start-mode pending|now|history]

`--auto-ack` posts an immediate receipt/status acknowledgement for each new
instruction. It never resolves approvals, grants authority, or changes
ForgeLoop state. Disabled by default to keep the board clean.

`--start-mode pending` is the default and hands off the latest existing
Engineer instruction on a first start. Use `--start-mode now` explicitly to
ignore messages already on the board, or `--start-mode history` to replay from
cursor zero.
"""

import argparse
import json
import secrets
import time
from pathlib import Path

import requests

BASE_URL = "http://localhost:8000"          # change to your ForgeLoopBridge URL
WORKER_TOKEN = "change-me"                  # same token configured on the server
POLL_INTERVAL = 10                          # seconds
STATE_FILE = Path(__file__).parent / ".worker_last_seen"  # survives restarts
OUTBOX_FILE = Path(__file__).parent / ".worker_typed_outbox.json"  # survives uncertain POSTs
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
LATEST_PAGE_SIZE = 200


FORGELOOP_BOOTSTRAP = """
Before creating or resuming protocol state, inspect:
  forgeloop protocol-info --json
Then use the advertised feature set for diagnostics, durableActions,
capabilityPolicy, durableApprovals, verificationExecutionIsolation,
workspaceBinding, canonicalHandoffs, responsibilityConstraints,
differentialVerificationScope, and codeAttestation. Package version is
informational only; feature support must be advertised by the canonical
protocol-info or structured integration result.

When workspaceBinding is supported, use only canonical workspace-bind and
workspace-status operations. A path, cwd, branch, copied checkout, or Bridge
message is not workspace identity proof. When canonicalHandoffs is supported,
use handoff-create/list/show for continuity; a handoff is not delegation,
identity, approval, completion, or verification evidence. When
responsibilityConstraints is supported, use responsibility-set/status and stop
on canonical scope or frozen-input violations. Never infer responsibility from
Markdown.

When differentialVerificationScope is supported, ask ForgeLoop for verify-scope
with AUTO and use its returned scope only through the trusted scoped checker.
AUTO falls back to FULL without that capability; explicit CHANGED/CLAIMED must
fail closed. Never calculate changed, claimed, or impacted paths locally.

When codeAttestation is supported or required, use the canonical attestation
commands and canonical revision-provider results. Distinguish NOT_VERIFIED,
VERIFIED, and ATTESTED; an external validated signature is required for
ATTESTED. Never self-sign, promote trust, or store signing credentials in the
Bridge.

Query `forgeloop next --task <task-id> --json` after every meaningful mutation;
its nextAction, reasonCodes, authorityRequired, approvalRequired,
capabilityDecision, hostActionRequired, and reconciliationAuthorityRequired
fields take precedence over examples.
""".strip()


class UnsupportedTypedMessageVersion(ValueError):
    """Raised when a Worker cannot safely interpret a typed message schema."""


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
        "CONTROL_NOTICE": handle_control_notice,
        "REVIEW_RESULT": handle_review_result,
    }
    handler = handlers.get(kind, handle_unknown_typed_message)
    handler(message)
    return kind


def _load_typed_outbox() -> dict[str, dict]:
    try:
        raw = json.loads(OUTBOX_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_typed_outbox(outbox: dict[str, dict]) -> None:
    OUTBOX_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = OUTBOX_FILE.with_name(f"{OUTBOX_FILE.name}.tmp")
    temporary_file.write_text(json.dumps(outbox, sort_keys=True), encoding="utf-8")
    temporary_file.replace(OUTBOX_FILE)


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
    expects_reply: bool = False,
    canonical_refs: list[dict] | None = None,
) -> dict:
    """Post a typed message and retain its key across uncertain retries."""
    if kind not in TYPED_MESSAGE_KINDS:
        raise ValueError(f"Unsupported typed message kind: {kind!r}")
    if not isinstance(payload, dict):
        raise ValueError("typed payload must be an object")
    if "kind" in payload and payload["kind"] != kind:
        raise ValueError("typed payload kind must match the envelope kind")

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
    body = {"token": WORKER_TOKEN, "content": content, "typed": typed}
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
    outbox[stable_key] = body
    _save_typed_outbox(outbox)
    response = requests.post(
        f"{BASE_URL}/api/messages",
        headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        json=body,
        timeout=15,
    )
    response.raise_for_status()
    result = response.json()
    outbox.pop(stable_key, None)
    _save_typed_outbox(outbox)
    return result


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
        "token": WORKER_TOKEN,
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
        typed = message.get("typed")
        if isinstance(typed, dict):
            post_typed_message(
                content=ack_content,
                kind="STATUS_UPDATE",
                payload={
                    "state": "RECEIVED",
                    "summary": "Coordination event received; canonical processing is in progress.",
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


def initialize_first_start(start_mode: str, auto_ack: bool = False) -> int:
    """Initialize a missing cursor without silently losing the current task."""
    if start_mode not in START_MODES:
        raise ValueError(f"Unsupported start mode: {start_mode}")
    if start_mode == "history":
        return 0

    if start_mode == "now":
        last_seen = fetch_latest_message_id()
        save_last_seen(last_seen)
        return last_seen

    latest_engineer = fetch_latest_engineer_message()
    if latest_engineer is not None:
        return process_polled_messages([latest_engineer], last_seen=0, auto_ack=auto_ack)

    last_seen = fetch_latest_message_id()
    save_last_seen(last_seen)
    return last_seen


def main():
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
    args = parser.parse_args()

    last_seen = load_last_seen()
    if not STATE_FILE.exists():
        last_seen = initialize_first_start(args.start_mode, auto_ack=args.auto_ack)
        print(f"First run ({args.start_mode}): starting from message id {last_seen}")
    else:
        print(f"Resuming from message id {last_seen}")

    print(f"Monitoring {BASE_URL} every {POLL_INTERVAL}s ...")

    while True:
        try:
            r = requests.get(
                f"{BASE_URL}/api/messages",
                params={"after_id": last_seen},
                headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
                timeout=15,
            )
            r.raise_for_status()
            messages = r.json()

            last_seen = process_polled_messages(messages, last_seen, auto_ack=args.auto_ack)

        except Exception as e:
            print(f"[error] {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
