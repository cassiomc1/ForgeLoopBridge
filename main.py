"""Master-Agent Bridge — High-reliability communication hub

Designed for stable and reliable exchange of orders and executions between
Master and Agent.
"""

import asyncio
import json
import logging
import math
import os
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import aiosqlite
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

import self_improvement
from bridge_hub import GENERAL_CONVERSATION_ID, HUB_VERSION
from bridge_hub.instructions import render_instructions_markdown, render_llms_txt
from bridge_hub.mcp_events import (
    ERR_HEADER_MISMATCH,
    MCPEventsError,
    MCPEventWebhookManager,
    handle_events_poll,
    prepare_events_stream,
    stream_events_sse,
)
from bridge_hub.mcp_server import MCPServices, _jsonrpc_error, handle_mcp_message
from bridge_hub.mentions import resolve_mentions
from bridge_hub.resilience import (
    MessageNotifier,
    StuckReadersTracker,
    format_checkpoint,
    parse_checkpoint,
)
from bridge_hub.schema import get_db_generation, init_hub_schema, sanitize_fts_query
from bridge_hub.service import (
    HubServiceError,
    ack_signal,
    check_signal,
    create_channel,
    create_or_rotate_signal,
    fetch_updates_page,
    get_hub_context,
    get_participant,
    hash_secret,
    list_conversations,
    list_participants,
    open_or_create_direct_chat,
    register_participant,
    remove_participant,
    resolve_conversation_by_id_or_name,
    resolve_participant_by_id_or_name,
    restore_participant,
    revoke_signals,
    rotate_participant_token,
    search_messages_fts,
    set_channel_archived,
    update_participant,
)
from bridge_hub.transfers import (
    TransferTicketStore,
    cleanup_expired_transfers,
    get_pack_command,
    get_transfer_details,
    list_transfers,
    prepare_transfer,
    record_transfer_fetch,
    resolve_safe_blob_path,
    sanitize_content_disposition_filename,
    stream_upload_transfer,
)
from bridge_hub.wakes import WakeScheduler
from bridge_protocol.errors import (
    E_BRIDGE_IDEMPOTENCY_CONFLICT,
    E_BRIDGE_PERSISTED_TYPED_INVALID,
    E_BRIDGE_REPLY_NOT_FOUND,
    E_BRIDGE_REPLY_ROLE_INVALID,
    E_BRIDGE_TYPED_PAYLOAD_INVALID,
    E_BRIDGE_TYPED_PAYLOAD_TOO_LARGE,
    BridgeProtocolError,
)
from bridge_protocol.validation import (
    envelope_to_dict,
    parse_typed_envelope,
    validate_legacy_kind_consistency,
    validate_reply_relationship,
)

# ─── Config ───────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(
    os.getenv(
        "BRIDGE_DB",
        os.getenv("FORGEBRIDGE_DB", str(BASE_DIR / "data" / "bridge.db")),
    )
)
STATIC_DIR = BASE_DIR / "static"
PROMPTS_DIR = BASE_DIR / "prompts"
IMPROVEMENTS_DIR = Path(os.getenv("IMPROVEMENTS_DIR", str(BASE_DIR / "improvements")))
UPLOADS_DIR = Path(os.getenv("UPLOADS_DIR", str(BASE_DIR / "data" / "uploads")))
HOST = os.getenv("HOST", "0.0.0.0")
RELOAD = os.getenv("RELOAD") == "1"

MASTER_TOKEN = os.getenv("MASTER_TOKEN") or os.getenv("ENGINEER_TOKEN")
AGENT_TOKEN = os.getenv("AGENT_TOKEN") or os.getenv("WORKER_TOKEN")

# Keep legacy names mapped for backward compatibility:
ENGINEER_TOKEN = os.getenv("ENGINEER_TOKEN") or MASTER_TOKEN
WORKER_TOKEN = os.getenv("WORKER_TOKEN") or AGENT_TOKEN

if not MASTER_TOKEN or not AGENT_TOKEN:
    raise RuntimeError(
        "MASTER_TOKEN and AGENT_TOKEN (or ENGINEER_TOKEN and WORKER_TOKEN) must be set in the environment. "
        "Generate strong tokens with: openssl rand -hex 32"
    )
if not {MASTER_TOKEN, ENGINEER_TOKEN}.isdisjoint({AGENT_TOKEN, WORKER_TOKEN}):
    raise RuntimeError("MASTER_TOKEN/ENGINEER_TOKEN and AGENT_TOKEN/WORKER_TOKEN must be different")
if len(MASTER_TOKEN) < 16 or len(AGENT_TOKEN) < 16:
    logging.getLogger("bridge").warning(
        "Tokens shorter than 16 chars are easy to brute-force; "
        "use `openssl rand -hex 32` to generate strong ones."
    )


def _parse_cors_origins() -> list[str]:
    raw = os.getenv("CORS_ALLOW_ORIGINS", "*")
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    return origins or ["*"]


CORS_ALLOW_ORIGINS = _parse_cors_origins()

MAX_PAGE_SIZE = 1000
BRIDGE_API_VERSION = HUB_VERSION
TYPED_MESSAGE_VERSIONS = [1]
TYPED_FEATURES = {
    "idempotency": True,
    "correlation": True,
    "reply_linkage": True,
    "outbox_safe_retry": True,
    "orders_and_executions": True,
    "mcp": True,
    "mcp_events": True,
    "fts_search": True,
    "long_polling": True,
    "channels_and_dms": True,
    "mentions": True,
    "file_transfers": True,
    "signals": True,
    "wakes": True,
    "retention": True,
}


def _env_int(name: str, default: int, minimum: int, maximum: int | None = None) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be an integer; got {raw!r}") from exc
    if value < minimum or (maximum is not None and value > maximum):
        upper = f" and <= {maximum}" if maximum is not None else ""
        raise RuntimeError(f"{name} must be >= {minimum}{upper}; got {value}")
    return value


def _env_float(
    name: str,
    default: float,
    minimum: float,
    maximum: float | None = None,
    *,
    minimum_inclusive: bool = False,
) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be a number; got {raw!r}") from exc
    below_minimum = value < minimum if minimum_inclusive else value <= minimum
    if not math.isfinite(value) or below_minimum or (maximum is not None and value > maximum):
        upper = f" and <= {maximum}" if maximum is not None else ""
        comparator = ">=" if minimum_inclusive else ">"
        raise RuntimeError(f"{name} must be {comparator} {minimum}{upper}; got {value}")
    return value


PORT = _env_int("PORT", 8000, 1, 65535)
RATE_LIMIT_POSTS = _env_int("RATE_LIMIT_POSTS", 60, 1)
RATE_LIMIT_WINDOW = _env_float("RATE_LIMIT_WINDOW", 60, 0)
DEFAULT_PAGE_SIZE = _env_int("DEFAULT_PAGE_SIZE", 200, 1, MAX_PAGE_SIZE)
SSE_QUEUE_SIZE = _env_int("SSE_QUEUE_SIZE", 256, 16, 10000)
SSE_TICKET_TTL = _env_float("SSE_TICKET_TTL", 30, 1, 300, minimum_inclusive=True)
SSE_TICKET_RATE_LIMIT = _env_int("SSE_TICKET_RATE_LIMIT", 60, 1)
SSE_TICKET_RATE_WINDOW = _env_float("SSE_TICKET_RATE_WINDOW", 60, 0)
MAX_TYPED_ENVELOPE_BYTES = _env_int("MAX_TYPED_ENVELOPE_BYTES", 65536, 1)
MAX_TRANSFER_BYTES = _env_int("MAX_TRANSFER_BYTES", 100 * 1024 * 1024, 1024)
TRANSFER_TTL_DAYS = _env_int("TRANSFER_TTL_DAYS", 30, 1, 365)
TRANSFER_TICKET_TTL = _env_float("TRANSFER_TICKET_TTL", 3600.0, 10.0, 86400.0, minimum_inclusive=True)
ALLOW_QUERY_TOKEN = os.getenv("ALLOW_QUERY_TOKEN", "1") != "0"
APPEND_ONLY_MODE = os.getenv("APPEND_ONLY_MODE", "0") == "1"
RETENTION_DAYS = _env_int("RETENTION_DAYS", 0, 0)
MAX_LONG_POLL_SECONDS = _env_float(
    "MAX_LONG_POLL_SECONDS", 50.0, 1.0, 120.0, minimum_inclusive=True
)
STUCK_READER_THRESHOLD = _env_int("STUCK_READER_THRESHOLD", 5, 2)
ENABLE_LOCAL_WAKE_COMMANDS = os.getenv("ENABLE_LOCAL_WAKE_COMMANDS", "0") == "1"
WAKE_ALLOW_PRIVATE_URLS = os.getenv("WAKE_ALLOW_PRIVATE_URLS", "0") == "1"
WAKE_HOURLY_BUDGET = _env_int("WAKE_HOURLY_BUDGET", 60, 1)
WAKE_DAILY_BUDGET = _env_int("WAKE_DAILY_BUDGET", 480, 1)
WAKE_TIMEOUT_SECONDS = _env_float("WAKE_TIMEOUT_SECONDS", 5.0, 0.5, 30.0, minimum_inclusive=True)

_notifier = MessageNotifier()
_stuck_readers = StuckReadersTracker(threshold=STUCK_READER_THRESHOLD)
_transfer_tickets = TransferTicketStore()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("bridge")

# ─── Rate limiting (in-memory sliding window) ────────────────────────────────
_post_timestamps: dict[str, deque[float]] = defaultdict(deque)
_rl_lock = asyncio.Lock()
_sse_ticket_timestamps: dict[str, deque[float]] = defaultdict(deque)
_sse_ticket_rl_lock = asyncio.Lock()


def _retry_after_seconds(window: deque[float], now: float, window_seconds: float) -> int:
    if not window:
        return 1
    elapsed = max(0.0, now - window[0])
    remaining = max(0.0, window_seconds - elapsed)
    ceiling = max(1, math.ceil(window_seconds))
    return min(ceiling, max(1, math.ceil(remaining)))


async def check_rate_limit(key: str) -> None:
    now = time.monotonic()
    async with _rl_lock:
        window = _post_timestamps[key]
        while window and now - window[0] > RATE_LIMIT_WINDOW:
            window.popleft()
        if len(window) >= RATE_LIMIT_POSTS:
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded, slow down",
                headers={
                    "Retry-After": str(_retry_after_seconds(window, now, RATE_LIMIT_WINDOW))
                },
            )
        window.append(now)


async def check_sse_ticket_rate_limit(role: str) -> None:
    now = time.monotonic()
    async with _sse_ticket_rl_lock:
        window = _sse_ticket_timestamps[role]
        while window and now - window[0] > SSE_TICKET_RATE_WINDOW:
            window.popleft()
        if len(window) >= SSE_TICKET_RATE_LIMIT:
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded, slow down",
                headers={
                    "Retry-After": str(
                        _retry_after_seconds(window, now, SSE_TICKET_RATE_WINDOW)
                    )
                },
            )
        window.append(now)


# ─── SSE subscribers ──────────────────────────────────────────────────────────
_subscribers: set[asyncio.Queue] = set()
_sse_tickets: dict[str, tuple[str, float]] = {}
_sse_ticket_lock = asyncio.Lock()
SSE_DISCONNECT = object()


def create_sse_queue() -> asyncio.Queue:
    return asyncio.Queue(maxsize=SSE_QUEUE_SIZE)


def _purge_sse_tickets(now: float) -> None:
    for ticket, (_, expires_at) in list(_sse_tickets.items()):
        if expires_at <= now:
            _sse_tickets.pop(ticket, None)


async def issue_sse_ticket(role: str) -> tuple[str, float]:
    now = time.monotonic()
    ticket = secrets.token_urlsafe(32)
    expires_at = now + SSE_TICKET_TTL
    async with _sse_ticket_lock:
        _purge_sse_tickets(now)
        _sse_tickets[ticket] = (role, expires_at)
    return ticket, SSE_TICKET_TTL


async def resolve_sse_ticket(ticket: str) -> str:
    now = time.monotonic()
    async with _sse_ticket_lock:
        _purge_sse_tickets(now)
        entry = _sse_tickets.pop(ticket, None)
    if entry is None:
        raise HTTPException(status_code=401, detail="Invalid or expired SSE ticket")
    return entry[0]


def disconnect_slow_subscriber(queue: asyncio.Queue) -> None:
    _subscribers.discard(queue)
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            break
    try:
        queue.put_nowait(SSE_DISCONNECT)
    except asyncio.QueueFull:
        pass


def broadcast(message: "MessageOut") -> None:
    _notifier.notify()
    for q in list(_subscribers):
        try:
            q.put_nowait(message)
        except asyncio.QueueFull:
            disconnect_slow_subscriber(q)
        except Exception:
            disconnect_slow_subscriber(q)


# ─── Models ───────────────────────────────────────────────────────────────────
VALID_MESSAGE_TYPES = frozenset({
    "ORDER",
    "EXECUTION",
    "STATUS",
    "PROGRESS",
    "RESULT",
    "BLOCKER",
    "INSTRUCTION",
    "DECISION",
    "CANCEL",
    "MESSAGE",
    "GENERAL",
    # Backward compatibility aliases:
    "TASK",
    "DECISION_NEEDED",
    "DECISION_RESOLVED",
    "DECISION_TAKEN",
    "BLOCKED",
    "REVIEW",
    "ACTION_REQUIRED",
    "APPROVAL_REQUIRED",
    "AUTHORITY_REQUIRED",
    "ACTION_RECONCILIATION_REQUIRED",
    "ACTION_RECONCILED",
    "DIAGNOSTIC",
    "POLICY_BLOCKED",
})

VALID_STATUSES = frozenset({
    "PENDING",
    "RUNNING",
    "COMPLETED",
    "FAILED",
    "BLOCKED",
    "CANCELLED",
    "CANCELED",
    "CANCEL",
})


def normalize_optional_reference(value: Any) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    if not stripped:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in stripped):
        raise ValueError("metadata references must contain printable characters only")
    return stripped


VALID_PRIORITIES = frozenset({"NORMAL", "URGENT"})


