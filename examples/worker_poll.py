#!/usr/bin/env python3
"""
Minimal example of how the Worker can monitor ForgeLoopBridge.
Replace the "execute task" logic with your agent (OpenCode, etc.).
"""

import time
import requests

BASE_URL = "http://localhost:8000"          # change to your ForgeLoopBridge URL
WORKER_TOKEN = "worker_secret"              # same token configured on the server
POLL_INTERVAL = 10                          # seconds


def post_status(content: str):
    r = requests.post(
        f"{BASE_URL}/api/messages",
        json={"token": WORKER_TOKEN, "content": content},
        timeout=15,
    )
    r.raise_for_status()
    print("Status posted successfully.")


def main():
    last_seen = 0.0
    print(f"Monitoring {BASE_URL} every {POLL_INTERVAL}s ...")

    while True:
        try:
            r = requests.get(
                f"{BASE_URL}/api/messages",
                params={"since": last_seen},
                timeout=15,
            )
            r.raise_for_status()
            messages = r.json()

            for msg in messages:
                last_seen = max(last_seen, msg["created_at"])

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

                # After finishing, post the status:
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
