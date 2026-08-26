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
   never infer durable actions or approvals from a package version alone.
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
    python worker_poll.py [--auto-ack]

`--auto-ack` posts an immediate receipt/status acknowledgement for each new
instruction. It never resolves approvals, grants authority, or changes
ForgeLoop state. Disabled by default to keep the board clean.
"""

import argparse
import time
from pathlib import Path

import requests

BASE_URL = "http://localhost:8000"          # change to your ForgeLoopBridge URL
WORKER_TOKEN = "change-me"                  # same token configured on the server
POLL_INTERVAL = 10                          # seconds
STATE_FILE = Path(__file__).parent / ".worker_last_seen"  # survives restarts


FORGELOOP_BOOTSTRAP = """
Before creating or resuming protocol state, inspect:
  forgeloop protocol-info --json
Then use the advertised feature set for diagnostics, durableActions,
capabilityPolicy, and durableApprovals. Query `forgeloop next --task
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


def fetch_latest_message_id() -> int:
    r = requests.get(
        f"{BASE_URL}/api/messages",
        params={"latest": "true", "limit": 1},
        headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        timeout=15,
    )
    r.raise_for_status()
    messages = r.json()
    return int(messages[-1]["id"]) if messages else 0


def load_last_seen() -> int:
    try:
        return int(STATE_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0


def save_last_seen(message_id: int):
    STATE_FILE.write_text(str(message_id))


def is_commit_unknown(message: dict) -> bool:
    """Detect a coordination hard-stop signal without interpreting protocol state."""
    fields = (
        message.get("content", ""),
        message.get("next_action", ""),
        message.get("reason_code", ""),
    )
    return any("COMMIT_UNKNOWN" in str(field).upper() for field in fields)


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

    if is_commit_unknown(message):
        print("HARD STOP: COMMIT_UNKNOWN; do not retry this action.")
        print("Record external observation through canonical ForgeLoop reconciliation.")


def main():
    parser = argparse.ArgumentParser(description="ForgeLoopBridge worker poller")
    parser.add_argument(
        "--auto-ack",
        action="store_true",
        help="post an immediate receipt/status acknowledgement per instruction",
    )
    args = parser.parse_args()

    last_seen = load_last_seen()
    if last_seen == 0:
        # First run: skip history, start from the latest message.
        last_seen = fetch_latest_message_id()
        save_last_seen(last_seen)
        print(f"First run: starting from message id {last_seen}")
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

            for msg in messages:
                msg_id = int(msg["id"])
                last_seen = max(last_seen, msg_id)
                save_last_seen(last_seen)

                if msg["role"] != "engineer":
                    continue

                task_id = msg.get("task_id")
                msg_type = msg.get("message_type")

                print("\n" + "=" * 60)
                print("NEW INSTRUCTION FROM ENGINEER")
                if task_id:
                    print(f"Task: {task_id}")
                if msg_type:
                    print(f"Type: {msg_type}")
                print("=" * 60)
                print(msg["content"])
                print("=" * 60 + "\n")
                print_control_event(msg)

                # ────────────────────────────────────────────────
                # HERE you hand off the instruction to your Worker
                # agent/harness (OpenCode, Cursor, script, etc.).
                # The agent follows advertised ForgeLoop protocol operations.
                # ────────────────────────────────────────────────

                if args.auto_ack:
                    ack_content = (
                        "### Receipt\n"
                        f"Source message type: `{msg_type or 'unspecified'}`\n\n"
                        "Received coordination event; processing with the advertised "
                        "ForgeLoop protocol/capabilities.\n\n"
                        "This acknowledgement is informational only and does not grant "
                        "approval or host authority."
                    )
                    if is_commit_unknown(msg):
                        ack_content += (
                            "\n\n**Hard stop:** `COMMIT_UNKNOWN` is unresolved. Do not retry; "
                            "follow canonical action reconciliation."
                        )
                    post_status(
                        content=ack_content,
                        task_id=task_id,
                        message_type="STATUS",
                        action_id=msg.get("action_id"),
                        approval_id=msg.get("approval_id"),
                        next_action=msg.get("next_action"),
                        reason_code=msg.get("reason_code"),
                    )

        except Exception as e:
            print(f"[error] {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
