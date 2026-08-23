#!/usr/bin/env python3
"""
Minimal example of how the Worker can monitor ForgeLoopBridge.
This script is a transport adapter. When an instruction is received, invoke
your Worker agent/harness (e.g. OpenCode, Cursor, or local agent).

The Worker agent MUST follow the canonical ForgeLoop 1.5 workflow:
1. Prefer official ForgeLoop structured integration when exposed by the host;
   otherwise use the project-local ForgeLoop CLI.
2. Inspect compatibility with `forgeloop protocol-info --json`.
3. Discover existing tasks (`forgeloop task-list --json`) before creating a new one.
4. Follow canonical `forgeloop next` as the control authority.
5. Reach VALID completion (`forgeloop complete --task <task-id> --json`) and verify terminal `nextAction: NONE`.
6. Open PR and report structured Markdown status on ForgeLoopBridge.

Usage:
    python worker_poll.py [--auto-ack]

`--auto-ack` posts an immediate "processing..." status for each new
instruction. Disabled by default to keep the board clean.
"""

import argparse
import time
from pathlib import Path

import requests

BASE_URL = "http://localhost:8000"          # change to your ForgeLoopBridge URL
WORKER_TOKEN = "change-me"                  # same token configured on the server
POLL_INTERVAL = 10                          # seconds
STATE_FILE = Path(__file__).parent / ".worker_last_seen"  # survives restarts


def post_status(content: str, task_id: str | None = None, message_type: str = "STATUS"):
    payload: dict = {
        "token": WORKER_TOKEN,
        "content": content,
        "message_type": message_type,
    }
    if task_id:
        payload["task_id"] = task_id

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


def main():
    parser = argparse.ArgumentParser(description="ForgeLoopBridge worker poller")
    parser.add_argument(
        "--auto-ack",
        action="store_true",
        help="post an immediate 'processing...' status per instruction",
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

                # ────────────────────────────────────────────────
                # HERE you hand off the instruction to your Worker
                # agent/harness (OpenCode, Cursor, script, etc.).
                # The agent follows ForgeLoop 1.5 protocol operations.
                # ────────────────────────────────────────────────

                if args.auto_ack:
                    post_status(
                        content=(
                            "### Status\n"
                            "Received instruction and processing with ForgeLoop...\n\n"
                            "*(replace this message with the real result + PR link)*"
                        ),
                        task_id=task_id,
                        message_type="STATUS",
                    )

        except Exception as e:
            print(f"[error] {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()