class MessageCreate(BaseModel):
    token: str = ""
    content: str = Field(..., min_length=1, max_length=50000)
    order_id: str | None = Field(default=None, min_length=1, max_length=200)
    task_id: str | None = Field(default=None, min_length=1, max_length=200)
    message_type: str | None = Field(default=None, min_length=1, max_length=40)
    status: str | None = Field(default=None, min_length=1, max_length=40)
    message_key: str | None = Field(default=None, min_length=4, max_length=200)
    reply_to_id: int | None = Field(default=None, ge=1)
    payload: dict[str, Any] | None = None
    typed: Any | None = None
    conversation_id: str | None = Field(default=None, min_length=1, max_length=100)
    to_participant_id: str | None = Field(default=None, min_length=1, max_length=100)
    participant_id: str | None = Field(default=None, min_length=1, max_length=100)
    priority: Literal["NORMAL", "URGENT"] | None = None
    transfer_id: str | None = Field(default=None, min_length=1, max_length=100)

    # Legacy fields
    action_id: str | None = Field(default=None, min_length=1, max_length=200)
    approval_id: str | None = Field(default=None, min_length=1, max_length=200)
    next_action: str | None = Field(default=None, min_length=1, max_length=100)
    reason_code: str | None = Field(default=None, min_length=1, max_length=160)

    @field_validator(
        "order_id",
        "task_id",
        "action_id",
        "approval_id",
        "next_action",
        "reason_code",
        "message_key",
        "conversation_id",
        "to_participant_id",
        "participant_id",
        "transfer_id",
        mode="before",
    )
    @classmethod
    def normalize_references(cls, value):
        return normalize_optional_reference(value)

    @field_validator("message_type", mode="before")
    @classmethod
    def normalize_message_type(cls, value):
        normalized = normalize_optional_reference(value)
        return normalized.upper() if normalized else None

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value):
        normalized = normalize_optional_reference(value)
        return normalized.upper() if normalized else None

    @field_validator("priority", mode="before")
    @classmethod
    def normalize_priority(cls, value):
        normalized = normalize_optional_reference(value)
        return normalized.upper() if normalized else None


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    created_at: float
    order_id: str | None = None
    task_id: str | None = None
    message_type: str | None = None
    status: str | None = None
    reply_to_id: int | None = None
    message_key: str | None = None
    payload: dict[str, Any] | None = None
    typed: dict[str, Any] | None = None
    typed_integrity: Literal["INVALID", "NOT_APPLICABLE", "VALID"] = "NOT_APPLICABLE"
    typed_error: dict[str, str] | None = None
    conversation_id: str | None = "general"
    sender_participant_id: str | None = None
    sender_name: str | None = None
    priority: Literal["NORMAL", "URGENT"] = "NORMAL"
    mentions: list[dict[str, str]] = Field(default_factory=list)
    transfer_id: str | None = None
    is_system_notice: bool = False
    deleted_at: float | None = None

    # Legacy compatibility fields
    action_id: str | None = None
    approval_id: str | None = None
    next_action: str | None = None
    reason_code: str | None = None


class OrderCreate(BaseModel):
    token: str = ""
    order_id: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., min_length=1, max_length=50000)
    status: str = Field(default="PENDING", min_length=1, max_length=40)
    message_key: str | None = Field(default=None, min_length=4, max_length=200)
    payload: dict[str, Any] | None = None
    reply_to_id: int | None = Field(default=None, ge=1)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=100)
    to_participant_id: str | None = Field(default=None, min_length=1, max_length=100)
    participant_id: str | None = Field(default=None, min_length=1, max_length=100)
    priority: Literal["NORMAL", "URGENT"] | None = None

    @field_validator("order_id", "message_key", "conversation_id", "to_participant_id", "participant_id", mode="before")
    @classmethod
    def normalize_refs(cls, value):
        return normalize_optional_reference(value)

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value):
        norm = normalize_optional_reference(value)
        return norm.upper() if norm else "PENDING"

    @field_validator("priority", mode="before")
    @classmethod
    def normalize_priority(cls, value):
        norm = normalize_optional_reference(value)
        return norm.upper() if norm else None


class ExecutionCreate(BaseModel):
    token: str = ""
    order_id: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., min_length=1, max_length=50000)
    status: str = Field(default="COMPLETED", min_length=1, max_length=40)
    reply_to_id: int | None = Field(default=None, ge=1)
    message_key: str | None = Field(default=None, min_length=4, max_length=200)
    payload: dict[str, Any] | None = None
    improvements: list[str] | str | None = None
    issues: list[str] | str | None = None
    action_items: list[str] | str | None = None
    conversation_id: str | None = Field(default=None, min_length=1, max_length=100)
    to_participant_id: str | None = Field(default=None, min_length=1, max_length=100)
    participant_id: str | None = Field(default=None, min_length=1, max_length=100)
    priority: Literal["NORMAL", "URGENT"] | None = None

    @field_validator("order_id", "message_key", "conversation_id", "to_participant_id", "participant_id", mode="before")
    @classmethod
    def normalize_refs(cls, value):
        return normalize_optional_reference(value)

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value):
        norm = normalize_optional_reference(value)
        return norm.upper() if norm else "COMPLETED"

    @field_validator("priority", mode="before")
    @classmethod
    def normalize_priority(cls, value):
        norm = normalize_optional_reference(value)
        return norm.upper() if norm else None


class ParticipantCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    role: str | None = None
    kind: Literal["agent", "person"] = "agent"
    agent_type: str = Field(default="generic", min_length=1, max_length=64)
    working_directory: str | None = Field(default=None, max_length=2048)
    joined_note: str | None = Field(default=None, max_length=1024)
    plugin_name: str | None = Field(default=None, max_length=64)
    plugin_version: str | None = Field(default=None, max_length=64)
    wake_cadence: Literal["right_away", "every_minute", "every_5_minutes", "every_15_minutes"] = "every_minute"
    request_key: str | None = Field(default=None, min_length=1, max_length=128)
    issue_token: bool = True


class ParticipantPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    working_directory: str | None = Field(default=None, max_length=2048)
    joined_note: str | None = Field(default=None, max_length=1024)
    plugin_name: str | None = Field(default=None, max_length=64)
    plugin_version: str | None = Field(default=None, max_length=64)
    wake_cadence: Literal["right_away", "every_minute", "every_5_minutes", "every_15_minutes"] | None = None


class ChannelCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=1024)
    participant_id: str | None = Field(default=None, min_length=1, max_length=100)


class ChannelArchiveUpdate(BaseModel):
    archived: bool
    expected_revision: int | None = Field(default=None, ge=0)


class DirectChatCreate(BaseModel):
    participant_id: str | None = Field(default=None, min_length=1, max_length=100)
    target_participant_id: str = Field(..., min_length=1, max_length=100)


class SignalCreate(BaseModel):
    participant_id: str | None = Field(default=None, min_length=1, max_length=100)
    conversation_ids: list[str] | None = None
    include_direct: bool = True
    after_id: int | None = Field(default=None, ge=0)
    ttl_seconds: float = Field(default=86400.0, ge=60.0, le=604800.0)


class SignalAck(BaseModel):
    after_id: int = Field(..., ge=0)


class TransferPrepare(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    kind: Literal["file", "folder"] = "file"
    size_bytes: int = Field(..., ge=1)
    sha256: str = Field(..., min_length=64, max_length=64)
    content_type: str | None = Field(default=None, max_length=200)
    file_count: int | None = Field(default=None, ge=0)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=100)
    participant_id: str | None = Field(default=None, min_length=1, max_length=100)


class TransferComplete(BaseModel):
    content: str | None = Field(default=None, max_length=50000)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=100)
    to_participant_id: str | None = Field(default=None, min_length=1, max_length=100)
    participant_id: str | None = Field(default=None, min_length=1, max_length=100)
    order_id: str | None = Field(default=None, min_length=1, max_length=200)
    message_key: str | None = Field(default=None, min_length=4, max_length=200)


class TransferDownloadTicketRequest(BaseModel):
    participant_id: str | None = Field(default=None, min_length=1, max_length=100)


class WakeCreate(BaseModel):
    participant_id: str = Field(..., min_length=1, max_length=100)
    kind: Literal["webhook", "claude_routine", "local_command"] = "webhook"
    url: str | None = Field(default=None, max_length=2048)
    command: list[str] | str | None = None
    secret: str | None = Field(default=None, max_length=512)
    channel_ids: list[str] | None = None
    direct_chats: Literal["own", "none"] = "own"
    debounce_seconds: float = Field(default=10.0, ge=0.0, le=3600.0)
    min_interval_seconds: float = Field(default=60.0, ge=0.0, le=86400.0)
    max_attempts: int = Field(default=5, ge=1, le=20)


class RetentionPruneRequest(BaseModel):
    older_than_days: float | None = Field(default=None, gt=0.0)
    before_id: int | None = Field(default=None, ge=1)


class ImprovementCreate(BaseModel):
    token: str = ""
    task_name: str | None = Field(default=None, max_length=200)
    summary: str = Field(default="", max_length=10000)
    improvements: list[str] | str | None = None
    issues: list[str] | str | None = None
    action_items: list[str] | str | None = None
    raw_content: str | None = Field(default=None, max_length=50000)
    status: str = Field(default="COMPLETED", min_length=1, max_length=40)

    @field_validator("task_name", mode="before")
    @classmethod
    def validate_task_name(cls, value: Any) -> str | None:
        if value is None:
            return None
        val_str = str(value).strip()
        if not val_str:
            return None
        if len(val_str) > 200:
            raise ValueError("task_name exceeds maximum length")
        # Strict validation of safe characters (alphanumeric, hyphens, underscores, dots, spaces)
        if not all(c.isascii() and (c.isalnum() or c in ("_", "-", ".", " ")) for c in val_str):
            raise ValueError("task_name contains invalid characters")
        return val_str[:60]

    @field_validator("status", mode="before")
    @classmethod
    def normalize_improvement_status(cls, value: Any) -> str:
        norm = normalize_optional_reference(value)
        return norm.upper() if norm else "COMPLETED"

    @field_validator("improvements", "issues", "action_items")
    @classmethod
    def validate_improvement_items(cls, value: list[str] | str | None) -> list[str] | str | None:
        if value is None:
            return None
        if isinstance(value, str):
            if len(value) > 20000:
                raise ValueError("field exceeds maximum length of 20000 characters")
            return value
        if len(value) > 100:
            raise ValueError("field cannot contain more than 100 items")
        if any(len(item) > 2000 for item in value):
            raise ValueError("individual items must be at most 2000 characters")
        return value




# ─── Auth helpers ─────────────────────────────────────────────────────────────
@dataclass
class AuthContext:
    role: str
    participant_id: str | None = None
    participant_name: str | None = None
    participant_kind: str | None = None
    agent_type: str | None = None
    used_query_token: bool = False


def _resolve_static_role(token: str) -> str | None:
    try:
        candidate = token.encode("utf-8")
    except UnicodeEncodeError as exc:
        logger.warning("Rejected invalid authentication token")
        raise HTTPException(status_code=401, detail="Invalid token") from exc
    if MASTER_TOKEN and secrets.compare_digest(candidate, MASTER_TOKEN.encode("utf-8")):
        return "master"
    if AGENT_TOKEN and secrets.compare_digest(candidate, AGENT_TOKEN.encode("utf-8")):
        return "agent"
    if ENGINEER_TOKEN and secrets.compare_digest(candidate, ENGINEER_TOKEN.encode("utf-8")):
        return "master"
    if WORKER_TOKEN and secrets.compare_digest(candidate, WORKER_TOKEN.encode("utf-8")):
        return "agent"
    return None


def resolve_role(token: str) -> str:
    """Resolve static token to role ('master' or 'agent').

    Supports MASTER_TOKEN / AGENT_TOKEN and legacy ENGINEER_TOKEN / WORKER_TOKEN.
    """
    role = _resolve_static_role(token)
    if role is not None:
        return role
    logger.warning("Rejected invalid authentication token")
    raise HTTPException(status_code=401, detail="Invalid token")


def normalize_role(role: str) -> str:
    """Normalize role string to canonical 'master' or 'agent'."""
    r = role.strip().lower()
    if r in ("master", "engineer"):
        return "master"
    if r in ("agent", "worker"):
        return "agent"
    return r


def extract_bearer_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    scheme, sep, value = auth.partition(" ")
    if sep and scheme.lower() == "bearer":
        return value.strip()
    return ""


async def resolve_token_auth(token: str, *, used_query_token: bool = False) -> AuthContext:
    static_role = _resolve_static_role(token)
    if static_role is not None:
        return AuthContext(role=static_role, used_query_token=used_query_token)

    try:
        token_hash = hash_secret(token)
    except UnicodeEncodeError as exc:
        logger.warning("Rejected invalid authentication token")
        raise HTTPException(status_code=401, detail="Invalid token") from exc

    async with connect_db() as db:
        try:
            cur = await db.execute(
                "SELECT id, name, role, kind, agent_type, removed_at FROM participants WHERE token_hash = ?",
                (token_hash,),
            )
            row = await cur.fetchone()
        except Exception:
            row = None
        if row is not None:
            if row["removed_at"] is not None:
                raise HTTPException(status_code=403, detail="Participant has been removed")
            await db.execute(
                "UPDATE participants SET last_seen_at = ? WHERE id = ?",
                (time.time(), row["id"]),
            )
            await db.commit()
            return AuthContext(
                role=normalize_role(row["role"]),
                participant_id=row["id"],
                participant_name=row["name"],
                participant_kind=row["kind"],
                agent_type=row["agent_type"],
                used_query_token=used_query_token,
            )

    logger.warning("Rejected invalid authentication token")
    raise HTTPException(status_code=401, detail="Invalid token")


async def require_auth(
    request: Request,
    token: str | None = None,
    *,
    allow_query_token: bool = False,
) -> AuthContext:
    bearer = extract_bearer_token(request)
    if bearer:
        return await resolve_token_auth(bearer, used_query_token=False)

    if token:
        if not allow_query_token or not ALLOW_QUERY_TOKEN:
            raise HTTPException(
                status_code=401,
                detail="Query/body token authentication is disabled; use Authorization: Bearer",
            )
        logger.warning("Deprecated query/body token authentication used; migrate to Authorization: Bearer")
        state = getattr(request, "state", None)
        if state is not None:
            state.used_query_token = True
        return await resolve_token_auth(token, used_query_token=True)

    raise HTTPException(status_code=401, detail="Missing token")


async def require_reader(request: Request, token: str | None) -> str:
    auth = await require_auth(request, token, allow_query_token=True)
    return auth.role


# ─── Database ─────────────────────────────────────────────────────────────────
@asynccontextmanager
async def connect_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH, timeout=10) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("PRAGMA synchronous=NORMAL")
        yield conn
        if conn.in_transaction:
            await conn.commit()


_wake_scheduler = WakeScheduler(
    connect_db,
    allow_local_commands=lambda: ENABLE_LOCAL_WAKE_COMMANDS,
    allow_private_urls=lambda: WAKE_ALLOW_PRIVATE_URLS,
    hourly_budget=lambda: WAKE_HOURLY_BUDGET,
    daily_budget=lambda: WAKE_DAILY_BUDGET,
    timeout_seconds=lambda: WAKE_TIMEOUT_SECONDS,
)

_mcp_event_webhooks = MCPEventWebhookManager(
    connect_db,
    allow_private_urls=lambda: WAKE_ALLOW_PRIVATE_URLS,
    timeout_seconds=lambda: WAKE_TIMEOUT_SECONDS,
)


