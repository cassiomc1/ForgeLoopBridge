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
import time
from pathlib import Path

import requests

BASE_URL = "http://localhost:8000"          # change to your ForgeLoopBridge URL
WORKER_TOKEN = "change-me"                  # same token configured on the server
POLL_INTERVAL = 10                          # seconds
STATE_FILE = Path(__file__).parent / ".worker_last_seen"  # survives restarts
COMMIT_UNKNOWN_REASON_CODES = frozenset({"COMMIT_UNKNOWN", "E_ACTION_COMMIT_UNKNOWN"})
VERIFICATION_ISOLATION_REASON_CODES = frozenset(
    {
        "E_VERIFICATION_ISOLATION_UNAVAILABLE",
        "E_VERIFICATION_EXECUTION_INVALID",
    }
)
START_MODES = ("pending", "now", "history")
LATEST_PAGE_SIZE = 200


FORGELOOP_BOOTSTRAP = """
Before creating or resuming protocol state, inspect:
  forgeloop protocol-info --json
Then use the advertised feature set for diagnostics, durableActions,
capabilityPolicy, durableApprovals, and verificationExecutionIsolation. For
verification, trust only canonical ForgeLoop execution-adapter results; never
infer isolation from cwd/path layout or Bridge metadata. Query `forgeloop next --task
<task-id> --json` after every meaningful mutation; its nextAction, reasonCodes,
authorityRequired, approvalRequired, capabilityDecision, hostActionRequired,
and reconciliationAuthorityRequired fields take precedence over examples.
""".strip()


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


def handoff_message(message: dict, auto_ack: bool = False) -> None:
    """Display and optionally acknowledge one Engineer instruction."""
    task_id = message.get("task_id")
    msg_type = message.get("message_type")

    print("\n" + "=" * 60)
    print("NEW INSTRUCTION FROM ENGINEER")
    if task_id:
        print(f"Task: {task_id}")
    if msg_type:
        print(f"Type: {msg_type}")
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
