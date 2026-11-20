"""Official MCP Events Extension (`io.modelcontextprotocol/events`, SEP-3415) for ForgeLoopBridge.

Implements:
- `server/discover` & `initialize` extension advertisement (`io.modelcontextprotocol/events`)
- `events/list`: typed ForgeLoopBridge event catalog with `inputSchema`, `payloadSchema`, and `delivery` modes
- `events/poll`: stateless poll delivery with opaque cursors (`<db_generation>:<after_id>`), `maxAgeMs`, `maxEvents`, `truncated`, `hasMore`, `nextPollMs`
- `events/stream`: real-time push delivery (`notifications/events/active`, `notifications/events/event`, `notifications/events/heartbeat`, `StreamEventsResult`) over Streamable HTTP SSE and stdio
- `events/subscribe` & `events/unsubscribe`: webhook delivery with Standard Webhooks HMAC-SHA256 signing (`whsec_`), SSRF protection, challenge verification handshake, TTL grants (`refreshBefore`), secret rotation dual-signing, and `deliveryStatus`
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from .resilience import MessageNotifier, format_checkpoint, parse_checkpoint
from .schema import get_db_generation
from .service import (
    HubServiceError,
    _matches_subscription,
    compute_is_waking,
    get_participant,
    normalize_role_value,
)
from .wakes import _validate_wake_url, backoff_seconds, build_standard_webhook_headers

logger = logging.getLogger("bridge.mcp_events")

EVENTS_EXTENSION_ID = "io.modelcontextprotocol/events"

# SEP-3415 JSON-RPC Error Codes
ERR_INVALID_PARAMS = -32602
ERR_HEADER_MISMATCH = -32020
ERR_NOT_FOUND = -32023
ERR_FORBIDDEN = -32024
ERR_RESOURCE_EXHAUSTED = -32025
ERR_UNSUPPORTED = -32026
ERR_CALLBACK_ENDPOINT = -32027

DEFAULT_POLL_MS_ACTIVE = 2000
DEFAULT_POLL_MS_IDLE = 10000
DEFAULT_MAX_EVENTS = 50
MAX_EVENTS_CAP = 200

DEFAULT_HEARTBEAT_MS = 15000
MIN_HEARTBEAT_MS = 1000
MAX_HEARTBEAT_MS = 60000

DEFAULT_WEBHOOK_TTL_MS = 3_600_000  # 1 hour
MIN_WEBHOOK_TTL_MS = 60_000  # 1 minute
MAX_WEBHOOK_TTL_MS = 7 * 86_400_000  # 7 days
SECRET_ROTATION_GRACE_SECONDS = 300.0  # 5 minutes dual-sign grace window
MAX_WEBHOOK_ATTEMPTS = 5


class MCPEventsError(Exception):
    """Protocol-level JSON-RPC error defined by SEP-3415."""

    def __init__(self, code: int, message: str, data: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _iso_timestamp(ts: float | None) -> str:
    val = ts if ts is not None else time.time()
    return datetime.fromtimestamp(float(val), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


_COMMON_SUBSCRIPTION_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "participant_id": {
            "type": "string",
            "description": "Optional participant ID to scope waking rules, @mentions, and direct chats.",
        },
        "conversation_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Subscribed channel/conversation IDs (defaults to all subscribed channels).",
        },
        "include_direct": {
            "type": "boolean",
            "default": True,
            "description": "Whether to include 1-to-1 direct messages.",
        },
        "priority": {
            "type": "string",
            "enum": ["NORMAL", "URGENT"],
            "description": "Optional filter by message priority.",
        },
        "order_id": {
            "type": "string",
            "description": "Optional filter for a specific Master order ID.",
        },
    },
    "additionalProperties": False,
}

_MINIMAL_MESSAGE_PAYLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "messageId": {"type": "integer"},
        "conversationId": {"type": "string"},
        "role": {"type": "string", "enum": ["master", "agent"]},
        "senderParticipantId": {"type": ["string", "null"]},
        "senderName": {"type": ["string", "null"]},
        "messageType": {"type": ["string", "null"]},
        "typedKind": {"type": ["string", "null"]},
        "orderId": {"type": ["string", "null"]},
        "status": {"type": ["string", "null"]},
        "priority": {"type": "string", "enum": ["NORMAL", "URGENT"]},
        "waking": {"type": "boolean"},
        "transferId": {"type": ["string", "null"]},
        "preview": {"type": "string", "description": "Short triage snippet (max 160 chars). Fetch full text via read_messages."},
        "createdAt": {"type": "string", "format": "date-time"},
    },
    "required": ["messageId", "conversationId", "role", "priority", "waking", "preview", "createdAt"],
}

EVENTS_CATALOG: list[dict[str, Any]] = [
    {
        "name": "bridge.message.waking",
        "description": (
            "Fires when a message arrives that should wake or interrupt the participant "
            "(URGENT priority, @mention, 1-to-1 direct message, system notice, Master ORDER for agents, or Agent EXECUTION/BLOCKER for Master)."
        ),
        "delivery": ["poll", "push", "webhook"],
        "inputSchema": _COMMON_SUBSCRIPTION_INPUT_SCHEMA,
        "payloadSchema": _MINIMAL_MESSAGE_PAYLOAD_SCHEMA,
    },
    {
        "name": "bridge.message.created",
        "description": "Fires when any new message is posted in the subscribed channels or direct chats.",
        "delivery": ["poll", "push", "webhook"],
        "inputSchema": _COMMON_SUBSCRIPTION_INPUT_SCHEMA,
        "payloadSchema": _MINIMAL_MESSAGE_PAYLOAD_SCHEMA,
    },
    {
        "name": "bridge.order.pending",
        "description": "Fires when Master publishes a new ORDER, TASK, INSTRUCTION, or DECISION message.",
        "delivery": ["poll", "push", "webhook"],
        "inputSchema": _COMMON_SUBSCRIPTION_INPUT_SCHEMA,
        "payloadSchema": _MINIMAL_MESSAGE_PAYLOAD_SCHEMA,
    },
    {
        "name": "bridge.execution.reported",
        "description": "Fires when an Agent submits an EXECUTION report, BLOCKER, or DECISION_NEEDED message.",
        "delivery": ["poll", "push", "webhook"],
        "inputSchema": _COMMON_SUBSCRIPTION_INPUT_SCHEMA,
        "payloadSchema": _MINIMAL_MESSAGE_PAYLOAD_SCHEMA,
    },
    {
        "name": "bridge.transfer.offered",
        "description": "Fires when an out-of-band file or folder transfer is completed and announced in a conversation.",
        "delivery": ["poll", "push", "webhook"],
        "inputSchema": _COMMON_SUBSCRIPTION_INPUT_SCHEMA,
        "payloadSchema": _MINIMAL_MESSAGE_PAYLOAD_SCHEMA,
    },
]

EVENTS_BY_NAME: dict[str, dict[str, Any]] = {e["name"]: e for e in EVENTS_CATALOG}


def validate_whsec_secret(secret: Any) -> str:
    """Validate Standard Webhooks symmetric secret (`whsec_<base64-of-24-to-64-bytes>`)."""
    if not isinstance(secret, str) or not secret.startswith("whsec_"):
        raise MCPEventsError(
            ERR_INVALID_PARAMS,
            "delivery.secret must be a Standard Webhooks secret starting with 'whsec_'",
        )
    b64_part = secret[6:]
    if not b64_part:
        raise MCPEventsError(ERR_INVALID_PARAMS, "delivery.secret base64 payload cannot be empty")
    try:
        raw = base64.b64decode(b64_part, validate=True)
    except Exception as exc:
        raise MCPEventsError(ERR_INVALID_PARAMS, "delivery.secret must contain valid base64 after 'whsec_'") from exc
    if not (24 <= len(raw) <= 64):
        raise MCPEventsError(
            ERR_INVALID_PARAMS,
            f"delivery.secret must decode to 24..64 random bytes (got {len(raw)} bytes)",
        )
    return secret


def validate_event_and_arguments(
    name: Any,
    arguments: Any,
    *,
    required_mode: str,
) -> tuple[str, dict[str, Any]]:
    if not isinstance(name, str) or not name:
        raise MCPEventsError(ERR_INVALID_PARAMS, "Event 'name' must be a non-empty string")
    descriptor = EVENTS_BY_NAME.get(name)
    if descriptor is None:
        raise MCPEventsError(
            ERR_NOT_FOUND,
            f"Unknown event type '{name}'",
            data={"kind": "event", "name": name},
        )
    if required_mode not in descriptor["delivery"]:
        raise MCPEventsError(
            ERR_UNSUPPORTED,
            f"Event '{name}' does not support delivery mode '{required_mode}'",
            data={"feature": "deliveryMode", "value": required_mode},
        )
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise MCPEventsError(ERR_INVALID_PARAMS, "'arguments' must be a JSON object")

    allowed_keys = set(_COMMON_SUBSCRIPTION_INPUT_SCHEMA["properties"].keys())
    unknown = set(arguments.keys()) - allowed_keys
    if unknown:
        raise MCPEventsError(
            ERR_INVALID_PARAMS,
            f"Unsupported subscription arguments: {sorted(unknown)}",
        )

    clean: dict[str, Any] = {}
    if "participant_id" in arguments and arguments["participant_id"] is not None:
        if not isinstance(arguments["participant_id"], str) or not arguments["participant_id"].strip():
            raise MCPEventsError(ERR_INVALID_PARAMS, "'participant_id' must be a non-empty string")
        clean["participant_id"] = arguments["participant_id"].strip()

    if "conversation_ids" in arguments and arguments["conversation_ids"] is not None:
        cids = arguments["conversation_ids"]
        if not isinstance(cids, list) or not all(isinstance(x, str) and x.strip() for x in cids):
            raise MCPEventsError(ERR_INVALID_PARAMS, "'conversation_ids' must be an array of non-empty strings")
        clean["conversation_ids"] = list(dict.fromkeys(x.strip() for x in cids))

    if "include_direct" in arguments and arguments["include_direct"] is not None:
        if not isinstance(arguments["include_direct"], bool):
            raise MCPEventsError(ERR_INVALID_PARAMS, "'include_direct' must be a boolean")
        clean["include_direct"] = arguments["include_direct"]

    if "priority" in arguments and arguments["priority"] is not None:
        if arguments["priority"] not in {"NORMAL", "URGENT"}:
            raise MCPEventsError(ERR_INVALID_PARAMS, "'priority' must be 'NORMAL' or 'URGENT'")
        clean["priority"] = arguments["priority"]

    if "order_id" in arguments and arguments["order_id"] is not None:
        if not isinstance(arguments["order_id"], str) or not arguments["order_id"].strip():
            raise MCPEventsError(ERR_INVALID_PARAMS, "'order_id' must be a non-empty string")
        clean["order_id"] = arguments["order_id"].strip()

    return name, clean


def derive_subscription_id(
    principal_key: str,
    callback_url: str,
    event_name: str,
    arguments: dict[str, Any],
) -> str:
    canonical_args = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
    material = f"{principal_key}\n{callback_url}\n{event_name}\n{canonical_args}".encode()
    digest = hashlib.sha256(material).hexdigest()[:16]
    return f"sub_{digest}"


def _event_matches_row(
    event_name: str,
    row: Any,
    *,
    recipient_participant_id: str | None,
    recipient_role: str,
    subscribed_conv_ids: set[str] | None,
    include_direct: bool,
    priority_filter: str | None,
    order_id_filter: str | None,
) -> tuple[bool, bool]:
    """Check whether a DB message row matches the event type and arguments.

    Returns ``(matches, is_waking)``.
    """
    data = dict(row)
    if recipient_participant_id and data.get("sender_participant_id") == recipient_participant_id:
        return False, False

    conv_kind = data.get("_conv_kind") or "channel"
    dm_low = data.get("_dm_low")
    dm_high = data.get("_dm_high")

    if not _matches_subscription(
        data,
        recipient_participant_id=recipient_participant_id,
        subscribed_conv_ids=subscribed_conv_ids,
        include_direct=include_direct,
        conv_kind=conv_kind,
        dm_low_id=dm_low,
        dm_high_id=dm_high,
    ):
        return False, False

    msg_priority = (data.get("priority") or "NORMAL").upper()
    if priority_filter and msg_priority != priority_filter:
        return False, False

    msg_order_id = data.get("order_id") or data.get("task_id")
    if order_id_filter and msg_order_id != order_id_filter:
        return False, False

    is_waking = compute_is_waking(
        data,
        recipient_participant_id=recipient_participant_id,
        recipient_role=recipient_role,
        conv_kind=conv_kind,
        dm_low_id=dm_low,
        dm_high_id=dm_high,
    )

    if event_name == "bridge.message.waking":
        return is_waking, is_waking

    if event_name == "bridge.message.created":
        return True, is_waking

    sender_role = normalize_role_value(str(data.get("role") or ""))
    msg_type = (data.get("message_type") or "").upper()
    typed_kind = (data.get("typed_kind") or "").upper()

    if event_name == "bridge.order.pending":
        is_order = sender_role == "master" and (
            msg_type in {"ORDER", "TASK", "INSTRUCTION", "DECISION"}
            or typed_kind in {"ORDER", "TASK_REQUEST", "DECISION"}
            or (not msg_type and bool(msg_order_id))
        )
        return is_order, is_waking

    if event_name == "bridge.execution.reported":
        is_exec = sender_role == "agent" and (
            msg_type in {"EXECUTION", "BLOCKER", "DECISION_NEEDED", "ACTION_REQUIRED"}
            or typed_kind in {"EXECUTION", "BLOCKER", "DECISION_REQUEST"}
        )
        return is_exec, is_waking

    if event_name == "bridge.transfer.offered":
        return bool(data.get("transfer_id")), is_waking

    return False, False


def format_event_occurrence(
    event_name: str,
    row: Any,
    *,
    is_waking: bool,
    cursor: str | None = None,
    include_cursor: bool = False,
) -> dict[str, Any]:
    data = dict(row)
    msg_id = int(data["id"])
    raw_content = str(data.get("content") or "").strip()
    preview = raw_content if len(raw_content) <= 160 else (raw_content[:157] + "...")
    created_iso = _iso_timestamp(data.get("created_at"))
    occurrence: dict[str, Any] = {
        "eventId": f"msg_{msg_id}",
        "name": event_name,
        "timestamp": created_iso,
        "data": {
            "messageId": msg_id,
            "conversationId": data.get("conversation_id") or "general",
            "role": normalize_role_value(str(data.get("role") or "agent")),
            "senderParticipantId": data.get("sender_participant_id"),
            "senderName": data.get("sender_name"),
            "messageType": data.get("message_type"),
            "typedKind": data.get("typed_kind"),
            "orderId": data.get("order_id") or data.get("task_id"),
            "status": data.get("status"),
            "priority": (data.get("priority") or "NORMAL").upper(),
            "waking": bool(is_waking),
            "transferId": data.get("transfer_id"),
            "preview": preview,
            "createdAt": created_iso,
        },
    }
    if include_cursor:
        occurrence["cursor"] = cursor
    return occurrence


async def authorize_event_subscription(
    db: Any,
    auth: Any,
    arguments: dict[str, Any],
) -> tuple[str | None, str]:
    """Verify principal permissions for subscription arguments.

    Returns ``(effective_participant_id, effective_role)``.
    Raises ``MCPEventsError(ERR_FORBIDDEN, ...)`` on access violation.
    """
    auth_pid = getattr(auth, "participant_id", None)
    auth_role = normalize_role_value(getattr(auth, "role", "agent"))
    req_pid = arguments.get("participant_id")

    if auth_pid is not None and req_pid is not None and req_pid != auth_pid:
        raise MCPEventsError(
            ERR_FORBIDDEN,
            "Cannot subscribe on behalf of another participant_id",
            data={"reason": "participant_mismatch"},
        )

    effective_pid = auth_pid or req_pid
    effective_role = auth_role
    if effective_pid is not None:
        part = await get_participant(db, effective_pid)
        if part is None:
            raise MCPEventsError(
                ERR_FORBIDDEN,
                f"Participant '{effective_pid}' does not exist",
                data={"reason": "participant_not_found"},
            )
        if part.get("removed_at") is not None:
            raise MCPEventsError(
                ERR_FORBIDDEN,
                f"Participant '{effective_pid}' has been removed",
                data={"reason": "participant_removed"},
            )
        effective_role = normalize_role_value(part["role"])
    return effective_pid, effective_role


async def resolve_cursor_and_events(
    db: Any,
    *,
    event_name: str,
    arguments: dict[str, Any],
    effective_pid: str | None,
    effective_role: str,
    cursor: str | None,
    max_age_ms: int | None = None,
    max_events: int = DEFAULT_MAX_EVENTS,
    include_occurrence_cursor: bool = False,
) -> dict[str, Any]:
    """Core cursor resolution and event query engine shared by Poll, Push, and Webhook modes."""
    current_gen = await get_db_generation(db)
    cur = await db.execute("SELECT COALESCE(MAX(id), 0) AS max_id FROM messages WHERE deleted_at IS NULL")
    head_id = int((await cur.fetchone())["max_id"])

    cur_pruned = await db.execute(
        "SELECT value FROM bridge_meta WHERE key = 'retention_pruned_through_id'"
    )
    pruned_row = await cur_pruned.fetchone()
    pruned_through_id = int(pruned_row["value"]) if pruned_row and pruned_row["value"] else 0

    # 1. Null cursor means "start from now" (no historical replay)
    initial_watermark = max(head_id, pruned_through_id)
    if cursor is None:
        return {
            "events": [],
            "cursor": format_checkpoint(current_gen, initial_watermark),
            "after_id": initial_watermark,
            "truncated": False,
            "hasMore": False,
        }

    # 2. Parse opaque cursor
    if not isinstance(cursor, str) or not cursor.strip():
        raise MCPEventsError(ERR_INVALID_PARAMS, "cursor must be a non-empty string or null")
    try:
        cursor_gen, cursor_after_id = parse_checkpoint(cursor)
    except ValueError as exc:
        raise MCPEventsError(ERR_INVALID_PARAMS, f"Malformed cursor: {exc}") from exc

    truncated = False
    effective_after = cursor_after_id

    # 3. Generation mismatch -> reset to current head with truncated=True
    if cursor_gen is not None and cursor_gen != current_gen:
        return {
            "events": [],
            "cursor": format_checkpoint(current_gen, initial_watermark),
            "after_id": initial_watermark,
            "truncated": True,
            "hasMore": False,
        }

    # 4. Retention pruning gap detection
    if pruned_through_id > 0 and effective_after < pruned_through_id:
        truncated = True
        effective_after = pruned_through_id

    # 5. Bounding replay with maxAgeMs
    now = time.time()
    if max_age_ms is not None:
        if not isinstance(max_age_ms, int) or max_age_ms < 0:
            raise MCPEventsError(ERR_INVALID_PARAMS, "maxAgeMs must be a non-negative integer")
        floor_ts = now - (max_age_ms / 1000.0)
        cur_floor = await db.execute(
            "SELECT COALESCE(MAX(id), 0) AS floor_id FROM messages WHERE deleted_at IS NULL AND created_at < ?",
            (floor_ts,),
        )
        floor_id = int((await cur_floor.fetchone())["floor_id"])
        if floor_id > effective_after:
            truncated = True
            effective_after = floor_id

    clamped_max = max(1, min(int(max_events), MAX_EVENTS_CAP))
    subscribed_convs = set(arguments["conversation_ids"]) if "conversation_ids" in arguments else None
    include_direct = bool(arguments.get("include_direct", True))
    priority_filter = arguments.get("priority")
    order_id_filter = arguments.get("order_id")

    rows = await db.execute_fetchall(
        """
        SELECT m.*, c.kind AS _conv_kind, c.participant_low_id AS _dm_low, c.participant_high_id AS _dm_high
        FROM messages m
        LEFT JOIN conversations c ON c.id = COALESCE(m.conversation_id, 'general')
        WHERE m.deleted_at IS NULL
          AND m.id > ?
        ORDER BY m.id ASC
        LIMIT ?
        """,
        (effective_after, clamped_max * 4 + 1),
    )

    occurrences: list[dict[str, Any]] = []
    has_more = False
    watermark_id = effective_after

    for row in rows:
        row_id = int(row["id"])
        matches, is_waking = _event_matches_row(
            event_name,
            row,
            recipient_participant_id=effective_pid,
            recipient_role=effective_role,
            subscribed_conv_ids=subscribed_convs,
            include_direct=include_direct,
            priority_filter=priority_filter,
            order_id_filter=order_id_filter,
        )
        if not matches:
            watermark_id = max(watermark_id, row_id)
            continue
        if len(occurrences) >= clamped_max:
            has_more = True
            break
        watermark_id = max(watermark_id, row_id)
        occ_cursor = format_checkpoint(current_gen, watermark_id)
        occurrences.append(
            format_event_occurrence(
                event_name,
                row,
                is_waking=is_waking,
                cursor=occ_cursor,
                include_cursor=include_occurrence_cursor,
            )
        )

    if not has_more and not rows:
        watermark_id = max(watermark_id, head_id)

    return {
        "events": occurrences,
        "cursor": format_checkpoint(current_gen, watermark_id),
        "after_id": watermark_id,
        "truncated": truncated,
        "hasMore": has_more,
    }


async def handle_events_poll(
    db: Any,
    auth: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handle `events/poll` request."""
    event_name, arguments = validate_event_and_arguments(
        params.get("name"),
        params.get("arguments"),
        required_mode="poll",
    )
    cursor = params.get("cursor")
    max_age_ms = params.get("maxAgeMs")
    max_events = params.get("maxEvents", DEFAULT_MAX_EVENTS)
    if max_events is not None and (not isinstance(max_events, int) or max_events < 1):
        raise MCPEventsError(ERR_INVALID_PARAMS, "maxEvents must be a positive integer")

    effective_pid, effective_role = await authorize_event_subscription(db, auth, arguments)
    resolved = await resolve_cursor_and_events(
        db,
        event_name=event_name,
        arguments=arguments,
        effective_pid=effective_pid,
        effective_role=effective_role,
        cursor=cursor,
        max_age_ms=max_age_ms,
        max_events=max_events or DEFAULT_MAX_EVENTS,
        include_occurrence_cursor=False,
    )
    next_poll_ms = DEFAULT_POLL_MS_ACTIVE if resolved["events"] else DEFAULT_POLL_MS_IDLE
    return {
        "resultType": "complete",
        "events": resolved["events"],
        "cursor": resolved["cursor"],
        "truncated": resolved["truncated"],
        "hasMore": resolved["hasMore"],
        "nextPollMs": next_poll_ms,
    }