async def init_db():
    async with connect_db() as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                order_id TEXT,
                task_id TEXT,
                message_type TEXT,
                status TEXT,
                action_id TEXT,
                approval_id TEXT,
                next_action TEXT,
                reason_code TEXT,
                typed_schema_version INTEGER,
                typed_kind TEXT,
                message_key TEXT,
                correlation_id TEXT,
                reply_to_id INTEGER,
                expects_reply INTEGER,
                typed_payload_json TEXT,
                canonical_refs_json TEXT
            )
        """)
        cursor = await db.execute("PRAGMA table_info(messages)")
        columns = {row["name"] for row in await cursor.fetchall()}

        schema_columns = {
            "order_id": "TEXT",
            "task_id": "TEXT",
            "message_type": "TEXT",
            "status": "TEXT",
            "action_id": "TEXT",
            "approval_id": "TEXT",
            "next_action": "TEXT",
            "reason_code": "TEXT",
            "typed_schema_version": "INTEGER",
            "typed_kind": "TEXT",
            "message_key": "TEXT",
            "correlation_id": "TEXT",
            "reply_to_id": "INTEGER",
            "expects_reply": "INTEGER",
            "typed_payload_json": "TEXT",
            "canonical_refs_json": "TEXT",
        }
        for col_name, col_type in schema_columns.items():
            if col_name not in columns:
                await db.execute(f"ALTER TABLE messages ADD COLUMN {col_name} {col_type}")

        indexes = [
            ("idx_messages_created_at", "messages(created_at)"),
            ("idx_messages_order_id", "messages(order_id)"),
            ("idx_messages_task_id", "messages(task_id)"),
            ("idx_messages_role", "messages(role)"),
            ("idx_messages_message_type", "messages(message_type)"),
            ("idx_messages_status", "messages(status)"),
            ("idx_messages_action_id", "messages(action_id)"),
            ("idx_messages_approval_id", "messages(approval_id)"),
            ("idx_messages_typed_kind", "messages(typed_kind)"),
            ("idx_messages_correlation_id", "messages(correlation_id)"),
            ("idx_messages_reply_to_id", "messages(reply_to_id)"),
        ]
        for idx_name, idx_target in indexes:
            await db.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {idx_target}")

        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_role_message_key
            ON messages(role, message_key)
            WHERE message_key IS NOT NULL
        """)
        await init_hub_schema(db)
        await db.commit()
    logger.info("Database ready at %s", DB_PATH)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    _wake_scheduler.start()
    _mcp_event_webhooks.start()
    yield
    await _mcp_event_webhooks.stop()
    await _wake_scheduler.stop()
    for q in list(_subscribers):
        disconnect_slow_subscriber(q)
    _subscribers.clear()
    _sse_tickets.clear()
    _sse_ticket_timestamps.clear()
    _notifier.clear()
    _transfer_tickets.clear()
    _stuck_readers.clear()


