#!/usr/bin/env python3
"""
Minimal example of how the Worker can monitor ForgeLoopBridge.
Replace the "execute task" logic with your agent (OpenCode, etc.).

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


def post_status(content: str):
    r = requests.post(
        f"{BASE_URL}/api/messages",
        headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        json={"token": WORKER_TOKEN, "content": content},
        timeout=15,
    )
    r.raise_for_status()
    print("Status posted successfully.")


def load_last_seen() -> float:
    try:
        return float(STATE_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0.0


def save_last_seen(ts: float):
    STATE_FILE.write_text(str(ts))


def main():
    parser = argparse.ArgumentParser(description="ForgeLoopBridge worker poller")
    parser.add_argument("--auto-ack", action="store_true",
                        help="post an immediate 'processing...' status per instruction")
    args = parser.parse_args()

    last_seen = load_last_seen()
    if last_seen == 0.0:
        # First run: skip history, only process messages from now on.
        r = requests.get(
            f"{BASE_URL}/api/messages",
            params={"limit": 1},
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
            timeout=15,
        )
        r.raise_for_status()
        msgs = r.json()
        last_seen = msgs[-1]["id"] if msgs else 0
        print(f"First run: starting from message id {last_seen}")
    else:
        print(f"Resuming from message id {last_seen}")

    print(f"Monitoring {BASE_URL} every {POLL_INTERVAL}s ...")

    while True:
        try:
            r = requests.get(
                f"{BASE_URL}/api/messages",
                params={"after_id": int(last_seen)},
                headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
                timeout=15,
            )
            r.raise_for_status()
            messages = r.json()

            for msg in messages:
                last_seen = max(last_seen, msg["id"])
                save_last_seen(last_seen)

                if msg["role"] != "engineer":
                    continue

                print("\n" + "=" * 60)
                print("NEW INSTRUCTION FROM ENGINEER")
                print("=" * 60)
                print(msg["content"])
                print("=" * 60 + "\n")

                # ────────────────────────────────────────────────
                # HERE you call your agent / OpenCode / script
                # Example:
                #   result = run_opencode(msg["content"])
                #   pr_url = open_pull_request(...)
                # ────────────────────────────────────────────────

                if args.auto_ack:
                    post_status(
                        "### Status\n"
                        "Received the instruction and processing...\n\n"
                        "*(replace this message with the real result + PR link)*"
                    )

        except Exception as e:
            print(f"[error] {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
