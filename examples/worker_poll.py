#!/usr/bin/env python3
"""Master-Agent Bridge — Agent Worker Poller.

A clean, reliable poller for the Agent to receive orders from the Master,
execute them, and report execution results back to the Bridge.

Features:
- Polls for new orders from the Master (`role == 'master'`).
- Dispatches orders to local execution.
- Reports status and results back (`role == 'agent'`).
- Outbox retry with idempotency key (`message_key`) to prevent duplicate executions.
- Run modes:
  - `daemon`: Continuously polls for new orders.
  - `once`: Runs a single poll cycle and exits.
  - `bounded`: Exits after N consecutive idle polls.
- Start modes:
  - `pending`: Starts from the last unhandled Master order.
  - `now`: Ignores historical messages and only listens for future orders.
  - `history`: Replays from the beginning.
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import sys
import time
from typing import Any

import requests

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [agent-worker]: %(message)s",
)
logger = logging.getLogger("agent-worker")

DEFAULT_BRIDGE_URL = os.getenv("BRIDGE_URL", "http://localhost:8000")
AGENT_TOKEN = os.getenv("AGENT_TOKEN") or os.getenv("WORKER_TOKEN", "")


def get_auth_headers(token: str | None = None) -> dict[str, str]:
    resolved_token = token or AGENT_TOKEN
    if not resolved_token:
        raise RuntimeError(
            "AGENT_TOKEN (or WORKER_TOKEN) must be set in environment or passed via --token"
        )
    return {
        "Authorization": f"Bearer {resolved_token}",
        "Content-Type": "application/json",
    }


def fetch_latest_message_id(bridge_url: str = DEFAULT_BRIDGE_URL, headers: dict[str, str] | None = None) -> int:
    """Fetch the highest message id currently recorded on the bridge."""
    hdrs = headers if headers is not None else get_auth_headers()
    try:
        resp = requests.get(
            f"{bridge_url}/api/messages",
            params={"latest": "true", "limit": 1},
            headers=hdrs,
            timeout=10,
        )
        resp.raise_for_status()
        messages = resp.json()
        return messages[-1]["id"] if messages else 0
    except Exception as exc:
        logger.warning("Could not fetch latest message id: %s", exc)
        return 0


def fetch_messages(
    after_id: int,
    bridge_url: str = DEFAULT_BRIDGE_URL,
    headers: dict[str, str] | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Poll messages from the bridge with id > after_id."""
    hdrs = headers if headers is not None else get_auth_headers()
    resp = requests.get(
        f"{bridge_url}/api/messages",
        params={"after_id": after_id, "limit": limit},
        headers=hdrs,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def post_execution_report(
    content: str,
    order_id: str | None = None,
    status: str = "COMPLETED",
    message_type: str = "EXECUTION",
    reply_to_id: int | None = None,
    message_key: str | None = None,
    payload: dict[str, Any] | None = None,
    bridge_url: str = DEFAULT_BRIDGE_URL,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Post an execution report from the agent back to the master."""
    hdrs = headers if headers is not None else get_auth_headers()
    key = message_key or f"agent-exec-{order_id or 'gen'}-{secrets.token_hex(6)}"
    body: dict[str, Any] = {
        "content": content,
        "order_id": order_id,
        "message_type": message_type,
        "status": status,
        "message_key": key,
        "reply_to_id": reply_to_id,
    }
    if payload:
        body["payload"] = payload

    for attempt in range(1, 4):
        try:
            resp = requests.post(
                f"{bridge_url}/api/messages",
                json=body,
                headers=hdrs,
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            if attempt == 3:
                raise
            logger.warning("Post attempt %d failed: %s. Retrying...", attempt, exc)
            time.sleep(attempt * 0.5)

    raise RuntimeError("Failed to post execution report after 3 attempts")


def is_master_order(msg: dict[str, Any]) -> bool:
    """Check if message is an actionable order from the master."""
    role = msg.get("role", "").lower()
    return role in ("master", "engineer")


def execute_order(msg: dict[str, Any], bridge_url: str, headers: dict[str, str]) -> None:
    """Handle an order received from the master."""
    order_id = msg.get("order_id") or msg.get("task_id")
    msg_id = msg["id"]
    content = msg.get("content", "")
    logger.info("Received order #%s (order_id: %s): %s", msg_id, order_id, content[:60])

    # 1. Acknowledge start of execution
    post_execution_report(
        content=f"Started execution of order #{msg_id} ({order_id or 'general'}): {content[:100]}",
        order_id=order_id,
        status="RUNNING",
        message_type="EXECUTION",
        reply_to_id=msg_id,
        bridge_url=bridge_url,
        headers=headers,
    )

    # 2. Simulated work or actual execution
    # In real usage, this dispatches to your agent tool or local script
    time.sleep(0.1)

    # 3. Report completion
    post_execution_report(
        content=f"Successfully executed order #{msg_id}: all steps completed.",
        order_id=order_id,
        status="COMPLETED",
        message_type="EXECUTION",
        reply_to_id=msg_id,
        payload={"order_id": order_id, "status": "COMPLETED", "exit_code": 0},
        bridge_url=bridge_url,
        headers=headers,
    )


def run_worker_loop(
    bridge_url: str = DEFAULT_BRIDGE_URL,
    token: str | None = None,
    start_mode: str = "pending",
    run_mode: str = "daemon",
    max_idle_polls: int = 3,
    poll_interval: float = 2.0,
) -> int:
    """Run the Agent polling loop."""
    headers = get_auth_headers(token)

    # Resolve starting cursor
    if start_mode == "now":
        cursor = fetch_latest_message_id(bridge_url, headers)
        logger.info("Start mode 'now': listening for messages after id=%d", cursor)
    elif start_mode == "history":
        cursor = 0
        logger.info("Start mode 'history': replaying all messages from id=0")
    else:  # 'pending'
        latest = fetch_latest_message_id(bridge_url, headers)
        cursor = max(0, latest - 20)
        logger.info("Start mode 'pending': checking recent messages from id=%d", cursor)

    idle_count = 0
    orders_processed = 0

    while True:
        try:
            messages = fetch_messages(cursor, bridge_url=bridge_url, headers=headers)
        except Exception as exc:
            logger.error("Error polling bridge: %s", exc)
            if run_mode == "once":
                return 1
            time.sleep(poll_interval)
            continue

        master_orders = [m for m in messages if is_master_order(m)]

        if messages:
            cursor = max(m["id"] for m in messages)

        if master_orders:
            idle_count = 0
            for order in master_orders:
                try:
                    execute_order(order, bridge_url=bridge_url, headers=headers)
                    orders_processed += 1
                except Exception as exc:
                    logger.error("Failed to execute order %s: %s", order.get("id"), exc)
                    post_execution_report(
                        content=f"Error executing order {order.get('id')}: {exc}",
                        order_id=order.get("order_id") or order.get("task_id"),
                        status="FAILED",
                        reply_to_id=order.get("id"),
                        bridge_url=bridge_url,
                        headers=headers,
                    )
        else:
            idle_count += 1

        if run_mode == "once":
            logger.info("Run mode 'once' complete (%d orders processed)", orders_processed)
            return 0

        if run_mode == "bounded" and idle_count >= max_idle_polls:
            logger.info(
                "Run mode 'bounded' finished after %d idle polls (%d orders processed)",
                idle_count,
                orders_processed,
            )
            return 0

        time.sleep(poll_interval)


def main() -> int:
    parser = argparse.ArgumentParser(description="Master-Agent Bridge — Agent Worker Poller")
    parser.add_argument(
        "--bridge-url",
        default=DEFAULT_BRIDGE_URL,
        help=f"Base URL for the Bridge API (default: {DEFAULT_BRIDGE_URL})",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Agent auth token (defaults to AGENT_TOKEN or WORKER_TOKEN env var)",
    )
    parser.add_argument(
        "--start-mode",
        choices=["pending", "now", "history"],
        default="pending",
        help="Starting point for message consumption (default: pending)",
    )
    parser.add_argument(
        "--run-mode",
        choices=["daemon", "once", "bounded"],
        default="daemon",
        help="Execution lifecycle mode (default: daemon)",
    )
    parser.add_argument(
        "--max-idle-polls",
        type=int,
        default=3,
        help="Max idle polls before exiting in bounded mode (default: 3)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="Seconds between polls in daemon mode (default: 2.0)",
    )
    args = parser.parse_args()

    return run_worker_loop(
        bridge_url=args.bridge_url,
        token=args.token,
        start_mode=args.start_mode,
        run_mode=args.run_mode,
        max_idle_polls=args.max_idle_polls,
        poll_interval=args.poll_interval,
    )


if __name__ == "__main__":
    sys.exit(main())