app = FastAPI(
    title="Master-Agent Bridge",
    description="High-reliability communication hub for orders, executions, channels, MCP, file transfers, and wakes",
    version=BRIDGE_API_VERSION,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    state = getattr(request, "state", None)
    if state is not None and getattr(state, "used_query_token", False):
        response.headers.setdefault("Deprecation", "true")
    return response


@app.exception_handler(HubServiceError)
async def hub_service_error_handler(_request: Request, exc: HubServiceError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.exception_handler(BridgeProtocolError)
async def bridge_protocol_error_handler(_request: Request, exc: BridgeProtocolError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


MESSAGE_SELECT = """
    id, role, content, created_at, order_id, task_id, message_type, status,
    action_id, approval_id, next_action, reason_code,
    typed_schema_version, typed_kind, message_key, correlation_id,
    reply_to_id, expects_reply, typed_payload_json, canonical_refs_json,
    conversation_id, sender_participant_id, sender_name, priority,
    mentions_json, transfer_id, is_system_notice, deleted_at
"""


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _serialize_legacy_payload(payload: dict[str, Any]) -> str:
    try:
        serialized = _json_dumps(payload)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail="payload must be valid JSON without NaN or Infinity",
        ) from exc
    size = len(serialized.encode("utf-8"))
    if size > MAX_TYPED_ENVELOPE_BYTES:
        raise BridgeProtocolError(
            E_BRIDGE_TYPED_PAYLOAD_TOO_LARGE,
            f"payload is {size} bytes; maximum is {MAX_TYPED_ENVELOPE_BYTES}",
            status_code=413,
        )
    return serialized


def _typed_envelope_size(envelope) -> int:
    return len(_json_dumps(envelope_to_dict(envelope)).encode("utf-8"))


def _validate_typed_envelope_size(envelope) -> None:
    size = _typed_envelope_size(envelope)
    if size > MAX_TYPED_ENVELOPE_BYTES:
        raise BridgeProtocolError(
            E_BRIDGE_TYPED_PAYLOAD_TOO_LARGE,
            f"typed envelope is {size} bytes; maximum is {MAX_TYPED_ENVELOPE_BYTES}",
            status_code=413,
        )


def _typed_storage_values(envelope) -> tuple[Any, ...]:
    if envelope is None:
        return (None, None, None, None, None, None, None, None)
    return (
        envelope.schema_version,
        envelope.kind,
        envelope.message_key,
        envelope.correlation_id,
        envelope.reply_to_id,
        int(envelope.expects_reply),
        _json_dumps(envelope.payload.model_dump(mode="json", exclude_none=False)),
        _json_dumps(
            [reference.model_dump(mode="json", exclude_none=False) for reference in envelope.canonical_refs]
        ),
    )


def _typed_row_state(row) -> tuple[Any, str, dict[str, str] | None]:
    data = dict(row)
    if data.get("typed_schema_version") is None:
        # Keys, replies, and payload JSON are shared with the legacy transport.
        if any(data.get(field) is not None for field in (
            "typed_kind", "correlation_id", "expects_reply", "canonical_refs_json",
        )):
            return (
                None,
                "INVALID",
                {
                    "code": E_BRIDGE_PERSISTED_TYPED_INVALID,
                    "message": "Persisted typed representation failed validation.",
                },
            )
        return None, "NOT_APPLICABLE", None

    try:
        payload = json.loads(data["typed_payload_json"])
        canonical_refs = json.loads(data["canonical_refs_json"]) if data.get("canonical_refs_json") else []
        stored_expects_reply = data["expects_reply"]
        if isinstance(stored_expects_reply, bool):
            expects_reply = stored_expects_reply
        elif isinstance(stored_expects_reply, int) and stored_expects_reply in {0, 1}:
            expects_reply = bool(stored_expects_reply)
        else:
            raise ValueError("persisted expects_reply must be a boolean integer")
        raw = {
            "schema_version": data["typed_schema_version"],
            "kind": data["typed_kind"],
            "message_key": data["message_key"],
            "correlation_id": data["correlation_id"],
            "reply_to_id": data["reply_to_id"],
            "expects_reply": expects_reply,
            "payload": payload,
            "canonical_refs": canonical_refs,
        }
        return parse_typed_envelope(raw), "VALID", None
    except Exception:
        return (
            None,
            "INVALID",
            {
                "code": E_BRIDGE_PERSISTED_TYPED_INVALID,
                "message": "Persisted typed representation failed validation.",
            },
        )


def _typed_envelope_from_row(row):
    return _typed_row_state(row)[0]


def _parse_mentions_json(raw: Any) -> list[dict[str, str]]:
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
        if isinstance(decoded, list):
            return [
                {"id": str(item["id"]), "name": str(item["name"])}
                for item in decoded
                if isinstance(item, dict) and "id" in item and "name" in item
            ]
    except Exception:
        pass
    return []


def _message_out_from_row(row) -> MessageOut:
    data = dict(row)
    envelope, typed_integrity, typed_error = _typed_row_state(data)
    order_id = data.get("order_id") or data.get("task_id")
    task_id = data.get("task_id") or data.get("order_id")

    payload_val = None
    if envelope is not None:
        payload_val = envelope.payload.model_dump(mode="json", exclude_none=False)
    elif data.get("typed_payload_json"):
        try:
            decoded = json.loads(data["typed_payload_json"])
            if isinstance(decoded, dict):
                payload_val = decoded
        except Exception:
            pass

    raw_priority = (data.get("priority") or "NORMAL").upper()
    priority: Literal["NORMAL", "URGENT"] = "URGENT" if raw_priority == "URGENT" else "NORMAL"

    return MessageOut(
        id=int(data["id"]),
        role=data["role"],
        content=data["content"],
        created_at=data["created_at"],
        order_id=order_id,
        task_id=task_id,
        message_type=data.get("message_type"),
        status=data.get("status"),
        reply_to_id=data.get("reply_to_id"),
        message_key=data.get("message_key"),
        payload=payload_val,
        typed=envelope_to_dict(envelope) if envelope is not None else None,
        typed_integrity=typed_integrity,
        typed_error=typed_error,
        conversation_id=data.get("conversation_id") or GENERAL_CONVERSATION_ID,
        sender_participant_id=data.get("sender_participant_id"),
        sender_name=data.get("sender_name"),
        priority=priority,
        mentions=_parse_mentions_json(data.get("mentions_json")),
        transfer_id=data.get("transfer_id"),
        is_system_notice=bool(data.get("is_system_notice")),
        deleted_at=data.get("deleted_at"),
        action_id=data.get("action_id"),
        approval_id=data.get("approval_id"),
        next_action=data.get("next_action"),
        reason_code=data.get("reason_code"),
    )


def _submission_fingerprint(
    *,
    content: str,
    order_id: str | None,
    task_id: str | None,
    message_type: str | None,
    status: str | None,
    action_id: str | None,
    approval_id: str | None,
    next_action: str | None,
    reason_code: str | None,
    envelope,
    reply_to_id: int | None = None,
    payload: dict[str, Any] | None = None,
    conversation_id: str | None = None,
    priority: str | None = None,
    transfer_id: str | None = None,
) -> str:
    return _json_dumps(
        {
            "content": content,
            "order_id": order_id,
            "task_id": task_id,
            "message_type": message_type,
            "status": status,
            "action_id": action_id,
            "approval_id": approval_id,
            "next_action": next_action,
            "reason_code": reason_code,
            "typed": envelope_to_dict(envelope) if envelope is not None else None,
            "reply_to_id": reply_to_id if envelope is None else None,
            "payload": payload if envelope is None else None,
            "conversation_id": conversation_id or GENERAL_CONVERSATION_ID,
            "priority": (priority or "NORMAL").upper(),
            "transfer_id": transfer_id,
        }
    )


def _row_submission_fingerprint(row) -> str | None:
    data = dict(row)
    envelope, typed_integrity, _typed_error = _typed_row_state(data)
    if typed_integrity == "INVALID" or (
        data.get("message_key") is not None and envelope is None and data.get("typed_schema_version") is not None
    ):
        return None
    payload = None
    if envelope is None and data.get("typed_payload_json") is not None:
        try:
            payload = json.loads(data["typed_payload_json"])
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
    return _submission_fingerprint(
        content=data["content"],
        order_id=data.get("order_id"),
        task_id=data.get("task_id"),
        message_type=data.get("message_type"),
        status=data.get("status"),
        action_id=data.get("action_id"),
        approval_id=data.get("approval_id"),
        next_action=data.get("next_action"),
        reason_code=data.get("reason_code"),
        envelope=envelope,
        reply_to_id=data.get("reply_to_id"),
        payload=payload,
        conversation_id=data.get("conversation_id") or GENERAL_CONVERSATION_ID,
        priority=data.get("priority") or "NORMAL",
        transfer_id=data.get("transfer_id"),
    )


async def _find_message_by_id(db, message_id: int):
    cursor = await db.execute(f"SELECT {MESSAGE_SELECT} FROM messages WHERE id = ?", (message_id,))
    return await cursor.fetchone()


async def _find_message_by_key(db, role: str, message_key: str):
    cursor = await db.execute(
        f"SELECT {MESSAGE_SELECT} FROM messages WHERE role = ? AND message_key = ?",
        (role, message_key),
    )
    return await cursor.fetchone()


# ─── API Endpoints ────────────────────────────────────────────────────────────
async def _resolve_message_query_params(
    *,
    order_id: str | None,
    task_id: str | None,
    role: str | None,
    role_filter: str | None,
    message_type: str | None,
    status: str | None,
    action_id: str | None,
    approval_id: str | None,
    typed_kind: str | None,
    correlation_id: str | None,
    conversation_id: str | None,
    participant_id: str | None,
    priority: str | None,
    q: str | None,
) -> dict[str, Any]:
    resolved_order_id = normalize_optional_reference(order_id or task_id)
    norm_type = normalize_optional_reference(message_type)
    norm_type = norm_type.upper() if norm_type else None
    norm_status = normalize_optional_reference(status)
    norm_status = norm_status.upper() if norm_status else None
    norm_action = normalize_optional_reference(action_id)
    norm_approval = normalize_optional_reference(approval_id)
    norm_typed_kind = normalize_optional_reference(typed_kind)
    norm_typed_kind = norm_typed_kind.upper() if norm_typed_kind else None
    norm_correlation = normalize_optional_reference(correlation_id)
    effective_role = normalize_optional_reference(role or role_filter)
    effective_role = effective_role.lower() if effective_role else None
    norm_conv = normalize_optional_reference(conversation_id)
    norm_pid = normalize_optional_reference(participant_id)
    norm_priority = normalize_optional_reference(priority)
    norm_priority = norm_priority.upper() if norm_priority else None
    if norm_priority is not None and norm_priority not in VALID_PRIORITIES:
        raise ValueError(f"priority must be one of {sorted(VALID_PRIORITIES)}")
    norm_q = q.strip() if q and q.strip() else None
    return {
        "order_id": resolved_order_id,
        "message_type": norm_type,
        "status": norm_status,
        "action_id": norm_action,
        "approval_id": norm_approval,
        "typed_kind": norm_typed_kind,
        "correlation_id": norm_correlation,
        "role": effective_role,
        "conversation_id": norm_conv,
        "participant_id": norm_pid,
        "priority": norm_priority,
        "q": norm_q,
    }


def _build_messages_sql(
    filters: dict[str, Any],
    *,
    reply_to_id: int | None,
    after_id: int | None,
    before_id: int | None,
    latest: bool,
    include_deleted: bool,
    limit: int,
) -> tuple[str, tuple[Any, ...], bool]:
    select_cols = ", ".join(f"m.{field.strip()}" for field in MESSAGE_SELECT.split(","))
    from_sql = "messages m"
    clauses: list[str] = []
    params: list[Any] = []

    if filters["q"] is not None:
        fts_expr = sanitize_fts_query(filters["q"])
        if not fts_expr:
            return "", (), True
        from_sql += " JOIN messages_fts ON messages_fts.rowid = m.id"
        clauses.append("messages_fts MATCH ?")
        params.append(fts_expr)

    if not include_deleted:
        clauses.append("m.deleted_at IS NULL")
    if filters["order_id"] is not None:
        clauses.append("(m.order_id = ? OR m.task_id = ?)")
        params.extend([filters["order_id"], filters["order_id"]])
    if filters["role"] is not None:
        norm_filter = normalize_role(filters["role"])
        if norm_filter == "master":
            clauses.append("m.role IN ('master', 'engineer')")
        elif norm_filter == "agent":
            clauses.append("m.role IN ('agent', 'worker')")
        else:
            clauses.append("m.role = ?")
            params.append(filters["role"])
    if filters["message_type"] is not None:
        clauses.append("m.message_type = ?")
        params.append(filters["message_type"])
    if filters["status"] is not None:
        clauses.append("m.status = ?")
        params.append(filters["status"])
    if filters["action_id"] is not None:
        clauses.append("m.action_id = ?")
        params.append(filters["action_id"])
    if filters["approval_id"] is not None:
        clauses.append("m.approval_id = ?")
        params.append(filters["approval_id"])
    if filters["typed_kind"] is not None:
        clauses.append("m.typed_kind = ?")
        params.append(filters["typed_kind"])
    if filters["correlation_id"] is not None:
        clauses.append("m.correlation_id = ?")
        params.append(filters["correlation_id"])
    if filters["conversation_id"] is not None:
        clauses.append("COALESCE(m.conversation_id, 'general') = ?")
        params.append(filters["conversation_id"])
    if filters["participant_id"] is not None:
        clauses.append("m.sender_participant_id = ?")
        params.append(filters["participant_id"])
    if filters["priority"] is not None:
        clauses.append("COALESCE(m.priority, 'NORMAL') = ?")
        params.append(filters["priority"])
    if reply_to_id is not None:
        clauses.append("m.reply_to_id = ?")
        params.append(reply_to_id)
    if after_id is not None:
        clauses.append("m.id > ?")
        params.append(after_id)
    if before_id is not None:
        clauses.append("m.id < ?")
        params.append(before_id)

    query = f"SELECT {select_cols} FROM {from_sql}"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)

    if latest or before_id is not None:
        query += " ORDER BY m.id DESC LIMIT ?"
    else:
        query += " ORDER BY m.id ASC LIMIT ?"
    params.append(limit)
    return query, tuple(params), False


@app.get("/api/messages", response_model=list[MessageOut])
async def get_messages(
    request: Request,
    response: Response,
    token: str | None = None,
    order_id: str | None = None,
    task_id: str | None = None,
    role: str | None = None,
    role_filter: str | None = None,
    message_type: str | None = None,
    status: str | None = None,
    action_id: str | None = None,
    approval_id: str | None = None,
    typed_kind: str | None = None,
    correlation_id: str | None = None,
    reply_to_id: int | None = None,
    conversation_id: str | None = None,
    participant_id: str | None = None,
    priority: str | None = None,
    q: str | None = None,
    include_deleted: bool = False,
    generation: str | None = None,
    wait_seconds: float = 0.0,
    after_id: int | None = None,
    before_id: int | None = None,
    latest: bool = False,
    limit: int = DEFAULT_PAGE_SIZE,
):
    """Query messages with optional filtering, FTS5 search, and long-polling. Requires authentication."""
    auth = await require_auth(request, token, allow_query_token=True)
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    clamped_wait = max(0.0, min(float(wait_seconds), MAX_LONG_POLL_SECONDS))

    if latest and (after_id is not None or before_id is not None):
        raise HTTPException(
            status_code=400,
            detail="latest cannot be combined with after_id or before_id",
        )

    try:
        filters = await _resolve_message_query_params(
            order_id=order_id,
            task_id=task_id,
            role=role,
            role_filter=role_filter,
            message_type=message_type,
            status=status,
            action_id=action_id,
            approval_id=approval_id,
            typed_kind=typed_kind,
            correlation_id=correlation_id,
            conversation_id=conversation_id,
            participant_id=participant_id,
            priority=priority,
            q=q,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    query, params, empty_search = _build_messages_sql(
        filters,
        reply_to_id=reply_to_id,
        after_id=after_id,
        before_id=before_id,
        latest=latest,
        include_deleted=include_deleted,
        limit=limit,
    )

    async def _run_query():
        async with connect_db() as db:
            current_gen = await get_db_generation(db)
            if generation is not None and generation != current_gen:
                return current_gen, None, True
            if empty_search:
                return current_gen, [], False
            fetched = await db.execute_fetchall(query, params)
            return current_gen, fetched, False

    current_gen, rows, gen_mismatch = await _run_query()
    if gen_mismatch:
        return JSONResponse(
            status_code=409,
            headers={"X-Bridge-Generation": current_gen},
            content={
                "error": {
                    "code": "E_BRIDGE_GENERATION_MISMATCH",
                    "message": "Database generation changed; reset your cursor to 0.",
                    "current_generation": current_gen,
                }
            },
        )

    can_long_poll = (
        clamped_wait > 0
        and after_id is not None
        and not latest
        and before_id is None
        and filters["q"] is None
        and not rows
    )
    if can_long_poll:
        deadline = time.monotonic() + clamped_wait
        while not rows:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            notified = await _notifier.wait_for_message(remaining, request=request)
            if not notified:
                break
            current_gen, rows, gen_mismatch = await _run_query()
            if gen_mismatch:
                return JSONResponse(
                    status_code=409,
                    headers={"X-Bridge-Generation": current_gen},
                    content={
                        "error": {
                            "code": "E_BRIDGE_GENERATION_MISMATCH",
                            "message": "Database generation changed; reset your cursor to 0.",
                            "current_generation": current_gen,
                        }
                    },
                )

    if after_id is not None and not latest and before_id is None and filters["q"] is None:
        reader_key = auth.participant_id or f"role:{auth.role}"
        _stuck_readers.observe(
            reader_key,
            format_checkpoint(current_gen, after_id),
            len(rows or ()),
        )

    response.headers["X-Bridge-Generation"] = current_gen
    messages = [_message_out_from_row(row) for row in (rows or ())]
    if latest or before_id is not None:
        messages.reverse()
    return messages


async def _create_message_internal(
    msg: MessageCreate,
    auth: AuthContext,
    *,
    is_system_notice: bool = False,
) -> MessageOut:
    role = auth.role
    rate_key = auth.participant_id or role
    await check_rate_limit(rate_key)

    content = msg.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Content cannot be empty")

    order_id = msg.order_id or msg.task_id
    task_id = msg.task_id or msg.order_id

    message_type = None
    if msg.message_type:
        normalized_type = msg.message_type
        if normalized_type not in VALID_MESSAGE_TYPES:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid message_type '{msg.message_type}'. Allowed types: {sorted(VALID_MESSAGE_TYPES)}",
            )
        message_type = normalized_type

    status = msg.status
    if status and status not in VALID_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid status '{status}'. Allowed statuses: {sorted(VALID_STATUSES)}",
        )

    envelope = None
    if msg.typed is not None:
        envelope = parse_typed_envelope(msg.typed)
        validate_legacy_kind_consistency(message_type, envelope)
        _validate_typed_envelope_size(envelope)
        if msg.message_key is not None and msg.message_key != envelope.message_key:
            raise BridgeProtocolError(
                E_BRIDGE_TYPED_PAYLOAD_INVALID,
                "message_key must match typed.message_key",
            )
        if msg.reply_to_id is not None and msg.reply_to_id != envelope.reply_to_id:
            raise BridgeProtocolError(
                E_BRIDGE_TYPED_PAYLOAD_INVALID,
                "reply_to_id must match typed.reply_to_id",
            )

    legacy_payload_json = None
    if envelope is None and msg.payload is not None:
        legacy_payload_json = _serialize_legacy_payload(msg.payload)

    message_key = envelope.message_key if envelope is not None else msg.message_key
    correlation_id = envelope.correlation_id if envelope is not None else None
    reply_to_id = envelope.reply_to_id if envelope is not None else msg.reply_to_id

    priority: Literal["NORMAL", "URGENT"] = "NORMAL"
    if msg.priority is not None:
        if msg.priority not in VALID_PRIORITIES:
            raise HTTPException(status_code=422, detail=f"Invalid priority '{msg.priority}'")
        priority = msg.priority
    elif envelope is not None and getattr(envelope.payload, "priority", None) == "URGENT":
        priority = "URGENT"

    typed_storage_values = _typed_storage_values(envelope)

    async with connect_db() as db:
        sender_pid = auth.participant_id
        sender_name = auth.participant_name
        if msg.participant_id is not None:
            if sender_pid is not None and msg.participant_id != sender_pid:
                raise HubServiceError(
                    "E_HUB_PARTICIPANT_MISMATCH",
                    "participant_id does not match authenticated participant token",
                    status_code=403,
                )
            if sender_pid is None:
                part = await get_participant(db, msg.participant_id)
                if part is None:
                    raise HubServiceError("E_HUB_PARTICIPANT_NOT_FOUND", "Participant not found", status_code=404)
                if part["removed_at"] is not None:
                    raise HubServiceError("E_HUB_PARTICIPANT_REMOVED", "Participant is removed", status_code=403)
                sender_pid = part["id"]
                sender_name = part["name"]

        conv_kind = "channel"
        dm_low_id: str | None = None
        dm_high_id: str | None = None
        if msg.to_participant_id is not None:
            if sender_pid is None:
                raise HubServiceError(
                    "E_HUB_PARTICIPANT_REQUIRED",
                    "Direct messages require a participant token or participant_id",
                    status_code=400,
                )
            target_part = await resolve_participant_by_id_or_name(db, msg.to_participant_id)
            if target_part is None:
                raise HubServiceError("E_HUB_PARTICIPANT_NOT_FOUND", "Target participant not found", status_code=404)
            dm_conv, _ = await open_or_create_direct_chat(
                db,
                participant_a_id=sender_pid,
                participant_b_id=target_part["id"],
            )
            target_conversation_id = dm_conv["id"]
            conv_kind = "direct"
            dm_low_id = dm_conv.get("participant_low_id")
            dm_high_id = dm_conv.get("participant_high_id")
        else:
            requested_conv = msg.conversation_id or GENERAL_CONVERSATION_ID
            conv = await resolve_conversation_by_id_or_name(db, requested_conv)
            if conv is None:
                raise HubServiceError(
                    "E_HUB_CONVERSATION_NOT_FOUND",
                    f"Conversation '{requested_conv}' not found",
                    status_code=404,
                )
            if conv.get("archived_at") is not None:
                raise HubServiceError(
                    "E_HUB_CHANNEL_ARCHIVED",
                    "Cannot post into an archived channel",
                    status_code=409,
                )
            if conv["kind"] == "direct" and sender_pid is not None:
                if sender_pid not in {conv.get("participant_low_id"), conv.get("participant_high_id")}:
                    raise HubServiceError(
                        "E_HUB_DIRECT_FORBIDDEN",
                        "Only the two participants of a direct chat can send into it",
                        status_code=403,
                    )
            target_conversation_id = conv["id"]
            conv_kind = conv["kind"]
            dm_low_id = conv.get("participant_low_id")
            dm_high_id = conv.get("participant_high_id")

        submission_fingerprint = _submission_fingerprint(
            content=content,
            order_id=order_id,
            task_id=task_id,
            message_type=message_type,
            status=status,
            action_id=msg.action_id,
            approval_id=msg.approval_id,
            next_action=msg.next_action,
            reason_code=msg.reason_code,
            envelope=envelope,
            reply_to_id=reply_to_id,
            payload=msg.payload,
            conversation_id=target_conversation_id,
            priority=priority,
            transfer_id=msg.transfer_id,
        )

        if message_key is not None:
            existing = await _find_message_by_key(db, role, message_key)
            if existing is not None:
                existing_fingerprint = _row_submission_fingerprint(existing)
                if existing_fingerprint == submission_fingerprint:
                    return _message_out_from_row(existing)
                raise BridgeProtocolError(
                    E_BRIDGE_IDEMPOTENCY_CONFLICT,
                    "The message key already exists with different content.",
                    status_code=409,
                )

        if envelope is not None:
            target = await _find_message_by_id(db, reply_to_id) if reply_to_id is not None else None
            target_typed = _typed_envelope_from_row(target) if target is not None else None
            validate_reply_relationship(envelope, role, target, target_typed)
        elif reply_to_id is not None:
            target = await _find_message_by_id(db, reply_to_id)
            if target is None:
                raise BridgeProtocolError(
                    E_BRIDGE_REPLY_NOT_FOUND,
                    f"reply target {reply_to_id} was not found",
                )
            if normalize_role(target["role"]) == normalize_role(role):
                raise BridgeProtocolError(
                    E_BRIDGE_REPLY_ROLE_INVALID,
                    "Replies must target a message authored by the opposite role",
                )

        mentions = await resolve_mentions(db, content) if not is_system_notice else []
        mentions_json = _json_dumps(mentions) if mentions else "[]"

        created_at = time.time()
        payload_json = typed_storage_values[6] if envelope is not None else legacy_payload_json

        try:
            cursor = await db.execute(
                """
                INSERT INTO messages (
                    role, content, created_at, order_id, task_id, message_type, status,
                    action_id, approval_id, next_action, reason_code,
                    typed_schema_version, typed_kind, message_key, correlation_id,
                    reply_to_id, expects_reply, typed_payload_json, canonical_refs_json,
                    conversation_id, sender_participant_id, sender_name, priority,
                    mentions_json, transfer_id, is_system_notice, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    role,
                    content,
                    created_at,
                    order_id,
                    task_id,
                    message_type,
                    status,
                    msg.action_id,
                    msg.approval_id,
                    msg.next_action,
                    msg.reason_code,
                    typed_storage_values[0],
                    typed_storage_values[1],
                    message_key,
                    correlation_id,
                    reply_to_id,
                    typed_storage_values[5],
                    payload_json,
                    typed_storage_values[7],
                    target_conversation_id,
                    sender_pid,
                    sender_name,
                    priority,
                    mentions_json,
                    msg.transfer_id,
                    int(bool(is_system_notice)),
                ),
            )
            msg_id = cursor.lastrowid
            if msg_id is None:
                raise HTTPException(status_code=500, detail="Could not allocate message id")

            await db.execute(
                """
                UPDATE conversations
                SET message_count = message_count + 1,
                    last_message_id = ?,
                    last_message_at = ?
                WHERE id = ?
                """,
                (int(msg_id), created_at, target_conversation_id),
            )
            if sender_pid is not None:
                await db.execute(
                    """
                    UPDATE participants
                    SET message_count = message_count + 1,
                        last_message_at = ?,
                        last_seen_at = ?
                    WHERE id = ?
                    """,
                    (created_at, created_at, sender_pid),
                )
            await db.commit()
        except aiosqlite.IntegrityError:
            await db.rollback()
            if message_key is None:
                raise
            existing = await _find_message_by_key(db, role, message_key)
            if existing is None:
                raise
            existing_fingerprint = _row_submission_fingerprint(existing)
            if existing_fingerprint == submission_fingerprint:
                return _message_out_from_row(existing)
            raise BridgeProtocolError(
                E_BRIDGE_IDEMPOTENCY_CONFLICT,
                "The message key already exists with different content.",
                status_code=409,
            ) from None

        try:
            await _wake_scheduler.schedule_for_message(
                db,
                msg_row={
                    "id": int(msg_id),
                    "role": role,
                    "message_type": message_type,
                    "typed_kind": typed_storage_values[1],
                    "conversation_id": target_conversation_id,
                    "sender_participant_id": sender_pid,
                    "priority": priority,
                    "mentions_json": mentions_json,
                    "is_system_notice": int(bool(is_system_notice)),
                },
                conv_kind=conv_kind,
                dm_low_id=dm_low_id,
                dm_high_id=dm_high_id,
                now=created_at,
            )
            await _mcp_event_webhooks.schedule_matching_subscriptions(db, now=created_at)
        except Exception as exc:
            logger.warning("Failed to schedule wake targets: %s", exc)

    out = MessageOut(
        id=int(msg_id),
        role=role,
        content=content,
        created_at=created_at,
        order_id=order_id,
        task_id=task_id,
        message_type=message_type,
        status=status,
        reply_to_id=reply_to_id,
        message_key=message_key,
        payload=msg.payload if envelope is None else envelope.payload.model_dump(mode="json", exclude_none=False),
        typed=envelope_to_dict(envelope) if envelope is not None else None,
        typed_integrity="VALID" if envelope is not None else "NOT_APPLICABLE",
        typed_error=None,
        conversation_id=target_conversation_id,
        sender_participant_id=sender_pid,
        sender_name=sender_name,
        priority=priority,
        mentions=mentions,
        transfer_id=msg.transfer_id,
        is_system_notice=bool(is_system_notice),
        deleted_at=None,
        action_id=msg.action_id,
        approval_id=msg.approval_id,
        next_action=msg.next_action,
        reason_code=msg.reason_code,
    )
    broadcast(out)
    return out


@app.post("/api/messages", response_model=MessageOut)
async def post_message(msg: MessageCreate, request: Request):
    """Post an order (Master), execution report (Agent), channel message, or direct message.

    Auth token in Authorization Bearer header or legacy request body.
    """
    auth = await require_auth(request, msg.token, allow_query_token=True)
    return await _create_message_internal(msg, auth)


@app.delete("/api/messages/{message_id}")
async def delete_message(message_id: int, request: Request, token: str | None = None):
    """Delete a message (or soft-delete when APPEND_ONLY_MODE=1). Only its author role can delete it."""
    auth = await require_auth(request, token, allow_query_token=True)

    async with connect_db() as db:
        cursor = await db.execute(
            "SELECT id, role, deleted_at FROM messages WHERE id = ?",
            (message_id,),
        )
        row = await cursor.fetchone()
        if row is None or (APPEND_ONLY_MODE and row["deleted_at"] is not None):
            raise HTTPException(status_code=404, detail="Message not found")
        if normalize_role(row["role"]) != normalize_role(auth.role):
            raise HTTPException(status_code=403, detail="Only the author can delete this message")
        if APPEND_ONLY_MODE:
            await db.execute(
                "UPDATE messages SET deleted_at = ? WHERE id = ?",
                (time.time(), message_id),
            )
            await db.commit()
            _notifier.notify()
            return {"deleted": message_id, "soft_deleted": True}
        await db.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        await db.commit()

    return {"deleted": message_id}


@app.get("/api/whoami")
async def whoami(request: Request, token: str | None = None):
    """Return the role (and participant info if applicable) associated with the provided token."""
    auth = await require_auth(request, token, allow_query_token=True)
    if auth.participant_id is None:
        return {"role": auth.role}
    return {
        "role": auth.role,
        "participant": {
            "id": auth.participant_id,
            "name": auth.participant_name,
            "kind": auth.participant_kind,
            "agent_type": auth.agent_type,
        },
    }


@app.post("/api/stream-ticket")
async def stream_ticket(request: Request):
    """Issue a short-lived ticket for browser EventSource connections."""
    role = await require_reader(request, None)
    await check_sse_ticket_rate_limit(role)
    ticket, expires_in = await issue_sse_ticket(role)
    return {"ticket": ticket, "expires_in": expires_in}


async def event_stream(request: Request, queue: asyncio.Queue):
    try:
        while True:
            if await request.is_disconnected():
                break
            try:
                item = await asyncio.wait_for(queue.get(), timeout=15)
                if item is SSE_DISCONNECT:
                    break
                yield f"id: {item.id}\ndata: {item.model_dump_json()}\n\n"
            except TimeoutError:
                yield ": keepalive\n\n"
    finally:
        _subscribers.discard(queue)


@app.get("/api/stream")
async def stream(request: Request, token: str | None = None, ticket: str | None = None):
    """Server-Sent Events stream of new messages (real-time push)."""
    if ticket:
        await resolve_sse_ticket(ticket)
    else:
        await require_reader(request, token)

    queue = create_sse_queue()
    _subscribers.add(queue)

    return StreamingResponse(
        event_stream(request, queue),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/healthz")
async def healthz():
    """Minimal public liveness endpoint for load balancers and process checks."""
    return {"status": "ok"}


@app.get("/api/status")
async def status():
    """Public health check + activity statistics."""
    async with connect_db() as db:
        cursor = await db.execute(
            "SELECT role, created_at FROM messages WHERE deleted_at IS NULL ORDER BY id DESC LIMIT 1"
        )
        last = await cursor.fetchone()
        cursor = await db.execute("SELECT COUNT(*) as total FROM messages WHERE deleted_at IS NULL")
        total = (await cursor.fetchone())["total"]
        db_generation = await get_db_generation(db)
        active_participants = int(
            (await (await db.execute("SELECT COUNT(*) AS c FROM participants WHERE removed_at IS NULL")).fetchone())["c"]
        )
        channels_count = int(
            (await (await db.execute("SELECT COUNT(*) AS c FROM conversations WHERE kind = 'channel' AND archived_at IS NULL")).fetchone())["c"]
        )

    return {
        "status": "ok",
        "bridge_api_version": BRIDGE_API_VERSION,
        "typed_message_versions": TYPED_MESSAGE_VERSIONS,
        "typed_features": TYPED_FEATURES,
        "total_messages": total,
        "last_message_role": normalize_role(last["role"]) if last and last["role"] else None,
        "last_message_at": last["created_at"] if last else None,
        "db_generation": db_generation,
        "active_participants": active_participants,
        "channels_count": channels_count,
        "stuck_readers_count": len(_stuck_readers.list_stuck()),
    }


# ─── Web LLM Support & Convenience Endpoints ─────────────────────────────────
def _load_prompt_content(filename: str, fallback: str) -> str:
    path = PROMPTS_DIR / filename
    if path.is_file():
        try:
            return path.read_text(encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to read prompt file %s: %s", path, exc)
    return fallback


@app.get("/api/prompts/master")
async def get_master_prompt():
    """Return universal system prompt and instructions for running Master via any Web Chat (Claude, ChatGPT, Gemini) or agent."""
    content = _load_prompt_content(
        "web_chat_master.md",
        fallback="# Universal Master Prompt\nYou are the Master (Orchestrator). Break tasks into atomic orders.",
    )
    return {
        "role": "master",
        "title": "Universal Master Prompt (Claude, ChatGPT, Gemini, or any Web Chat / Agent)",
        "prompt": content,
    }


@app.get("/api/prompts/agent")
async def get_agent_prompt():
    """Return universal system prompt and instructions for running Agent via any Web Chat (Claude, ChatGPT, Gemini) or agent."""
    content = _load_prompt_content(
        "web_chat_agent.md",
        fallback="# Universal Agent Prompt\nYou are the Agent (Executor). Execute orders and report status back.",
    )
    return {
        "role": "agent",
        "title": "Universal Agent Prompt (Claude, ChatGPT, Gemini, or any Web Chat / Agent)",
        "prompt": content,
    }


async def _query_pending_orders(limit: int = 50, before_id: int | None = None) -> list[MessageOut]:
    limit = max(1, min(limit, MAX_PAGE_SIZE))

    def candidate(alias: str) -> str:
        # Aliases are internal constants, never supplied by a client.
        return f"""
            {alias}.deleted_at IS NULL
            AND {alias}.role IN ('master', 'engineer')
            AND (
                {alias}.message_type IN ('ORDER', 'TASK', 'INSTRUCTION')
                OR ({alias}.message_type IS NULL AND (
                    COALESCE({alias}.order_id, {alias}.task_id) IS NOT NULL
                    OR {alias}.typed_kind IN ('ORDER', 'TASK_REQUEST')
                ))
            )
            AND ({alias}.typed_schema_version IS NULL
                 OR {alias}.typed_kind IN ('ORDER', 'TASK_REQUEST'))
        """

    select = ", ".join(f"o.{field.strip()}" for field in MESSAGE_SELECT.split(","))
    async with connect_db() as db:
        rows = await db.execute_fetchall(
            f"""
            SELECT {select} FROM messages o
            WHERE {candidate('o')}
              AND (o.status IS NULL OR o.status NOT IN ('COMPLETED', 'CANCELLED', 'CANCELED', 'CANCEL'))
              AND NOT EXISTS (
                  SELECT 1 FROM messages newer
                  WHERE newer.id > o.id AND {candidate('newer')}
                    AND COALESCE(newer.order_id, newer.task_id) = COALESCE(o.order_id, o.task_id)
              )
              AND NOT EXISTS (
                  SELECT 1 FROM messages terminal
                  WHERE terminal.id > o.id
                    AND terminal.deleted_at IS NULL
                    AND (terminal.status IN ('COMPLETED', 'CANCELLED', 'CANCELED', 'CANCEL')
                         OR (terminal.role IN ('master', 'engineer') AND terminal.message_type = 'CANCEL'))
                    AND (
                        terminal.reply_to_id = o.id
                        OR (
                            COALESCE(terminal.order_id, terminal.task_id) = COALESCE(o.order_id, o.task_id)
                            AND (
                                terminal.reply_to_id IS NULL
                                OR EXISTS (
                                    SELECT 1 FROM messages rt
                                    WHERE rt.id = terminal.reply_to_id
                                      AND rt.deleted_at IS NULL
                                      AND rt.id > o.id
                                      AND NOT ({candidate('rt')})
                                )
                            )
                        )
                    )
              )
              AND (? IS NULL OR o.id < ?)
            ORDER BY o.id DESC LIMIT ?
            """,
            (before_id, before_id, limit),
        )
    return [_message_out_from_row(row) for row in rows]


@app.get("/api/orders/pending", response_model=list[MessageOut])
async def get_pending_orders(
    request: Request,
    token: str | None = None,
    limit: int = 50,
    before_id: int | None = None,
):
    """Retrieve unfinished orders, newest first, with an optional history cursor."""
    await require_auth(request, token, allow_query_token=True)
    return await _query_pending_orders(limit=limit, before_id=before_id)


async def _post_order_internal(order: OrderCreate, auth: AuthContext) -> MessageOut:
    if auth.role != "master":
        raise HTTPException(status_code=403, detail="Only Master can create orders")

    msg = MessageCreate(
        token=order.token,
        content=order.content,
        order_id=order.order_id,
        message_type="ORDER",
        status=order.status,
        message_key=order.message_key,
        reply_to_id=order.reply_to_id,
        payload=order.payload,
        conversation_id=order.conversation_id,
        to_participant_id=order.to_participant_id,
        participant_id=order.participant_id,
        priority=order.priority,
    )
    return await _create_message_internal(msg, auth)


@app.post("/api/orders", response_model=MessageOut)
async def post_order(order: OrderCreate, request: Request):
    """Post an order as Master. Requires Master authentication."""
    auth = await require_auth(request, order.token, allow_query_token=True)
    return await _post_order_internal(order, auth)


async def _post_execution_internal(execution: ExecutionCreate, auth: AuthContext) -> MessageOut:
    if auth.role != "agent":
        raise HTTPException(status_code=403, detail="Only Agent can submit executions")

    msg = MessageCreate(
        token=execution.token,
        content=execution.content,
        order_id=execution.order_id,
        message_type="EXECUTION",
        status=execution.status,
        message_key=execution.message_key,
        reply_to_id=execution.reply_to_id,
        payload=execution.payload,
        conversation_id=execution.conversation_id,
        to_participant_id=execution.to_participant_id,
        participant_id=execution.participant_id,
        priority=execution.priority,
    )
    result = await _create_message_internal(msg, auth)

    # Automatically create a standardized self-improvement record per execution
    try:
        await asyncio.to_thread(
            self_improvement.create_improvement_record,
            task_name=execution.order_id,
            role="agent",
            status=execution.status,
            summary=f"Execution report for order '{execution.order_id}'.",
            improvements=execution.improvements,
            issues=execution.issues,
            action_items=execution.action_items,
            raw_content=execution.content,
            target_dir=IMPROVEMENTS_DIR,
            timestamp=datetime.fromtimestamp(result.created_at, UTC),
            record_id=result.id,
        )
    except Exception as exc:
        logging.getLogger("bridge").warning("Failed to auto-create improvement record: %s", exc)

    return result


@app.post("/api/executions", response_model=MessageOut)
async def post_execution(execution: ExecutionCreate, request: Request):
    """Post an execution report as Agent. Requires Agent authentication."""
    auth = await require_auth(request, execution.token, allow_query_token=True)
    return await _post_execution_internal(execution, auth)


async def _post_improvement_internal(data: ImprovementCreate, auth: AuthContext) -> dict[str, Any]:
    await check_rate_limit(auth.participant_id or auth.role)
    try:
        path = await asyncio.to_thread(
            self_improvement.create_improvement_record,
            task_name=data.task_name,
            role=auth.role,
            status=data.status,
            summary=data.summary,
            improvements=data.improvements,
            issues=data.issues,
            action_items=data.action_items,
            raw_content=data.raw_content,
            target_dir=IMPROVEMENTS_DIR,
        )
        return {
            "status": "ok",
            "filename": path.name,
            "message": f"Self-improvement record saved to {path.name}",
        }
    except Exception as exc:
        logger.exception("Failed to save improvement record")
        raise HTTPException(status_code=500, detail="Failed to save improvement record") from exc


# ─── Self-Improvement Endpoints ───────────────────────────────────────────────
@app.post("/api/improvements")
async def post_improvement(data: ImprovementCreate, request: Request):
    """Record a self-improvement log. Authenticated for Master or Agent."""
    auth = await require_auth(request, data.token, allow_query_token=True)
    return await _post_improvement_internal(data, auth)


@app.get("/api/improvements")
async def get_improvements(request: Request, token: str | None = None, limit: int = 50):
    """List recorded self-improvement logs. Authenticated."""
    await require_reader(request, token)
    limit = max(1, min(limit, 200))
    records = await asyncio.to_thread(
        self_improvement.list_improvement_records, target_dir=IMPROVEMENTS_DIR, limit=limit,
    )
    return records


@app.get("/api/improvements/{filename}")
async def get_improvement_by_filename(filename: str, request: Request, token: str | None = None):
    """Read specific self-improvement log. Authenticated."""
    await require_reader(request, token)
    if not self_improvement.is_valid_improvement_filename(filename):
        raise HTTPException(status_code=400, detail="Invalid improvement record filename format")
    try:
        content = await asyncio.to_thread(
            self_improvement.read_improvement_record, filename, target_dir=IMPROVEMENTS_DIR,
        )
        return {"filename": filename, "content": content}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Improvement record not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ─── Hub Instructions, Context, Participants, Channels & DMs ─────────────────
@app.get("/instructions", response_class=PlainTextResponse)
async def instructions_page(request: Request):
    """Self-served onboarding guide for MCP clients and agents."""
    base_url = str(request.base_url).rstrip("/")
    return PlainTextResponse(
        content=render_instructions_markdown(base_url),
        media_type="text/markdown; charset=utf-8",
    )


@app.get("/llms.txt", response_class=PlainTextResponse)
async def llms_txt(request: Request):
    """Machine-readable service discovery summary."""
    base_url = str(request.base_url).rstrip("/")
    return PlainTextResponse(
        content=render_llms_txt(base_url),
        media_type="text/plain; charset=utf-8",
    )


@app.get("/api/context")
async def get_context_endpoint(request: Request, participant_id: str | None = None):
    """Return server generation, active participants, channels, DMs, and General preview."""
    auth = await require_auth(request, allow_query_token=False)
    async with connect_db() as db:
        return await get_hub_context(
            db,
            participant_id=participant_id or auth.participant_id,
            message_select=MESSAGE_SELECT,
            message_row_converter=_message_out_from_row,
        )


async def _register_participant_internal(auth: AuthContext, payload: ParticipantCreate) -> dict[str, Any]:
    target_role = normalize_role(payload.role) if payload.role else auth.role
    if auth.role != "master":
        if target_role == "master" or payload.kind == "person":
            raise HTTPException(
                status_code=403,
                detail="Only Master can register a master or person participant",
            )
    async with connect_db() as db:
        return await register_participant(
            db,
            name=payload.name,
            role=target_role,
            kind=payload.kind,
            agent_type=payload.agent_type,
            working_directory=payload.working_directory,
            joined_note=payload.joined_note,
            plugin_name=payload.plugin_name,
            plugin_version=payload.plugin_version,
            wake_cadence=payload.wake_cadence,
            request_key=payload.request_key,
            issue_token=payload.issue_token,
        )


@app.post("/api/participants")
async def post_participant(body: ParticipantCreate, request: Request):
    """Register a participant (agent or person) and optionally issue a revocable token."""
    auth = await require_auth(request, allow_query_token=False)
    return await _register_participant_internal(auth, body)


@app.get("/api/participants")
async def get_participants_endpoint(request: Request, include_removed: bool = False):
    """List participants on the hub."""
    await require_auth(request, allow_query_token=False)
    async with connect_db() as db:
        return await list_participants(db, include_removed=include_removed)


@app.patch("/api/participants/{participant_id}")
async def patch_participant_endpoint(
    participant_id: str,
    body: ParticipantPatch,
    request: Request,
):
    """Rename or update metadata / wake cadence for a participant."""
    auth = await require_auth(request, allow_query_token=False)
    if auth.role != "master" and auth.participant_id != participant_id:
        raise HTTPException(status_code=403, detail="Cannot modify another participant")
    if body.wake_cadence is not None and auth.role != "master":
        raise HTTPException(status_code=403, detail="Only Master can configure wake_cadence")
    async with connect_db() as db:
        return await update_participant(
            db,
            participant_id,
            name=body.name,
            working_directory=body.working_directory,
            joined_note=body.joined_note,
            plugin_name=body.plugin_name,
            plugin_version=body.plugin_version,
            wake_cadence=body.wake_cadence,
            renamed_by=auth.participant_id or auth.role,
        )


@app.post("/api/participants/{participant_id}/remove")
async def remove_participant_endpoint(participant_id: str, request: Request):
    """Soft-remove a participant from active messaging, signals, and wakes (Master-only)."""
    auth = await require_auth(request, allow_query_token=False)
    if auth.role != "master":
        raise HTTPException(status_code=403, detail="Only Master can remove participants")
    async with connect_db() as db:
        return await remove_participant(db, participant_id)


@app.post("/api/participants/{participant_id}/restore")
async def restore_participant_endpoint(participant_id: str, request: Request):
    """Restore a previously removed participant (Master-only)."""
    auth = await require_auth(request, allow_query_token=False)
    if auth.role != "master":
        raise HTTPException(status_code=403, detail="Only Master can restore participants")
    async with connect_db() as db:
        return await restore_participant(db, participant_id)


@app.post("/api/participants/{participant_id}/rotate-token")
async def rotate_participant_token_endpoint(participant_id: str, request: Request):
    """Rotate a participant's bearer token (Master or self)."""
    auth = await require_auth(request, allow_query_token=False)
    if auth.role != "master" and auth.participant_id != participant_id:
        raise HTTPException(status_code=403, detail="Cannot rotate another participant's token")
    async with connect_db() as db:
        return await rotate_participant_token(db, participant_id)


@app.get("/api/conversations")
async def get_conversations_endpoint(request: Request, include_archived: bool = False):
    """List channels and auditable direct chats."""
    await require_auth(request, allow_query_token=False)
    async with connect_db() as db:
        return await list_conversations(db, include_archived=include_archived)


async def _create_channel_internal(auth: AuthContext, body: ChannelCreate) -> dict[str, Any]:
    creator_pid = auth.participant_id or body.participant_id
    async with connect_db() as db:
        channel = await create_channel(
            db,
            name=body.name,
            description=body.description,
            created_by_participant_id=creator_pid,
        )
    desc_note = f" — {channel['description']}" if channel.get("description") else ""
    creator_label = auth.participant_name or creator_pid or auth.role
    notice = MessageCreate(
        content=f"New channel **#{channel['name']}** (`{channel['id']}`) created by `{creator_label}`{desc_note}",
        message_type="GENERAL",
        conversation_id=GENERAL_CONVERSATION_ID,
    )
    await _create_message_internal(notice, auth, is_system_notice=True)
    return channel


@app.post("/api/conversations/channels")
async def post_channel_endpoint(body: ChannelCreate, request: Request):
    """Create a channel and announce it in General."""
    auth = await require_auth(request, allow_query_token=False)
    return await _create_channel_internal(auth, body)


@app.post("/api/conversations/{conversation_id}/archive")
async def archive_channel_endpoint(
    conversation_id: str,
    body: ChannelArchiveUpdate,
    request: Request,
):
    """Archive or reopen a channel with optimistic revision check."""
    await require_auth(request, allow_query_token=False)
    async with connect_db() as db:
        return await set_channel_archived(
            db,
            conversation_id,
            archived=body.archived,
            expected_revision=body.expected_revision,
        )


@app.post("/api/conversations/direct")
async def open_direct_chat_endpoint(body: DirectChatCreate, request: Request):
    """Open or reuse a 1-to-1 direct chat between two participants."""
    auth = await require_auth(request, allow_query_token=False)
    sender_pid = auth.participant_id or body.participant_id
    if not sender_pid:
        raise HubServiceError(
            "E_HUB_PARTICIPANT_REQUIRED",
            "Opening a direct chat requires a participant token or participant_id",
            status_code=400,
        )
    async with connect_db() as db:
        target = await resolve_participant_by_id_or_name(db, body.target_participant_id)
        if target is None:
            raise HubServiceError("E_HUB_PARTICIPANT_NOT_FOUND", "Target participant not found", status_code=404)
        conv, created = await open_or_create_direct_chat(
            db,
            participant_a_id=sender_pid,
            participant_b_id=target["id"],
        )
    return {"conversation": conv, "created": created}


# ─── Search, Updates Long-Poll & Stuck Reader Observability ───────────────────
@app.get("/api/messages/search")
async def search_messages_endpoint(
    request: Request,
    q: str,
    conversation_id: str | None = None,
    role: str | None = None,
    order_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
):
    """Full-text FTS5 search across all persisted messages."""
    await require_auth(request, allow_query_token=False)
    if not q or not q.strip():
        raise HTTPException(status_code=422, detail="Search query q cannot be empty")
    async with connect_db() as db:
        matches = await search_messages_fts(
            db,
            query=q,
            conversation_id=conversation_id,
            role=role,
            order_id=order_id,
            status=status,
            limit=limit,
            message_select=MESSAGE_SELECT,
            message_row_converter=_message_out_from_row,
        )
    return {
        "query": q,
        "total": len(matches),
        "messages": [m.model_dump(mode="json") for m in matches],
    }


async def _get_updates_internal(
    auth: AuthContext,
    *,
    after_id: int = 0,
    checkpoint: str | None = None,
    generation: str | None = None,
    participant_id: str | None = None,
    conversation_ids: list[str] | None = None,
    include_direct: bool = True,
    waking_only: bool = False,
    wait_seconds: float = 0.0,
    limit: int = 100,
    plugin: str | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    effective_pid = auth.participant_id or participant_id
    clamped_wait = max(0.0, min(float(wait_seconds), MAX_LONG_POLL_SECONDS))
    clamped_limit = max(1, min(int(limit), 500))

    async def _query_once() -> dict[str, Any]:
        async with connect_db() as db:
            if effective_pid and plugin:
                parts = plugin.split("@", 1)
                pname = parts[0][:64]
                pver = parts[1][:64] if len(parts) > 1 else None
                await db.execute(
                    """
                    UPDATE participants
                    SET plugin_name = ?, plugin_version = COALESCE(?, plugin_version), last_seen_at = ?
                    WHERE id = ?
                    """,
                    (pname, pver, time.time(), effective_pid),
                )
                await db.commit()
            return await fetch_updates_page(
                db,
                after_id=after_id,
                checkpoint=checkpoint,
                generation=generation,
                recipient_participant_id=effective_pid,
                recipient_role=auth.role,
                conversation_ids=conversation_ids,
                include_direct=include_direct,
                waking_only=waking_only,
                limit=clamped_limit,
                message_row_converter=_message_out_from_row,
            )

    page = await _query_once()
    if clamped_wait > 0 and not page["generation_mismatch"] and not page["messages"]:
        deadline = time.monotonic() + clamped_wait
        while not page["messages"] and not page["generation_mismatch"]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            notified = await _notifier.wait_for_message(remaining, request=request)
            if not notified:
                break
            page = await _query_once()

    if not page["generation_mismatch"]:
        reader_key = effective_pid or f"role:{auth.role}"
        req_gen, req_after = parse_checkpoint(checkpoint)
        obs_after = max(after_id, req_after)
        obs_cp = format_checkpoint(req_gen or page["generation"], obs_after)
        _stuck_readers.observe(
            reader_key,
            obs_cp,
            len(page["messages"]),
            plugin=plugin,
        )

    return page


@app.get("/api/updates")
async def get_updates_endpoint(
    request: Request,
    after_id: int = 0,
    checkpoint: str | None = None,
    generation: str | None = None,
    participant_id: str | None = None,
    conversation_ids: str | None = None,
    include_direct: bool = True,
    waking_only: bool = False,
    wait_seconds: float = 0.0,
    limit: int = 100,
    plugin: str | None = None,
):
    """Fetch incremental messages, waking flags, renames, and optional long-polling."""
    auth = await require_auth(request, allow_query_token=False)
    conv_list = (
        [c.strip() for c in conversation_ids.split(",") if c.strip()]
        if conversation_ids is not None
        else None
    )
    try:
        page = await _get_updates_internal(
            auth,
            after_id=after_id,
            checkpoint=checkpoint,
            generation=generation,
            participant_id=participant_id,
            conversation_ids=conv_list,
            include_direct=include_direct,
            waking_only=waking_only,
            wait_seconds=wait_seconds,
            limit=limit,
            plugin=plugin,
            request=request,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if page["generation_mismatch"]:
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "code": "E_BRIDGE_GENERATION_MISMATCH",
                    "message": "Database generation changed; reset checkpoint to 0.",
                    "current_generation": page["generation"],
                }
            },
        )
    return page