async def prepare_events_stream(
    db: Any,
    auth: Any,
    req_id: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Validate and bootstrap an `events/stream` request before opening the SSE response."""
    if req_id is None:
        raise MCPEventsError(ERR_INVALID_PARAMS, "events/stream must be a request with an 'id'")
    event_name, arguments = validate_event_and_arguments(
        params.get("name"),
        params.get("arguments"),
        required_mode="push",
    )
    cursor = params.get("cursor")
    max_age_ms = params.get("maxAgeMs")
    heartbeat_ms = params.get("heartbeatMs", DEFAULT_HEARTBEAT_MS)
    if heartbeat_ms is not None and (not isinstance(heartbeat_ms, int) or heartbeat_ms <= 0):
        raise MCPEventsError(ERR_INVALID_PARAMS, "heartbeatMs must be a positive integer")
    granted_heartbeat_ms = max(MIN_HEARTBEAT_MS, min(int(heartbeat_ms or DEFAULT_HEARTBEAT_MS), MAX_HEARTBEAT_MS))

    effective_pid, effective_role = await authorize_event_subscription(db, auth, arguments)
    initial = await resolve_cursor_and_events(
        db,
        event_name=event_name,
        arguments=arguments,
        effective_pid=effective_pid,
        effective_role=effective_role,
        cursor=cursor,
        max_age_ms=max_age_ms,
        max_events=DEFAULT_MAX_EVENTS,
        include_occurrence_cursor=True,
    )
    return {
        "req_id": req_id,
        "event_name": event_name,
        "arguments": arguments,
        "effective_pid": effective_pid,
        "effective_role": effective_role,
        "granted_heartbeat_ms": granted_heartbeat_ms,
        "initial": initial,
    }


async def stream_events_sse(
    *,
    connect_db: Callable[[], Any],
    notifier: MessageNotifier,
    auth: Any,
    stream_ctx: dict[str, Any],
    request: Any = None,
    max_events_for_test: int | None = None,
) -> AsyncIterator[str]:
    """Yield SSE `data: <jsonrpc>\\n\\n` frames for `events/stream` push mode."""
    req_id = stream_ctx["req_id"]
    event_name = stream_ctx["event_name"]
    arguments = stream_ctx["arguments"]
    granted_hb_ms = stream_ctx["granted_heartbeat_ms"]
    initial = stream_ctx["initial"]
    sub_meta = {"io.modelcontextprotocol/subscriptionId": req_id}

    def _sse_frame(obj: dict[str, Any]) -> str:
        return f"data: {json.dumps(obj, ensure_ascii=False, separators=(',', ':'))}\n\n"

    # 1. Confirm subscription with notifications/events/active
    current_cursor = initial["cursor"]
    yield _sse_frame(
        {
            "jsonrpc": "2.0",
            "method": "notifications/events/active",
            "params": {
                "cursor": current_cursor,
                "truncated": initial["truncated"],
                "heartbeatMs": granted_hb_ms,
                "_meta": sub_meta,
            },
        }
    )

    delivered_count = 0
    for occ in initial["events"]:
        current_cursor = occ.get("cursor") or current_cursor
        params_obj = dict(occ)
        params_obj["_meta"] = sub_meta
        yield _sse_frame(
            {
                "jsonrpc": "2.0",
                "method": "notifications/events/event",
                "params": params_obj,
            }
        )
        delivered_count += 1
        if max_events_for_test is not None and delivered_count >= max_events_for_test:
            yield _sse_frame(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"resultType": "complete", "_meta": sub_meta},
                }
            )
            return

    hb_seconds = granted_hb_ms / 1000.0
    while True:
        if request is not None and hasattr(request, "is_disconnected") and await request.is_disconnected():
            break

        notified = await notifier.wait_for_message(hb_seconds, request=request)
        if request is not None and hasattr(request, "is_disconnected") and await request.is_disconnected():
            break

        async with connect_db() as db:
            try:
                effective_pid, effective_role = await authorize_event_subscription(db, auth, arguments)
            except MCPEventsError as exc:
                yield _sse_frame(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/events/terminated",
                        "params": {
                            "error": {"code": exc.code, "message": exc.message, "data": exc.data or {"reason": "Access revoked"}},
                            "_meta": sub_meta,
                        },
                    }
                )
                yield _sse_frame(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"resultType": "complete", "_meta": sub_meta},
                    }
                )
                return

            batch = await resolve_cursor_and_events(
                db,
                event_name=event_name,
                arguments=arguments,
                effective_pid=effective_pid,
                effective_role=effective_role,
                cursor=current_cursor,
                max_events=DEFAULT_MAX_EVENTS,
                include_occurrence_cursor=True,
            )

        if batch["truncated"]:
            current_cursor = batch["cursor"]
            yield _sse_frame(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/events/active",
                    "params": {
                        "cursor": current_cursor,
                        "truncated": True,
                        "heartbeatMs": granted_hb_ms,
                        "_meta": sub_meta,
                    },
                }
            )

        if batch["events"]:
            for occ in batch["events"]:
                current_cursor = occ.get("cursor") or batch["cursor"]
                params_obj = dict(occ)
                params_obj["_meta"] = sub_meta
                yield _sse_frame(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/events/event",
                        "params": params_obj,
                    }
                )
                delivered_count += 1
                if max_events_for_test is not None and delivered_count >= max_events_for_test:
                    yield _sse_frame(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {"resultType": "complete", "_meta": sub_meta},
                        }
                    )
                    return
        elif not notified:
            current_cursor = batch["cursor"]
            yield _sse_frame(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/events/heartbeat",
                    "params": {
                        "cursor": current_cursor,
                        "_meta": sub_meta,
                    },
                }
            )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "Redirects are not allowed for MCP webhooks", headers, fp)


def _post_mcp_webhook_sync(
    url: str,
    body_obj: dict[str, Any],
    *,
    subscription_id: str,
    secret: str,
    secondary_secret: str | None = None,
    webhook_id: str | None = None,
    timeout: float = 5.0,
    allow_private: bool = False,
) -> tuple[int, str, bytes]:
    """Perform SSRF-validated, redirect-disabled, Standard-Webhooks-signed HTTP POST."""
    try:
        _validate_wake_url(url, allow_private=allow_private)
    except HubServiceError as exc:
        return 0, "connection_refused", str(exc).encode("utf-8")

    body_bytes = json.dumps(body_obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "ForgeLoopBridge-MCPEvents/4.0",
        "X-MCP-Subscription-Id": subscription_id,
    }
    headers.update(
        build_standard_webhook_headers(
            body_bytes,
            secret,
            webhook_id=webhook_id,
            secondary_secret=secondary_secret,
        )
    )
    req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
    opener = urllib.request.build_opener(_NoRedirectHandler)
    try:
        with opener.open(req, timeout=timeout) as resp:
            resp_bytes = resp.read(16384)
            status = int(resp.status)
            if 200 <= status < 300:
                return status, "ok", resp_bytes
            return status, ("http_4xx" if 400 <= status < 500 else "http_5xx"), resp_bytes
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        cat = "http_4xx" if 400 <= status < 500 else "http_5xx"
        return status, cat, b""
    except urllib.error.URLError as exc:
        reason_name = type(exc.reason).__name__.lower()
        if "ssl" in reason_name or "cert" in reason_name:
            return 0, "tls_error", b""
        if "timeout" in reason_name:
            return 0, "timeout", b""
        return 0, "connection_refused", b""
    except TimeoutError:
        return 0, "timeout", b""


async def _default_mcp_webhook_sender(
    url: str,
    body_obj: dict[str, Any],
    *,
    subscription_id: str,
    secret: str,
    secondary_secret: str | None = None,
    webhook_id: str | None = None,
    timeout: float = 5.0,
    allow_private: bool = False,
) -> tuple[int, str, bytes]:
    return await asyncio.to_thread(
        _post_mcp_webhook_sync,
        url,
        body_obj,
        subscription_id=subscription_id,
        secret=secret,
        secondary_secret=secondary_secret,
        webhook_id=webhook_id,
        timeout=timeout,
        allow_private=allow_private,
    )


WebhookTransport = Callable[..., Awaitable[tuple[int, str, bytes]]]


class MCPEventWebhookManager:
    """Manages SEP-3415 `events/subscribe`, `events/unsubscribe`, endpoint verification, and webhook delivery."""

    def __init__(
        self,
        db_connector: Any,
        *,
        allow_private_urls: Callable[[], bool] | None = None,
        timeout_seconds: Callable[[], float] | None = None,
        transport: WebhookTransport | None = None,
    ) -> None:
        self._connect_db = db_connector
        self._allow_private = allow_private_urls or (lambda: False)
        self._timeout = timeout_seconds or (lambda: 5.0)
        self._transport: WebhookTransport = transport or _default_mcp_webhook_sender
        self._verified_endpoints: dict[tuple[str, str], float] = {}
        self._wakeup = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopped = False

    def set_transport(self, transport: WebhookTransport | None) -> None:
        self._transport = transport or _default_mcp_webhook_sender

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopped = False
        self._task = asyncio.create_task(self._loop(), name="bridge-mcp-events-webhooks")

    async def stop(self) -> None:
        self._stopped = True
        self._wakeup.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def notify_new_message(self) -> None:
        self._wakeup.set()

    async def _loop(self) -> None:
        while not self._stopped:
            try:
                await self.dispatch_due()
                self._wakeup.clear()
                try:
                    await asyncio.wait_for(self._wakeup.wait(), timeout=5.0)
                except TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("MCP Events webhook loop error: %s", exc)
                await asyncio.sleep(1.0)

    @staticmethod
    def _principal_key(auth: Any) -> str:
        pid = getattr(auth, "participant_id", None)
        role = normalize_role_value(getattr(auth, "role", "agent"))
        return f"participant:{pid}" if pid else f"role:{role}"

    def _validate_callback_url(self, raw_url: Any) -> str:
        if not isinstance(raw_url, str) or not raw_url.strip():
            raise MCPEventsError(ERR_INVALID_PARAMS, "delivery.url must be a non-empty https URL")
        parsed = urllib.parse.urlparse(raw_url.strip())
        if parsed.scheme != "https" or not parsed.hostname:
            raise MCPEventsError(
                ERR_INVALID_PARAMS,
                "delivery.url must use https:// and include a valid hostname",
            )
        try:
            return _validate_wake_url(raw_url.strip(), allow_private=self._allow_private())
        except HubServiceError as exc:
            raise MCPEventsError(ERR_INVALID_PARAMS, exc.message) from exc

    async def _verify_endpoint(
        self,
        *,
        principal_key: str,
        url: str,
        subscription_id: str,
        secret: str,
    ) -> float:
        now = time.time()
        cached_at = self._verified_endpoints.get((principal_key, url))
        if cached_at is not None and (now - cached_at) < 86_400.0:
            return cached_at

        nonce = f"chl_{secrets.token_urlsafe(24)}"
        wid = f"msg_verification_{secrets.token_urlsafe(12)}"
        status, reason, resp_bytes = await self._transport(
            url,
            {"type": "verification", "challenge": nonce},
            subscription_id=subscription_id,
            secret=secret,
            webhook_id=wid,
            timeout=self._timeout(),
            allow_private=self._allow_private(),
        )
        if not (200 <= status < 300):
            err_cat = reason if reason in {"connection_refused", "timeout", "tls_error", "http_4xx", "http_5xx"} else "challenge_failed"
            raise MCPEventsError(
                ERR_CALLBACK_ENDPOINT,
                "Webhook callback endpoint failed verification",
                data={"reason": err_cat},
            )
        try:
            body_json = json.loads(resp_bytes.decode("utf-8"))
            echoed = body_json.get("challenge") if isinstance(body_json, dict) else None
        except Exception:
            echoed = None

        if not isinstance(echoed, str) or not hmac.compare_digest(echoed, nonce):
            raise MCPEventsError(
                ERR_CALLBACK_ENDPOINT,
                "Webhook callback endpoint did not echo verification challenge",
                data={"reason": "challenge_failed"},
            )

        self._verified_endpoints[(principal_key, url)] = now
        return now

    async def subscribe(
        self,
        db: Any,
        auth: Any,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle `events/subscribe` (create or refresh webhook subscription)."""
        event_name, arguments = validate_event_and_arguments(
            params.get("name"),
            params.get("arguments"),
            required_mode="webhook",
        )
        delivery = params.get("delivery")
        if not isinstance(delivery, dict):
            raise MCPEventsError(ERR_INVALID_PARAMS, "delivery must be an object with url and secret")
        mode = delivery.get("mode", "webhook")
        if mode != "webhook":
            raise MCPEventsError(ERR_INVALID_PARAMS, "events/subscribe delivery.mode must be 'webhook'")

        url = self._validate_callback_url(delivery.get("url"))
        secret = validate_whsec_secret(delivery.get("secret"))

        has_ttl_key = "ttlMs" in params
        raw_ttl = params.get("ttlMs", DEFAULT_WEBHOOK_TTL_MS)
        if raw_ttl is not None and (not isinstance(raw_ttl, int) or isinstance(raw_ttl, bool) or raw_ttl <= 0):
            raise MCPEventsError(ERR_INVALID_PARAMS, "ttlMs must be a positive integer or null")

        now = time.time()
        if has_ttl_key and raw_ttl is None:
            refresh_before_ts: float | None = None
            refresh_before_iso: str | None = None
        else:
            clamped_ttl_ms = max(MIN_WEBHOOK_TTL_MS, min(int(raw_ttl or DEFAULT_WEBHOOK_TTL_MS), MAX_WEBHOOK_TTL_MS))
            refresh_before_ts = now + (clamped_ttl_ms / 1000.0)
            refresh_before_iso = _iso_timestamp(refresh_before_ts)

        effective_pid, effective_role = await authorize_event_subscription(db, auth, arguments)
        principal_key = self._principal_key(auth)
        sub_id = derive_subscription_id(principal_key, url, event_name, arguments)

        # Check if subscription already exists
        cur = await db.execute("SELECT * FROM mcp_event_subscriptions WHERE id = ?", (sub_id,))
        existing = await cur.fetchone()
        is_existing_live = existing is not None and (
            existing["refresh_before"] is None or float(existing["refresh_before"]) > now
        )

        if is_existing_live:
            self._verified_endpoints[(principal_key, url)] = float(existing["verified_at"])
            verified_at = float(existing["verified_at"])
        else:
            verified_at = await self._verify_endpoint(
                principal_key=principal_key,
                url=url,
                subscription_id=sub_id,
                secret=secret,
            )

        cursor_param = params.get("cursor")
        max_age_ms = params.get("maxAgeMs")
        resolved = await resolve_cursor_and_events(
            db,
            event_name=event_name,
            arguments=arguments,
            effective_pid=effective_pid,
            effective_role=effective_role,
            cursor=cursor_param,
            max_age_ms=max_age_ms,
            max_events=1,
            include_occurrence_cursor=False,
        )

        # Determine initial cursor watermark
        current_gen = await get_db_generation(db)
        if cursor_param is None:
            start_after_id = int(resolved["after_id"])
        else:
            _, parsed_after = parse_checkpoint(cursor_param)
            if resolved["truncated"]:
                start_after_id = int(resolved["after_id"])
            else:
                start_after_id = parsed_after

        if is_existing_live:
            watermark_after_id = max(int(existing["cursor_after_id"]), start_after_id)
            prev_secret = existing["secret_encrypted"] if existing["secret_encrypted"] != secret else existing["prev_secret_encrypted"]
            prev_until = (now + SECRET_ROTATION_GRACE_SECONDS) if existing["secret_encrypted"] != secret else existing["prev_secret_until"]

            was_active = bool(existing["active"])
            last_delivery_iso = _iso_timestamp(existing["last_delivery_at"]) if existing["last_delivery_at"] else None
            last_error = existing["last_error"]
            failed_since_iso = _iso_timestamp(existing["failed_since"]) if existing["failed_since"] else None

            await db.execute(
                """
                UPDATE mcp_event_subscriptions
                SET secret_encrypted = ?,
                    prev_secret_encrypted = ?,
                    prev_secret_until = ?,
                    cursor_after_id = ?,
                    refresh_before = ?,
                    active = 1,
                    failures = CASE WHEN active = 0 THEN 0 ELSE failures END,
                    due_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    secret,
                    prev_secret,
                    prev_until,
                    watermark_after_id,
                    refresh_before_ts,
                    now if resolved["events"] else existing["due_at"],
                    now,
                    sub_id,
                ),
            )
            await db.commit()

            delivery_status: dict[str, Any] = {
                "active": was_active,
                "lastDeliveryAt": last_delivery_iso,
                "lastError": last_error,
            }
            if failed_since_iso and not was_active:
                delivery_status["failedSince"] = failed_since_iso

            if resolved["events"]:
                self._wakeup.set()

            return {
                "resultType": "complete",
                "id": sub_id,
                "refreshBefore": refresh_before_iso,
                "cursor": format_checkpoint(current_gen, watermark_after_id),
                "truncated": resolved["truncated"],
                "deliveryStatus": delivery_status,
            }

        # Fresh or expired subscription
        due_at = now if resolved["events"] else None
        await db.execute(
            """
            INSERT OR REPLACE INTO mcp_event_subscriptions (
                id, principal_key, role, participant_id, event_name, arguments_json,
                callback_url, secret_encrypted, prev_secret_encrypted, prev_secret_until,
                cursor_after_id, refresh_before, active, failures, due_at,
                last_delivery_at, last_error, failed_since, verified_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, 1, 0, ?, NULL, NULL, NULL, ?, ?, ?)
            """,
            (
                sub_id,
                principal_key,
                effective_role,
                effective_pid,
                event_name,
                json.dumps(arguments, sort_keys=True, separators=(",", ":")),
                url,
                secret,
                start_after_id,
                refresh_before_ts,
                due_at,
                verified_at,
                now,
                now,
            ),
        )
        await db.commit()
        if due_at is not None:
            self._wakeup.set()

        return {
            "resultType": "complete",
            "id": sub_id,
            "refreshBefore": refresh_before_iso,
            "cursor": format_checkpoint(current_gen, start_after_id),
            "truncated": resolved["truncated"],
        }

    async def unsubscribe(
        self,
        db: Any,
        auth: Any,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle `events/unsubscribe`."""
        event_name, arguments = validate_event_and_arguments(
            params.get("name"),
            params.get("arguments"),
            required_mode="webhook",
        )
        delivery = params.get("delivery")
        if not isinstance(delivery, dict) or not isinstance(delivery.get("url"), str):
            raise MCPEventsError(ERR_INVALID_PARAMS, "delivery.url is required for events/unsubscribe")
        url = self._validate_callback_url(delivery.get("url"))
        principal_key = self._principal_key(auth)
        sub_id = derive_subscription_id(principal_key, url, event_name, arguments)

        cur = await db.execute(
            "DELETE FROM mcp_event_subscriptions WHERE id = ? AND principal_key = ?",
            (sub_id, principal_key),
        )
        await db.commit()
        if (cur.rowcount or 0) == 0:
            raise MCPEventsError(
                ERR_NOT_FOUND,
                "No matching webhook subscription found",
                data={"kind": "subscription"},
            )
        return {"resultType": "complete"}

    async def schedule_matching_subscriptions(self, db: Any, *, now: float | None = None) -> int:
        current = now if now is not None else time.time()
        cur = await db.execute(
            """
            UPDATE mcp_event_subscriptions
            SET due_at = COALESCE(due_at, ?)
            WHERE active = 1
              AND (refresh_before IS NULL OR refresh_before > ?)
            """,
            (current, current),
        )
        count = int(cur.rowcount or 0)
        if count > 0:
            await db.commit()
            self._wakeup.set()
        return count

    async def dispatch_due(self, *, now: float | None = None) -> list[dict[str, Any]]:
        current = now if now is not None else time.time()
        results: list[dict[str, Any]] = []
        async with self._connect_db() as db:
            # Reclaim expired TTL subscriptions
            await db.execute(
                "DELETE FROM mcp_event_subscriptions WHERE refresh_before IS NOT NULL AND refresh_before <= ?",
                (current,),
            )
            await db.commit()

            subs = await db.execute_fetchall(
                """
                SELECT * FROM mcp_event_subscriptions
                WHERE active = 1
                  AND due_at IS NOT NULL
                  AND due_at <= ?
                ORDER BY due_at ASC
                LIMIT 16
                """,
                (current,),
            )
            if not subs:
                return results

            current_gen = await get_db_generation(db)
            for sub in subs:
                sub_id = sub["id"]
                url = sub["callback_url"]
                secret = sub["secret_encrypted"]
                secondary_secret = (
                    sub["prev_secret_encrypted"]
                    if sub["prev_secret_encrypted"] and sub["prev_secret_until"] and float(sub["prev_secret_until"]) > current
                    else None
                )
                arguments = json.loads(sub["arguments_json"] or "{}")
                pid = sub["participant_id"]
                role = sub["role"]

                # Re-verify participant authorization at delivery time
                if pid is not None:
                    part = await get_participant(db, pid)
                    if part is None or part.get("removed_at") is not None:
                        await self._transport(
                            url,
                            {
                                "type": "terminated",
                                "error": {
                                    "code": ERR_FORBIDDEN,
                                    "message": "Forbidden",
                                    "data": {"reason": "Access revoked"},
                                },
                            },
                            subscription_id=sub_id,
                            secret=secret,
                            secondary_secret=secondary_secret,
                            webhook_id=f"msg_terminated_{secrets.token_urlsafe(12)}",
                            timeout=self._timeout(),
                            allow_private=self._allow_private(),
                        )
                        await db.execute("DELETE FROM mcp_event_subscriptions WHERE id = ?", (sub_id,))
                        await db.commit()
                        results.append({"id": sub_id, "terminated": True, "reason": "Forbidden"})
                        continue

                cursor_str = format_checkpoint(current_gen, int(sub["cursor_after_id"]))
                batch = await resolve_cursor_and_events(
                    db,
                    event_name=sub["event_name"],
                    arguments=arguments,
                    effective_pid=pid,
                    effective_role=role,
                    cursor=cursor_str,
                    max_events=25,
                    include_occurrence_cursor=True,
                )

                if batch["truncated"]:
                    await self._transport(
                        url,
                        {"type": "gap", "cursor": batch["cursor"]},
                        subscription_id=sub_id,
                        secret=secret,
                        secondary_secret=secondary_secret,
                        webhook_id=f"msg_gap_{secrets.token_urlsafe(12)}",
                        timeout=self._timeout(),
                        allow_private=self._allow_private(),
                    )

                if not batch["events"]:
                    await db.execute(
                        "UPDATE mcp_event_subscriptions SET cursor_after_id = ?, due_at = NULL WHERE id = ?",
                        (batch["after_id"], sub_id),
                    )
                    await db.commit()
                    continue

                all_ok = True
                last_err_cat: str | None = None
                ack_after_id = int(sub["cursor_after_id"])

                for occ in batch["events"]:
                    occ_msg_id = int(occ["data"]["messageId"])
                    status_code, reason_cat, _ = await self._transport(
                        url,
                        occ,
                        subscription_id=sub_id,
                        secret=secret,
                        secondary_secret=secondary_secret,
                        webhook_id=f"msg_{sub_id}_{occ_msg_id}",
                        timeout=self._timeout(),
                        allow_private=self._allow_private(),
                    )
                    # 2xx or non-retryable 410 Gone / 413 Payload Too Large advances watermark
                    if (200 <= status_code < 300) or status_code in {410, 413}:
                        ack_after_id = max(ack_after_id, occ_msg_id)
                        results.append({"id": sub_id, "eventId": occ["eventId"], "ok": True})
                    else:
                        all_ok = False
                        last_err_cat = reason_cat
                        results.append({"id": sub_id, "eventId": occ["eventId"], "ok": False, "error": reason_cat})
                        break

                if all_ok:
                    next_due = current if batch["hasMore"] else None
                    await db.execute(
                        """
                        UPDATE mcp_event_subscriptions
                        SET cursor_after_id = ?,
                            due_at = ?,
                            failures = 0,
                            last_delivery_at = ?,
                            last_error = NULL,
                            failed_since = NULL,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            batch["after_id"] if not batch["hasMore"] else ack_after_id,
                            next_due,
                            current,
                            current,
                            sub_id,
                        ),
                    )
                else:
                    failures = int(sub["failures"] or 0) + 1
                    failed_since = sub["failed_since"] or current
                    if failures >= MAX_WEBHOOK_ATTEMPTS:
                        await db.execute(
                            """
                            UPDATE mcp_event_subscriptions
                            SET cursor_after_id = ?,
                                active = 0,
                                failures = ?,
                                due_at = NULL,
                                last_error = ?,
                                failed_since = ?,
                                updated_at = ?
                            WHERE id = ?
                            """,
                            (ack_after_id, failures, last_err_cat, failed_since, current, sub_id),
                        )
                    else:
                        retry_at = current + backoff_seconds(failures)
                        await db.execute(
                            """
                            UPDATE mcp_event_subscriptions
                            SET cursor_after_id = ?,
                                failures = ?,
                                due_at = ?,
                                last_error = ?,
                                failed_since = ?,
                                updated_at = ?
                            WHERE id = ?
                            """,
                            (ack_after_id, failures, retry_at, last_err_cat, failed_since, current, sub_id),
                        )
                await db.commit()
        return results
