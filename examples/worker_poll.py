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
import json
import logging
import math
import os
import secrets
import sys
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

try:
    from self_improvement import create_improvement_record, extract_improvement_sections
except ImportError:
    create_improvement_record = None  # type: ignore[assignment]
    extract_improvement_sections = None  # type: ignore[assignment]


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [agent-worker]: %(message)s",
)
logger = logging.getLogger("agent-worker")

DEFAULT_BRIDGE_URL = os.getenv("BRIDGE_URL", "http://localhost:8000")
AGENT_TOKEN = os.getenv("AGENT_TOKEN") or os.getenv("WORKER_TOKEN", "")


def _normalize_bridge_url(bridge_url: str) -> str:
    return bridge_url.rstrip("/")


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
    base_url = _normalize_bridge_url(bridge_url)
    resp = requests.get(
        f"{base_url}/api/messages",
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
    base_url = _normalize_bridge_url(bridge_url)
    seen_ids: dict[int, dict[str, Any]] = {}
    before_id = None
    while True:
        params = {"limit": 100}
        if before_id is not None:
            params["before_id"] = before_id
        resp = requests.get(
            f"{base_url}/api/orders/pending", params=params, headers=hdrs, timeout=15,
        )
        resp.raise_for_status()
        page = resp.json()
        if not page:
            break
        next_cursor = min(order["id"] for order in page)
        if before_id is not None and any(order["id"] >= before_id for order in page):
            raise RuntimeError("Bridge pending-order pagination did not advance")
        for order in page:
            seen_ids.setdefault(order["id"], order)
        if len(page) < 100:
            break
        before_id = next_cursor
    return sorted(seen_ids.values(), key=lambda order: order["id"])


class GenerationMismatchError(RuntimeError):
    """Raised when the server database generation changed since the saved cursor."""

    def __init__(self, current_generation: str | None):
        super().__init__("Bridge database generation mismatch")
        self.current_generation = current_generation


def load_cursor_state(cursor_file: str | Path) -> tuple[int, str | None]:
    """Load persisted ``(cursor, generation)`` from a local state file."""
    path = Path(cursor_file)
    if not path.is_file():
        return 0, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return 0, None
        cursor = max(0, int(data.get("cursor", 0)))
        generation = data.get("generation")
        return cursor, str(generation) if generation else None
    except Exception:
        return 0, None


def save_cursor_state(
    cursor_file: str | Path,
    cursor: int,
    generation: str | None = None,
) -> None:
    """Persist ``(cursor, generation)`` atomically with owner-only (0600) permissions."""
    path = Path(cursor_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".{path.name}.{os.getpid()}.tmp"
    payload = {"cursor": max(0, int(cursor)), "generation": generation, "updated_at": time.time()}
    tmp_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    os.replace(tmp_path, path)


def register_worker_participant(
    name: str,
    bridge_url: str = DEFAULT_BRIDGE_URL,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Register (or reuse) a participant identity for this worker."""
    hdrs = headers if headers is not None else get_auth_headers()
    base_url = _normalize_bridge_url(bridge_url)
    resp = requests.post(
        f"{base_url}/api/participants",
        json={
            "name": name,
            "role": "agent",
            "kind": "agent",
            "agent_type": "worker-poll",
            "issue_token": False,
        },
        headers=hdrs,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_messages(
    after_id: int,
    bridge_url: str = DEFAULT_BRIDGE_URL,
    headers: dict[str, str] | None = None,
    limit: int = 100,
    *,
    wait_seconds: float | None = None,
    conversation_id: str | None = None,
    generation: str | None = None,
) -> list[dict[str, Any]]:
    """Poll messages from the bridge with id > after_id."""
    hdrs = headers if headers is not None else get_auth_headers()
    base_url = _normalize_bridge_url(bridge_url)
    params: dict[str, Any] = {"after_id": after_id, "limit": limit}
    timeout = 15.0
    if wait_seconds is not None and wait_seconds > 0:
        params["wait_seconds"] = wait_seconds
        timeout = max(15.0, float(wait_seconds) + 10.0)
    if conversation_id is not None:
        params["conversation_id"] = conversation_id
    if generation is not None:
        params["generation"] = generation
    resp = requests.get(
        f"{base_url}/api/messages",
        params=params,
        headers=hdrs,
        timeout=timeout,
    )
    if getattr(resp, "status_code", 200) == 409:
        try:
            err = resp.json().get("error", {})
        except Exception:
            err = {}
        if err.get("code") == "E_BRIDGE_GENERATION_MISMATCH":
            raise GenerationMismatchError(err.get("current_generation"))
    resp.raise_for_status()
    return resp.json()


def iter_sse_messages(
    bridge_url: str = DEFAULT_BRIDGE_URL,
    headers: dict[str, str] | None = None,
    *,
    timeout: float = 30.0,
) -> Iterator[dict[str, Any]]:
    """Stream incoming messages over SSE using a short-lived stream ticket."""
    hdrs = headers if headers is not None else get_auth_headers()
    base_url = _normalize_bridge_url(bridge_url)
    ticket_resp = requests.post(f"{base_url}/api/stream-ticket", headers=hdrs, timeout=10)
    ticket_resp.raise_for_status()
    ticket = ticket_resp.json()["ticket"]
    with requests.get(
        f"{base_url}/api/stream",
        params={"ticket": ticket},
        stream=True,
        timeout=timeout,
    ) as stream_resp:
        stream_resp.raise_for_status()
        for raw_line in stream_resp.iter_lines(decode_unicode=True):
            if not raw_line or raw_line.startswith(":") or raw_line.startswith("id:"):
                continue
            if raw_line.startswith("data:"):
                payload = raw_line[5:].strip()
                if payload:
                    try:
                        yield json.loads(payload)
                    except json.JSONDecodeError:
                        continue


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
    base_url = _normalize_bridge_url(bridge_url)
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
                f"{base_url}/api/messages",
                json=body,
                headers=hdrs,
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code is not None and 400 <= status_code < 500 and status_code not in (408, 429):
                raise
            if attempt == 3:
                raise
            delay = attempt * 0.5
            if status_code == 429 and exc.response is not None:
                raw_retry = exc.response.headers.get("Retry-After")
                if raw_retry is not None:
                    try:
                        retry_after = float(raw_retry)
                    except (TypeError, ValueError):
                        retry_after = delay
                    if math.isfinite(retry_after):
                        delay = min(30.0, max(0.5, retry_after))
            logger.warning("Post attempt %d failed: %s. Retrying...", attempt, exc)
            time.sleep(delay)
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
            extracted = (
                extract_improvement_sections(report)
                if extract_improvement_sections is not None
                else {"improvements": [], "issues": [], "action_items": []}
            )
            improvements = extracted["improvements"] or [
                "Maintain continuous automated test verifications after code edits.",
                "Review latency and resource consumption for high-frequency polling.",
            ]
            issues = extracted["issues"] or [
                "No runtime blockers detected during execution." if executor else report,
            ]
            action_items = extracted["action_items"] or [
                "Inspect verification logs and wait for next Master order.",
            ]
            rec_path = create_improvement_record(
                task_name=task_name,
                role="agent",
                status=status,
                summary=report,
                improvements=improvements,
                issues=issues,
                action_items=action_items,
                raw_content=report,
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
    *,
    cursor_file: str | Path | None = None,
    transport: str = "poll",
    wait_seconds: float = 25.0,
    conversation_id: str | None = None,
    participant_name: str | None = None,
) -> int:
    """Run the Agent polling loop."""
    headers = get_auth_headers(token)
    bridge_url = _normalize_bridge_url(bridge_url)

    if start_mode not in {"pending", "now", "history"} or run_mode not in {"daemon", "once", "bounded"}:
        raise ValueError("Unsupported worker start or run mode")
    if transport not in {"poll", "long-poll", "sse"}:
        raise ValueError("Unsupported worker transport")
    if max_idle_polls < 1 or not math.isfinite(poll_interval) or poll_interval < 0:
        raise ValueError("max_idle_polls must be positive and poll_interval finite and non-negative")

    if participant_name:
        try:
            register_worker_participant(participant_name, bridge_url=bridge_url, headers=headers)
        except Exception as exc:
            logger.warning("Participant auto-registration failed (continuing): %s", exc)

    saved_cursor, saved_generation = (0, None)
    has_saved_cursor = False
    if cursor_file is not None and Path(cursor_file).is_file():
        saved_cursor, saved_generation = load_cursor_state(cursor_file)
        has_saved_cursor = True

    backlog = None
    generation = saved_generation
    try:
        if has_saved_cursor:
            cursor = saved_cursor
            if start_mode == "pending":
                backlog = fetch_pending_orders(bridge_url, headers)
        elif start_mode == "history":
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

    def _persist_cursor() -> None:
        if cursor_file is not None:
            save_cursor_state(cursor_file, cursor, generation)

    def _fetch_next() -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"bridge_url": bridge_url, "headers": headers}
        if transport == "long-poll":
            kwargs["wait_seconds"] = wait_seconds
        if conversation_id is not None:
            kwargs["conversation_id"] = conversation_id
        if generation is not None:
            kwargs["generation"] = generation
        return fetch_messages(cursor, **kwargs)

    sse_iter: Iterator[dict[str, Any]] | None = None

    while True:
        is_bootstrap = backlog is not None
        try:
            if is_bootstrap:
                messages = backlog
            elif transport == "sse":
                if sse_iter is None:
                    catchup = _fetch_next()
                    if catchup:
                        messages = catchup
                    else:
                        sse_iter = iter_sse_messages(bridge_url, headers, timeout=max(15.0, wait_seconds))
                        try:
                            event_msg = next(sse_iter)
                            messages = [event_msg] if event_msg.get("id", 0) > cursor else []
                        except StopIteration:
                            sse_iter = None
                            messages = []
                else:
                    try:
                        event_msg = next(sse_iter)
                        messages = [event_msg] if event_msg.get("id", 0) > cursor else []
                    except StopIteration:
                        sse_iter = None
                        messages = _fetch_next()
            else:
                messages = _fetch_next()
        except GenerationMismatchError as exc:
            logger.warning("Server database generation changed; resetting cursor from %d to 0", cursor)
            cursor = 0
            generation = exc.current_generation
            _persist_cursor()
            sse_iter = None
            continue
        except Exception as exc:
            sse_iter = None
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
                _persist_cursor()

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
        if not is_bootstrap and len(messages) >= 100:
            continue
        if transport == "long-poll" and messages:
            continue
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
    parser.add_argument(
        "--cursor-file",
        default=None,
        help="Optional JSON file path to persist cursor and db_generation across restarts",
    )
    parser.add_argument(
        "--transport",
        choices=["poll", "long-poll", "sse"],
        default="poll",
        help="Delivery transport: poll, long-poll, or sse (default: poll)",
    )
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=25.0,
        help="Wait timeout in seconds for --transport long-poll (default: 25.0)",
    )
    parser.add_argument(
        "--conversation-id",
        default=None,
        help="Optional channel or conversation ID filter",
    )
    parser.add_argument(
        "--participant-name",
        default=None,
        help="Optional participant handle to register automatically on startup",
    )
    args = parser.parse_args()

    try:
        return run_worker_loop(
            bridge_url=args.bridge_url,
            token=args.token,
            start_mode=args.start_mode,
            run_mode=args.run_mode,
            max_idle_polls=args.max_idle_polls,
            poll_interval=args.poll_interval,
            cursor_file=args.cursor_file,
            transport=args.transport,
            wait_seconds=args.wait_seconds,
            conversation_id=args.conversation_id,
            participant_name=args.participant_name,
        )
    except KeyboardInterrupt:
        logger.info("Agent worker stopped by interrupt")
        return 0


if __name__ == "__main__":
    sys.exit(main())