@app.get("/api/health/readers")
async def get_stuck_readers_endpoint(request: Request):
    """Return readers currently stuck polling the same checkpoint repeatedly."""
    await require_auth(request, allow_query_token=False)
    return {"stuck_readers": _stuck_readers.list_stuck(), "threshold": STUCK_READER_THRESHOLD}


# ─── Count-Only Signal Links (Hooks) ─────────────────────────────────────────
async def _create_signal_internal(auth: AuthContext, body: SignalCreate) -> dict[str, Any]:
    pid = auth.participant_id or body.participant_id
    if not pid:
        raise HubServiceError(
            "E_HUB_PARTICIPANT_REQUIRED",
            "Creating a signal link requires a participant_id or participant token",
            status_code=400,
        )
    if auth.participant_id is not None and body.participant_id and body.participant_id != auth.participant_id:
        raise HTTPException(status_code=403, detail="Cannot create a signal link for another participant")
    async with connect_db() as db:
        return await create_or_rotate_signal(
            db,
            participant_id=pid,
            conversation_ids=body.conversation_ids,
            include_direct=body.include_direct,
            after_id=body.after_id,
            ttl_seconds=body.ttl_seconds,
        )


@app.post("/api/signals")
async def create_signal_endpoint(body: SignalCreate, request: Request):
    """Create or rotate a count-only signal link for hook scripts."""
    auth = await require_auth(request, allow_query_token=False)
    return await _create_signal_internal(auth, body)


