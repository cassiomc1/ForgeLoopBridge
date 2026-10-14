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
import math
import os
import secrets
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

try:
    from self_improvement import create_improvement_record
except ImportError:
    create_improvement_record = None  # type: ignore[assignment]


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
    resp = requests.get(
        f"{bridge_url}/api/messages",
        params={"latest": "true", "limit": 1},
        headers=hdrs,
        timeout=10,
    )
    resp.raise_for_status()
    messages = resp.json()
    return messages[-1]["id"] if messages else 0


def fetch_pending_orders(
    bridge_url: str = DEFAULT_BRIDGE_URL,
    headers: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Read the full pending backlog using descending message-ID pagination."""
    hdrs = headers if headers is not None else get_auth_headers()
    orders: list[dict[str, Any]] = []
    before_id = None
    while True:
        params = {"limit": 100}
        if before_id is not None:
            params["before_id"] = before_id
        resp = requests.get(
            f"{bridge_url}/api/orders/pending", params=params, headers=hdrs, timeout=15,
        )
        resp.raise_for_status()
        page = resp.json()
        if not page:
            break
        next_cursor = min(order["id"] for order in page)
        if before_id is not None and any(order["id"] >= before_id for order in page):
            raise RuntimeError("Bridge pending-order pagination did not advance")
        orders.extend(page)
        if len(page) < 100:
            break
        before_id = next_cursor
    return sorted(orders, key=lambda order: order["id"])


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
    if role not in ("master", "engineer"):
        return False
    message_type = (msg.get("message_type") or "").upper()
    if message_type and message_type not in ("ORDER", "TASK", "INSTRUCTION"):
        return False
    if msg.get("typed_integrity") == "INVALID":
        return False
    typed = msg.get("typed")
    if typed is not None and typed.get("kind") not in ("ORDER", "TASK_REQUEST"):
        return False
    if not message_type and typed is None and not (msg.get("order_id") or msg.get("task_id")):
        return False
    status = (msg.get("status") or "").upper()
    return status not in ("COMPLETED", "CANCEL", "CANCELLED", "CANCELED")



def execute_order(
    msg: dict[str, Any], bridge_url: str, headers: dict[str, str],
    executor: Callable[[dict[str, Any]], str] | None = None,
) -> None:
    """Dispatch to a configured executor; report BLOCKED when none is configured.

    The executor must perform and verify the work before returning its report.
    Receiving an order alone never constitutes successful execution.
    """
    order_id = msg.get("order_id") or msg.get("task_id")
    msg_id = msg["id"]
    content = msg.get("content", "")
    logger.info("Received order #%s (order_id: %s): %s", msg_id, order_id, content[:60])

    if executor is None:
        status = "BLOCKED"
        report = "No task executor is configured. Connect an executor before running this order."
    else:
        post_execution_report(
            content=f"Started execution of order #{msg_id} ({order_id or 'general'}): {content[:100]}",
            order_id=order_id, status="RUNNING", reply_to_id=msg_id,
            message_key=f"agent-order-{msg_id}-running", bridge_url=bridge_url, headers=headers,
        )
        report = executor(msg)
        if not isinstance(report, str) or not report.strip():
            raise ValueError("Executor must return a non-empty verified execution report")
        status = "COMPLETED"

    result = post_execution_report(
        content=report,
        order_id=order_id,
        status=status,
        message_type="EXECUTION",
        reply_to_id=msg_id,
        message_key=f"agent-order-{msg_id}-{status.lower()}",
        bridge_url=bridge_url,
        headers=headers,
    )

    # 4. Standardized Self-Improvement Record
    if create_improvement_record is not None:
        try:
            task_name = order_id or f"order-{msg_id}"
            rec_path = create_improvement_record(
                task_name=task_name,
                role="agent",
                status=status,
                summary=report,
                improvements=[
                    "Maintain continuous automated test verifications after code edits.",
                    "Review latency and resource consumption for high-frequency polling.",
                ],
                issues=[
                    "No runtime blockers detected during execution." if executor else report,
                ],
                action_items=[
                    "Inspect verification logs and wait for next Master order.",
                ],
                raw_content=content,
                record_id=result["id"] if isinstance(result, dict) else None,
                timestamp=datetime.fromtimestamp(result["created_at"], UTC)
                if isinstance(result, dict) and "created_at" in result else None,
            )
            logger.info("Created self-improvement record: %s", rec_path.name)
        except Exception as exc:
            logger.warning("Failed to create self-improvement record: %s", exc)



def run_worker_loop(
    bridge_url: str = DEFAULT_BRIDGE_URL,
    token: str | None = None,
    start_mode: str = "pending",
    run_mode: str = "daemon",
    max_idle_polls: int = 3,
    poll_interval: float = 2.0,
    executor: Callable[[dict[str, Any]], str] | None = None,
) -> int:
    """Run the Agent polling loop."""
    headers = get_auth_headers(token)

    if start_mode not in {"pending", "now", "history"} or run_mode not in {"daemon", "once", "bounded"}:
        raise ValueError("Unsupported worker start or run mode")
    if max_idle_polls < 1 or not math.isfinite(poll_interval) or poll_interval < 0:
        raise ValueError("max_idle_polls must be positive and poll_interval finite and non-negative")

    backlog = None
    try:
        if start_mode == "history":
            cursor = 0
        else:
            # Capture the watermark before reading the backlog, so arrivals
            # during startup are still covered by subsequent polling.
            cursor = fetch_latest_message_id(bridge_url, headers)
            if start_mode == "pending":
                backlog = fetch_pending_orders(bridge_url, headers)
    except Exception as exc:
        logger.error("Could not initialize worker cursor/backlog: %s", exc)
        return 1

    idle_count = 0
    failure_count = 0
    orders_processed = 0
    had_failure = False
    bootstrap_handled: set[int] = set()

    while True:
        is_bootstrap = backlog is not None
        try:
            messages = backlog if is_bootstrap else fetch_messages(
                cursor, bridge_url=bridge_url, headers=headers,
            )
        except Exception as exc:
            logger.error("Error polling bridge: %s", exc)
            failure_count += 1
            if run_mode == "once" or (run_mode == "bounded" and failure_count >= max_idle_polls):
                return 1
            time.sleep(poll_interval)
            continue

        cycle_processed = 0
        reporting_failed = False
        for index, message in enumerate(messages):
            message_id = message["id"]
            if message_id in bootstrap_handled:
                bootstrap_handled.discard(message_id)
            elif is_master_order(message):
                try:
                    execute_order(message, bridge_url=bridge_url, headers=headers, executor=executor)
                    orders_processed += 1
                    cycle_processed += 1
                except Exception as exc:
                    had_failure = True
                    logger.error("Failed to execute order %s: %s", message_id, exc)
                    try:
                        post_execution_report(
                            content=f"Error executing order {message_id}: {exc}",
                            order_id=message.get("order_id") or message.get("task_id"),
                            status="FAILED", reply_to_id=message_id,
                            bridge_url=bridge_url, headers=headers,
                        )
                    except Exception as report_exc:
                        logger.error("Could not report failure for order %s: %s", message_id, report_exc)
                        reporting_failed = True
                        if is_bootstrap:
                            backlog = messages[index:]
                        break
                if is_bootstrap and message_id > cursor:
                    bootstrap_handled.add(message_id)
            if not is_bootstrap:
                # Advance only after dispatch or a failure report was accepted.
                cursor = max(cursor, message_id)

        if reporting_failed:
            failure_count += 1
            if run_mode == "once" or (run_mode == "bounded" and failure_count >= max_idle_polls):
                return 1
            time.sleep(poll_interval)
            continue

        backlog = None
        failure_count = 0
        idle_count = 0 if cycle_processed else idle_count + 1
        if run_mode == "once":
            logger.info("Run mode 'once' finished (%d orders dispatched)", orders_processed)
            return int(had_failure)
        if run_mode == "bounded" and idle_count >= max_idle_polls:
            logger.info("Run mode 'bounded' finished (%d orders dispatched)", orders_processed)
            return int(had_failure)
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