@app.delete("/api/signals")
async def revoke_signals_by_participant_endpoint(
    request: Request,
    participant_id: str | None = None,
):
    """Revoke signal links for a participant."""
    auth = await require_auth(request, allow_query_token=False)
    if (
        participant_id
        and auth.role != "master"
        and auth.participant_id is not None
        and participant_id != auth.participant_id
    ):
        raise HTTPException(status_code=403, detail="Cannot revoke another participant's signal link")
    pid = participant_id or auth.participant_id
    if not pid:
        raise HTTPException(status_code=400, detail="participant_id is required")
    async with connect_db() as db:
        removed = await revoke_signals(db, participant_id=pid)
    return {"removed": removed}


@app.delete("/api/signals/{signal_id}")
async def revoke_signal_by_id_endpoint(signal_id: str, request: Request):
    """Revoke a specific signal link."""
    auth = await require_auth(request, allow_query_token=False)
    async with connect_db() as db:
        if auth.role != "master" and auth.participant_id is not None:
            cur = await db.execute(
                "SELECT participant_id FROM participant_signals WHERE id = ?",
                (signal_id,),
            )
            row = await cur.fetchone()
            if row is not None and row["participant_id"] != auth.participant_id:
                raise HTTPException(status_code=403, detail="Cannot revoke another participant's signal link")
        removed = await revoke_signals(db, signal_id=signal_id)
    return {"removed": removed}


@app.get("/api/signal/{signal_id}")
async def check_signal_endpoint(signal_id: str, request: Request):
    """Check pending/waking/urgent counts via signal link secret in Authorization: Bearer header."""
    secret = extract_bearer_token(request)
    if not secret:
        raise HTTPException(status_code=401, detail="Signal secret must be supplied in Authorization: Bearer header")
    async with connect_db() as db:
        return await check_signal(db, signal_id=signal_id, bearer_secret=secret)


@app.post("/api/signal/{signal_id}/ack")
async def ack_signal_endpoint(signal_id: str, body: SignalAck, request: Request):
    """Advance signal watermark after reading updates."""
    secret = extract_bearer_token(request)
    if not secret:
        raise HTTPException(status_code=401, detail="Signal secret must be supplied in Authorization: Bearer header")
    async with connect_db() as db:
        return await ack_signal(db, signal_id=signal_id, bearer_secret=secret, after_id=body.after_id)


# ─── Out-of-Band File Transfers ──────────────────────────────────────────────
@app.get("/api/transfers/pack-command")
async def transfer_pack_command_endpoint(request: Request, folder: str = "<folder>"):
    """Return a local tar+sha256 packing command for folder transfers."""
    await require_auth(request, allow_query_token=False)
    return get_pack_command(folder)


async def _prepare_transfer_internal(auth: AuthContext, body: TransferPrepare) -> dict[str, Any]:
    sender_pid = auth.participant_id or body.participant_id
    async with connect_db() as db:
        return await prepare_transfer(
            db,
            sender_role=auth.role,
            sender_participant_id=sender_pid,
            name=body.name,
            kind=body.kind,
            size_bytes=body.size_bytes,
            sha256=body.sha256,
            content_type=body.content_type,
            file_count=body.file_count,
            conversation_id=body.conversation_id,
            max_transfer_bytes=MAX_TRANSFER_BYTES,
            ttl_days=TRANSFER_TTL_DAYS,
        )


@app.post("/api/transfers/prepare")
async def prepare_transfer_endpoint(body: TransferPrepare, request: Request):
    """Step 1: Reserve a file or folder transfer and obtain upload URL."""
    auth = await require_auth(request, allow_query_token=False)
    return await _prepare_transfer_internal(auth, body)


@app.put("/api/transfers/{transfer_id}/upload")
async def upload_transfer_endpoint(transfer_id: str, request: Request):
    """Step 2: Stream file bytes directly to disk with size and SHA-256 verification."""
    await require_auth(request, allow_query_token=False)
    async with connect_db() as db:
        return await stream_upload_transfer(
            db,
            transfer_id=transfer_id,
            uploads_dir=UPLOADS_DIR,
            chunks=request.stream(),
            max_transfer_bytes=MAX_TRANSFER_BYTES,
        )


async def _complete_transfer_internal(
    auth: AuthContext,
    transfer_id: str,
    body: TransferComplete,
) -> dict[str, Any]:
    async with connect_db() as db:
        cur = await db.execute("SELECT * FROM file_transfers WHERE id = ?", (transfer_id,))
        row = await cur.fetchone()
        if row is None:
            raise HubServiceError("E_TRANSFER_NOT_FOUND", "Transfer not found", status_code=404)
        row_data = dict(row)
        if float(row_data["expires_at"]) <= time.time() or row_data["status"] == "EXPIRED":
            raise HubServiceError("E_TRANSFER_EXPIRED", "Transfer has expired", status_code=410)
        if row_data["status"] == "COMPLETED" and row_data["message_id"] is not None:
            msg_row = await _find_message_by_id(db, int(row_data["message_id"]))
            return {
                "transfer": await get_transfer_details(db, transfer_id=transfer_id, uploads_dir=UPLOADS_DIR),
                "message": _message_out_from_row(msg_row).model_dump(mode="json") if msg_row else None,
            }
        if row_data["status"] != "UPLOADED":
            raise HubServiceError(
                "E_TRANSFER_NOT_UPLOADED",
                f"Transfer must be uploaded before completion (current status: {row_data['status']})",
                status_code=409,
            )

    content = (body.content or "").strip() or (
        f"Shared {row_data['kind']}: **{row_data['name']}** ({row_data['size_bytes']} bytes, sha256 `{row_data['sha256'][:12]}…`)"
    )
    conv_id = body.conversation_id or row_data["conversation_id"] or GENERAL_CONVERSATION_ID
    msg = MessageCreate(
        content=content,
        order_id=body.order_id,
        message_type="MESSAGE",
        message_key=body.message_key or f"transfer-{transfer_id}",
        conversation_id=None if body.to_participant_id else conv_id,
        to_participant_id=body.to_participant_id,
        participant_id=body.participant_id or row_data["sender_participant_id"],
        transfer_id=transfer_id,
        payload={
            "transfer": {
                "id": transfer_id,
                "name": row_data["name"],
                "kind": row_data["kind"],
                "size_bytes": int(row_data["size_bytes"]),
                "sha256": row_data["sha256"],
                "file_count": row_data["file_count"],
                "expires_at": row_data["expires_at"],
            }
        },
    )
    created_msg = await _create_message_internal(msg, auth)

    async with connect_db() as db:
        await db.execute(
            """
            UPDATE file_transfers
            SET status = 'COMPLETED', message_id = ?, conversation_id = ?
            WHERE id = ?
            """,
            (created_msg.id, created_msg.conversation_id, transfer_id),
        )
        await db.commit()
        details = await get_transfer_details(db, transfer_id=transfer_id, uploads_dir=UPLOADS_DIR)
    return {"transfer": details, "message": created_msg.model_dump(mode="json")}


@app.post("/api/transfers/{transfer_id}/complete")
async def complete_transfer_endpoint(transfer_id: str, body: TransferComplete, request: Request):
    """Step 3: Announce an uploaded transfer as a message in a channel or DM."""
    auth = await require_auth(request, allow_query_token=False)
    return await _complete_transfer_internal(auth, transfer_id, body)


@app.get("/api/transfers")
async def list_transfers_endpoint(
    request: Request,
    conversation_id: str | None = None,
    limit: int = 100,
):
    """List file and folder transfers newest first."""
    await require_auth(request, allow_query_token=False)
    async with connect_db() as db:
        return await list_transfers(db, uploads_dir=UPLOADS_DIR, conversation_id=conversation_id, limit=limit)


@app.get("/api/transfers/{transfer_id}")
async def get_transfer_endpoint(transfer_id: str, request: Request):
    """Inspect a transfer's state, metadata, and download receipts."""
    await require_auth(request, allow_query_token=False)
    async with connect_db() as db:
        return await get_transfer_details(db, transfer_id=transfer_id, uploads_dir=UPLOADS_DIR)


async def _issue_transfer_download_ticket_internal(
    auth: AuthContext,
    transfer_id: str,
    participant_id: str | None = None,
) -> dict[str, Any]:
    async with connect_db() as db:
        details = await get_transfer_details(db, transfer_id=transfer_id, uploads_dir=UPLOADS_DIR)
        if details["expired"] or details["status"] == "EXPIRED":
            raise HubServiceError("E_TRANSFER_EXPIRED", "Transfer has expired", status_code=410)
        if details["status"] != "COMPLETED":
            raise HubServiceError("E_TRANSFER_NOT_READY", "Transfer upload is not completed", status_code=409)
        reader_pid = auth.participant_id or participant_id
        reader_key = reader_pid or f"role:{auth.role}"
        await record_transfer_fetch(
            db,
            transfer_id=transfer_id,
            reader_key=reader_key,
            participant_id=reader_pid,
        )
    ticket, expires_in = _transfer_tickets.issue(transfer_id, TRANSFER_TICKET_TTL)
    download_url = f"/api/transfers/{transfer_id}/download?ticket={ticket}"
    return {
        "transfer_id": transfer_id,
        "name": details["name"],
        "kind": details["kind"],
        "size_bytes": details["size_bytes"],
        "sha256": details["sha256"],
        "ticket": ticket,
        "download_url": download_url,
        "expires_in": expires_in,
        "verify_command": (
            f'curl -fsSL "$BRIDGE_URL{download_url}" -o "{details["name"]}" && '
            f'echo "{details["sha256"]}  {details["name"]}" | (shasum -a 256 -c - 2>/dev/null || sha256sum -c -)'
        ),
    }


@app.post("/api/transfers/{transfer_id}/download-ticket")
async def create_transfer_download_ticket_endpoint(
    transfer_id: str,
    request: Request,
    body: TransferDownloadTicketRequest | None = None,
):
    """Record receipt and issue a short-lived download ticket."""
    auth = await require_auth(request, allow_query_token=False)
    pid = body.participant_id if body else None
    return await _issue_transfer_download_ticket_internal(auth, transfer_id, pid)


@app.get("/api/transfers/{transfer_id}/download")
async def download_transfer_endpoint(
    transfer_id: str,
    request: Request,
    ticket: str | None = None,
):
    """Download transfer bytes via short-lived ticket or Bearer auth."""
    if ticket:
        if not _transfer_tickets.validate(ticket, transfer_id):
            raise HTTPException(status_code=401, detail="Invalid or expired transfer download ticket")
    else:
        auth = await require_auth(request, allow_query_token=False)
        async with connect_db() as db:
            await record_transfer_fetch(
                db,
                transfer_id=transfer_id,
                reader_key=auth.participant_id or f"role:{auth.role}",
                participant_id=auth.participant_id,
            )

    async with connect_db() as db:
        cur = await db.execute("SELECT * FROM file_transfers WHERE id = ?", (transfer_id,))
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Transfer not found")
        row_data = dict(row)
        if float(row_data["expires_at"]) <= time.time() or row_data["status"] == "EXPIRED":
            raise HTTPException(status_code=410, detail="Transfer has expired")
        if row_data["status"] not in {"UPLOADED", "COMPLETED"}:
            raise HTTPException(status_code=409, detail="Transfer is not ready for download")

    blob_path = resolve_safe_blob_path(UPLOADS_DIR, row_data["storage_relpath"])
    if not blob_path.is_file():
        raise HTTPException(status_code=404, detail="Transfer file missing on disk")

    safe_filename = sanitize_content_disposition_filename(row_data["name"])
    return FileResponse(
        path=blob_path,
        media_type=row_data["content_type"] or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_filename}"',
            "X-Transfer-SHA256": row_data["sha256"],
        },
    )


@app.post("/api/transfers/cleanup")
async def cleanup_transfers_endpoint(request: Request):
    """Clean expired transfer blobs from disk (Master-only)."""
    auth = await require_auth(request, allow_query_token=False)
    if auth.role != "master":
        raise HTTPException(status_code=403, detail="Only Master can trigger transfer cleanup")
    async with connect_db() as db:
        cleaned = await cleanup_expired_transfers(db, uploads_dir=UPLOADS_DIR)
    return {"cleaned": cleaned}


# ─── Outbound Auto-Start Wakes ───────────────────────────────────────────────
@app.post("/api/wakes")
async def create_wake_endpoint(body: WakeCreate, request: Request):
    """Register an outbound wake target (webhook, Claude routine, or opt-in local command)."""
    auth = await require_auth(request, allow_query_token=False)
    if auth.role != "master" and auth.participant_id != body.participant_id:
        raise HTTPException(status_code=403, detail="Cannot manage wakes for another participant")
    async with connect_db() as db:
        return await _wake_scheduler.create_target(
            db,
            participant_id=body.participant_id,
            kind=body.kind,
            url=body.url,
            command=body.command,
            secret=body.secret,
            channel_ids=body.channel_ids,
            direct_chats=body.direct_chats,
            debounce_seconds=body.debounce_seconds,
            min_interval_seconds=body.min_interval_seconds,
            max_attempts=body.max_attempts,
        )


@app.get("/api/wakes")
async def list_wakes_endpoint(request: Request, participant_id: str | None = None):
    """List configured wake targets with masked secret hints."""
    auth = await require_auth(request, allow_query_token=False)
    pid = participant_id if auth.role == "master" else (auth.participant_id or participant_id)
    async with connect_db() as db:
        return await _wake_scheduler.list_targets(db, participant_id=pid)


@app.delete("/api/wakes/{wake_id}")
async def delete_wake_endpoint(wake_id: str, request: Request):
    """Delete a wake target."""
    auth = await require_auth(request, allow_query_token=False)
    async with connect_db() as db:
        if auth.role != "master":
            cur = await db.execute("SELECT participant_id FROM wake_targets WHERE id = ?", (wake_id,))
            row = await cur.fetchone()
            if row is not None and row["participant_id"] != auth.participant_id:
                raise HTTPException(status_code=403, detail="Cannot delete another participant's wake target")
        removed = await _wake_scheduler.delete_target(db, wake_id)
    return {"removed": removed}


@app.post("/api/wakes/{wake_id}/test")
async def test_wake_endpoint(wake_id: str, request: Request):
    """Immediately send a test wake event to a target."""
    auth = await require_auth(request, allow_query_token=False)
    if auth.role != "master":
        async with connect_db() as db:
            cur = await db.execute("SELECT participant_id FROM wake_targets WHERE id = ?", (wake_id,))
            row = await cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Wake target not found")
            if row["participant_id"] != auth.participant_id:
                raise HTTPException(status_code=403, detail="Cannot test another participant's wake target")
    results = await _wake_scheduler.dispatch_due(force_target_id=wake_id)
    if not results:
        raise HTTPException(status_code=404, detail="Wake target not found or participant removed")
    return results[0]


@app.post("/api/wakes/dispatch")
async def dispatch_wakes_endpoint(request: Request):
    """Trigger immediate dispatch of due wakes (Master-only)."""
    auth = await require_auth(request, allow_query_token=False)
    if auth.role != "master":
        raise HTTPException(status_code=403, detail="Only Master can trigger wake dispatch")
    results = await _wake_scheduler.dispatch_due()
    return {"dispatched": results}


# ─── Retention & Append-Only Controls ─────────────────────────────────────────
@app.get("/api/retention")
async def get_retention_endpoint(request: Request):
    """Inspect append-only mode and retention prune metadata."""
    await require_auth(request, allow_query_token=False)
    async with connect_db() as db:
        rows = await db.execute_fetchall(
            "SELECT key, value FROM bridge_meta WHERE key IN ('retention_pruned_through_id', 'retention_pruned_through_at')"
        )
        meta = {r["key"]: r["value"] for r in rows}
    pruned_id = meta.get("retention_pruned_through_id")
    pruned_at = meta.get("retention_pruned_through_at")
    return {
        "append_only_mode": APPEND_ONLY_MODE,
        "retention_days": RETENTION_DAYS,
        "pruned_through_id": int(pruned_id) if pruned_id is not None else None,
        "pruned_through_at": float(pruned_at) if pruned_at is not None else None,
    }


@app.post("/api/retention/prune")
async def prune_retention_endpoint(body: RetentionPruneRequest, request: Request):
    """Prune historical messages older than a retention cutoff (Master-only)."""
    auth = await require_auth(request, allow_query_token=False)
    if auth.role != "master":
        raise HTTPException(status_code=403, detail="Only Master can prune history")
    if body.older_than_days is None and body.before_id is None:
        raise HTTPException(status_code=422, detail="Specify older_than_days or before_id")

    clauses: list[str] = []
    params: list[Any] = []
    if body.older_than_days is not None:
        cutoff_ts = time.time() - (body.older_than_days * 86_400.0)
        clauses.append("created_at < ?")
        params.append(cutoff_ts)
    if body.before_id is not None:
        clauses.append("id < ?")
        params.append(body.before_id)

    where_sql = " AND ".join(clauses)
    now = time.time()
    async with connect_db() as db:
        cur = await db.execute(
            f"SELECT COUNT(*) AS cnt, MAX(id) AS max_id FROM messages WHERE {where_sql}",
            tuple(params),
        )
        summary = await cur.fetchone()
        removed = int(summary["cnt"] or 0)
        max_removed_id = summary["max_id"]
        if removed > 0:
            await db.execute(f"DELETE FROM messages WHERE {where_sql}", tuple(params))
            await db.execute(
                "INSERT OR REPLACE INTO bridge_meta (key, value) VALUES ('retention_pruned_through_id', ?)",
                (str(int(max_removed_id)),),
            )
            await db.execute(
                "INSERT OR REPLACE INTO bridge_meta (key, value) VALUES ('retention_pruned_through_at', ?)",
                (str(now),),
            )
            await db.commit()
    return {
        "removed": removed,
        "pruned_through_id": int(max_removed_id) if max_removed_id is not None else None,
        "pruned_through_at": now if removed > 0 else None,
    }


# ─── Native Streamable HTTP MCP Endpoint ──────────────────────────────────────
def _build_mcp_services() -> MCPServices:
    async def svc_get_context(auth: AuthContext, participant_id: str | None) -> dict[str, Any]:
        async with connect_db() as db:
            return await get_hub_context(
                db,
                participant_id=participant_id or auth.participant_id,
                message_select=MESSAGE_SELECT,
                message_row_converter=_message_out_from_row,
            )

    async def svc_register_participant(auth: AuthContext, args: dict[str, Any]) -> dict[str, Any]:
        model = ParticipantCreate.model_validate(args)
        return await _register_participant_internal(auth, model)

    async def svc_rename_participant(auth: AuthContext, participant_id: str, name: str) -> dict[str, Any]:
        if auth.role != "master" and auth.participant_id != participant_id:
            raise HTTPException(status_code=403, detail="Cannot rename another participant")
        async with connect_db() as db:
            return await update_participant(
                db,
                participant_id,
                name=name,
                renamed_by=auth.participant_id or auth.role,
            )

    async def svc_get_updates(auth: AuthContext, args: dict[str, Any]) -> dict[str, Any]:
        return await _get_updates_internal(
            auth,
            after_id=int(args.get("after_id", 0)),
            checkpoint=args.get("checkpoint"),
            generation=args.get("generation"),
            participant_id=args.get("participant_id"),
            conversation_ids=args.get("conversation_ids"),
            include_direct=bool(args.get("include_direct", True)),
            waking_only=bool(args.get("waking_only", False)),
            wait_seconds=float(args.get("wait_seconds", 0.0)),
            limit=int(args.get("limit", 100)),
            plugin=args.get("plugin"),
        )

    async def svc_read_messages(_auth: AuthContext, args: dict[str, Any]) -> list[dict[str, Any]]:
        limit = max(1, min(int(args.get("limit", 50)), 200))
        query_text = args.get("query")
        if query_text:
            async with connect_db() as db:
                hits = await search_messages_fts(
                    db,
                    query=str(query_text),
                    conversation_id=args.get("conversation_id"),
                    role=args.get("role"),
                    order_id=args.get("order_id"),
                    status=args.get("status"),
                    limit=limit,
                    message_select=MESSAGE_SELECT,
                    message_row_converter=_message_out_from_row,
                )
            return [m.model_dump(mode="json") for m in hits]

        filters = await _resolve_message_query_params(
            order_id=args.get("order_id"),
            task_id=None,
            role=args.get("role"),
            role_filter=None,
            message_type=None,
            status=args.get("status"),
            action_id=None,
            approval_id=None,
            typed_kind=None,
            correlation_id=None,
            conversation_id=args.get("conversation_id"),
            participant_id=None,
            priority=None,
            q=None,
        )
        latest = bool(args.get("latest", False))
        after_id = args.get("after_id")
        before_id = args.get("before_id")
        sql, params, _ = _build_messages_sql(
            filters,
            reply_to_id=None,
            after_id=after_id,
            before_id=before_id,
            latest=latest,
            include_deleted=False,
            limit=limit,
        )
        async with connect_db() as db:
            rows = await db.execute_fetchall(sql, params)
        items = [_message_out_from_row(r).model_dump(mode="json") for r in rows]
        if latest or before_id is not None:
            items.reverse()
        return items

    async def svc_send_message(auth: AuthContext, args: dict[str, Any]) -> dict[str, Any]:
        model = MessageCreate.model_validate(args)
        msg = await _create_message_internal(model, auth)
        return msg.model_dump(mode="json")

    async def svc_list_pending(_auth: AuthContext, args: dict[str, Any]) -> list[dict[str, Any]]:
        orders = await _query_pending_orders(
            limit=int(args.get("limit", 50)),
            before_id=args.get("before_id"),
        )
        return [o.model_dump(mode="json") for o in orders]

    async def svc_post_order(auth: AuthContext, args: dict[str, Any]) -> dict[str, Any]:
        model = OrderCreate.model_validate(args)
        return (await _post_order_internal(model, auth)).model_dump(mode="json")

    async def svc_report_execution(auth: AuthContext, args: dict[str, Any]) -> dict[str, Any]:
        model = ExecutionCreate.model_validate(args)
        return (await _post_execution_internal(model, auth)).model_dump(mode="json")

    async def svc_create_channel(auth: AuthContext, args: dict[str, Any]) -> dict[str, Any]:
        model = ChannelCreate.model_validate(args)
        return await _create_channel_internal(auth, model)

    async def svc_set_channel_archived(_auth: AuthContext, args: dict[str, Any]) -> dict[str, Any]:
        async with connect_db() as db:
            return await set_channel_archived(
                db,
                args["conversation_id"],
                archived=bool(args["archived"]),
                expected_revision=args.get("expected_revision"),
            )

    async def svc_prepare_transfer(auth: AuthContext, args: dict[str, Any]) -> dict[str, Any]:
        model = TransferPrepare.model_validate(args)
        return await _prepare_transfer_internal(auth, model)

    async def svc_complete_transfer(auth: AuthContext, args: dict[str, Any]) -> dict[str, Any]:
        transfer_id = str(args["transfer_id"])
        rest = {k: v for k, v in args.items() if k != "transfer_id"}
        model = TransferComplete.model_validate(rest)
        return await _complete_transfer_internal(auth, transfer_id, model)

    async def svc_fetch_transfer(auth: AuthContext, args: dict[str, Any]) -> dict[str, Any]:
        return await _issue_transfer_download_ticket_internal(
            auth,
            str(args["transfer_id"]),
            args.get("participant_id"),
        )

    async def svc_read_transfer(_auth: AuthContext, transfer_id: str) -> dict[str, Any]:
        async with connect_db() as db:
            return await get_transfer_details(db, transfer_id=transfer_id, uploads_dir=UPLOADS_DIR)

    async def svc_create_signal(auth: AuthContext, args: dict[str, Any]) -> dict[str, Any]:
        model = SignalCreate.model_validate(args)
        return await _create_signal_internal(auth, model)

    async def svc_revoke_signal(auth: AuthContext, args: dict[str, Any]) -> dict[str, Any]:
        signal_id = args.get("signal_id")
        target_pid = args.get("participant_id")
        if (
            target_pid
            and auth.role != "master"
            and auth.participant_id is not None
            and target_pid != auth.participant_id
        ):
            raise HTTPException(status_code=403, detail="Cannot revoke another participant's signal link")
        async with connect_db() as db:
            if signal_id and auth.role != "master" and auth.participant_id is not None:
                cur = await db.execute(
                    "SELECT participant_id FROM participant_signals WHERE id = ?",
                    (signal_id,),
                )
                row = await cur.fetchone()
                if row is not None and row["participant_id"] != auth.participant_id:
                    raise HTTPException(status_code=403, detail="Cannot revoke another participant's signal link")
            removed = await revoke_signals(
                db,
                signal_id=signal_id,
                participant_id=target_pid or auth.participant_id,
            )
        return {"removed": removed}

    async def svc_list_wakes(auth: AuthContext, participant_id: str | None) -> list[dict[str, Any]]:
        pid = participant_id if auth.role == "master" else (auth.participant_id or participant_id)
        async with connect_db() as db:
            return await _wake_scheduler.list_targets(db, participant_id=pid)

    async def svc_revoke_wake(auth: AuthContext, wake_id: str) -> dict[str, Any]:
        async with connect_db() as db:
            if auth.role != "master":
                cur = await db.execute("SELECT participant_id FROM wake_targets WHERE id = ?", (wake_id,))
                row = await cur.fetchone()
                if row is not None and row["participant_id"] != auth.participant_id:
                    raise HTTPException(status_code=403, detail="Cannot revoke another participant's wake target")
            removed = await _wake_scheduler.delete_target(db, wake_id)
        return {"removed": removed}

    async def svc_record_improvement(auth: AuthContext, args: dict[str, Any]) -> dict[str, Any]:
        model = ImprovementCreate.model_validate(args)
        return await _post_improvement_internal(model, auth)

    def svc_get_prompt_text(role_name: str) -> str:
        if role_name == "master":
            return _load_prompt_content(
                "web_chat_master.md",
                fallback="# Universal Master Prompt\nYou are the Master (Orchestrator). Break tasks into atomic orders.",
            )
        return _load_prompt_content(
            "web_chat_agent.md",
            fallback="# Universal Agent Prompt\nYou are the Agent (Executor). Execute orders and report status back.",
        )

    async def svc_events_poll(auth: AuthContext, params: dict[str, Any]) -> dict[str, Any]:
        async with connect_db() as db:
            return await handle_events_poll(db, auth, params)

    async def svc_events_subscribe(auth: AuthContext, params: dict[str, Any]) -> dict[str, Any]:
        async with connect_db() as db:
            return await _mcp_event_webhooks.subscribe(db, auth, params)

    async def svc_events_unsubscribe(auth: AuthContext, params: dict[str, Any]) -> dict[str, Any]:
        async with connect_db() as db:
            return await _mcp_event_webhooks.unsubscribe(db, auth, params)

    return MCPServices(
        get_context=svc_get_context,
        register_participant=svc_register_participant,
        rename_participant=svc_rename_participant,
        get_updates=svc_get_updates,
        read_messages=svc_read_messages,
        send_message=svc_send_message,
        list_pending_orders=svc_list_pending,
        post_order=svc_post_order,
        report_execution=svc_report_execution,
        create_channel=svc_create_channel,
        set_channel_archived=svc_set_channel_archived,
        get_pack_command=get_pack_command,
        prepare_transfer=svc_prepare_transfer,
        complete_transfer=svc_complete_transfer,
        fetch_transfer=svc_fetch_transfer,
        read_transfer=svc_read_transfer,
        create_signal=svc_create_signal,
        revoke_signal=svc_revoke_signal,
        list_wakes=svc_list_wakes,
        revoke_wake=svc_revoke_wake,
        record_improvement=svc_record_improvement,
        get_prompt_text=svc_get_prompt_text,
        events_poll=svc_events_poll,
        events_subscribe=svc_events_subscribe,
        events_unsubscribe=svc_events_unsubscribe,
    )


_MCP_SERVICES = _build_mcp_services()


@app.post("/mcp")
async def mcp_post_endpoint(request: Request):
    """Streamable HTTP MCP (JSON-RPC 2.0) endpoint with SEP-3415 Events support."""
    try:
        auth = await require_auth(request, allow_query_token=False)
    except HTTPException as exc:
        if exc.status_code == 401:
            return JSONResponse(
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="ForgeLoopBridge MCP"'},
                content={"error": {"code": "E_MCP_UNAUTHORIZED", "message": str(exc.detail)}},
            )
        raise

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
        )

    # SEP-3415 Streamable HTTP routing header validation (Mcp-Name) when supplied by the client
    if isinstance(payload, dict) and isinstance(payload.get("method"), str):
        method = payload["method"]
        req_id = payload.get("id")
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        mcp_name_hdr = request.headers.get("Mcp-Name")
        if mcp_name_hdr is not None and method in {
            "events/poll",
            "events/stream",
            "events/subscribe",
            "events/unsubscribe",
            "tools/call",
            "prompts/get",
        }:
            if mcp_name_hdr != params.get("name"):
                return JSONResponse(
                    status_code=400,
                    content=_jsonrpc_error(
                        req_id,
                        ERR_HEADER_MISMATCH,
                        "Mcp-Name header does not match params.name",
                    ),
                )

        if method == "events/stream":
            try:
                async with connect_db() as db:
                    stream_ctx = await prepare_events_stream(db, auth, req_id, params)
            except MCPEventsError as exc:
                return JSONResponse(content=_jsonrpc_error(req_id, exc.code, exc.message, exc.data))
            except HubServiceError as exc:
                return JSONResponse(content=_jsonrpc_error(req_id, -32602, exc.message, {"code": exc.code}))

            max_events_test: int | None = None
            raw_max_hdr = request.headers.get("X-MCP-Test-Max-Events")
            if raw_max_hdr and raw_max_hdr.isdigit():
                max_events_test = int(raw_max_hdr)

            return StreamingResponse(
                stream_events_sse(
                    connect_db=connect_db,
                    notifier=_notifier,
                    auth=auth,
                    stream_ctx=stream_ctx,
                    request=request,
                    max_events_for_test=max_events_test,
                ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache, no-transform",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

    base_url = str(request.base_url).rstrip("/")
    result = await handle_mcp_message(
        payload,
        auth=auth,
        services=_MCP_SERVICES,
        base_url=base_url,
    )
    if result is None:
        return Response(status_code=202)
    return JSONResponse(content=result)


@app.get("/mcp")
async def mcp_get_endpoint():
    return JSONResponse(
        status_code=405,
        headers={"Allow": "POST, DELETE"},
        content={"error": {"code": "E_MCP_METHOD_NOT_ALLOWED", "message": "Use POST /mcp for JSON-RPC requests"}},
    )


@app.delete("/mcp")
async def mcp_delete_endpoint():
    return Response(status_code=204)





# ─── Frontend ─────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR), check_dir=False), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=HOST, port=PORT, reload=RELOAD)
