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
from pathlib import Path
from typing import Any, Literal

import aiosqlite
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

import self_improvement
from bridge_protocol.errors import (
    E_BRIDGE_IDEMPOTENCY_CONFLICT,
    E_BRIDGE_PERSISTED_TYPED_INVALID,
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
if MASTER_TOKEN == AGENT_TOKEN:
    raise RuntimeError("MASTER_TOKEN and AGENT_TOKEN must be different")
if len(MASTER_TOKEN) < 16 or len(AGENT_TOKEN) < 16:
    logging.getLogger("bridge").warning(
        "Tokens shorter than 16 chars are easy to brute-force; "
        "use `openssl rand -hex 32` to generate strong ones."
    )

MAX_PAGE_SIZE = 1000
BRIDGE_API_VERSION = "3.2.0"
TYPED_MESSAGE_VERSIONS = [1]
TYPED_FEATURES = {
    "idempotency": True,
    "correlation": True,
    "reply_linkage": True,
    "outbox_safe_retry": True,
    "orders_and_executions": True,
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
    now = time.time()
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
    now = time.time()
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
    now = time.time()
    ticket = secrets.token_urlsafe(32)
    expires_at = now + SSE_TICKET_TTL
    async with _sse_ticket_lock:
        _purge_sse_tickets(now)
        _sse_tickets[ticket] = (role, expires_at)
    return ticket, SSE_TICKET_TTL


async def resolve_sse_ticket(ticket: str) -> str:
    now = time.time()
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

    @field_validator("order_id", "message_key", mode="before")
    @classmethod
    def normalize_refs(cls, value):
        return normalize_optional_reference(value)

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value):
        norm = normalize_optional_reference(value)
        return norm.upper() if norm else "PENDING"


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

    @field_validator("order_id", "message_key", mode="before")
    @classmethod
    def normalize_refs(cls, value):
        return normalize_optional_reference(value)

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value):
        norm = normalize_optional_reference(value)
        return norm.upper() if norm else "COMPLETED"


class ImprovementCreate(BaseModel):
    token: str = ""
    task_name: str | None = Field(default=None, max_length=200)
    summary: str = ""
    improvements: list[str] | str | None = None
    issues: list[str] | str | None = None
    action_items: list[str] | str | None = None
    raw_content: str | None = None
    status: str = "COMPLETED"



# ─── Auth helpers ─────────────────────────────────────────────────────────────
def resolve_role(token: str) -> str:
    """Resolve token to role ('master' or 'agent').

    Supports MASTER_TOKEN / AGENT_TOKEN and legacy ENGINEER_TOKEN / WORKER_TOKEN.
    """
    if MASTER_TOKEN and secrets.compare_digest(token, MASTER_TOKEN):
        return "master"
    if AGENT_TOKEN and secrets.compare_digest(token, AGENT_TOKEN):
        return "agent"
    if ENGINEER_TOKEN and secrets.compare_digest(token, ENGINEER_TOKEN):
        return "master"
    if WORKER_TOKEN and secrets.compare_digest(token, WORKER_TOKEN):
        return "agent"
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
    if auth.startswith("Bearer "):
        return auth[len("Bearer ") :].strip()
    return ""


async def require_reader(request: Request, token: str | None) -> str:
    candidate = extract_bearer_token(request) or (token or "")
    if not candidate:
        raise HTTPException(status_code=401, detail="Missing token")
    return resolve_role(candidate)


# ─── Database ─────────────────────────────────────────────────────────────────
@asynccontextmanager
async def connect_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = aiosqlite.connect(DB_PATH, timeout=10)
    try:
        conn = await db
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("PRAGMA synchronous=NORMAL")
        yield conn
        await conn.commit()
    finally:
        await db.close()


async def init_db():
    async with connect_db() as db:
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
        await db.commit()
    logger.info("Database ready at %s", DB_PATH)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield
    _subscribers.clear()
    _sse_tickets.clear()
    _sse_ticket_timestamps.clear()


app = FastAPI(
    title="Master-Agent Bridge",
    description="High-reliability communication hub for orders and executions between Master and Agent",
    version=BRIDGE_API_VERSION,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
    reply_to_id, expects_reply, typed_payload_json, canonical_refs_json
"""


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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
    typed_values = (
        data.get("typed_schema_version"),
        data.get("typed_kind"),
        data.get("message_key"),
        data.get("correlation_id"),
        data.get("reply_to_id"),
        data.get("expects_reply"),
        data.get("typed_payload_json"),
        data.get("canonical_refs_json"),
    )
    if data.get("typed_schema_version") is None:
        if any(value is not None for value in typed_values[1:]):
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
            payload_val = json.loads(data["typed_payload_json"])
        except Exception:
            pass

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
        }
    )


def _row_submission_fingerprint(row) -> str | None:
    data = dict(row)
    envelope, typed_integrity, _typed_error = _typed_row_state(data)
    if typed_integrity == "INVALID" or (
        data.get("message_key") is not None and envelope is None and data.get("typed_schema_version") is not None
    ):
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
@app.get("/api/messages", response_model=list[MessageOut])
async def get_messages(
    request: Request,
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
    after_id: int | None = None,
    before_id: int | None = None,
    latest: bool = False,
    limit: int = DEFAULT_PAGE_SIZE,
):
    """Query messages with optional filtering. Requires authentication."""
    await require_reader(request, token)
    limit = max(1, min(limit, MAX_PAGE_SIZE))

    if latest and (after_id is not None or before_id is not None):
        raise HTTPException(
            status_code=400,
            detail="latest cannot be combined with after_id or before_id",
        )

    try:
        resolved_order_id = normalize_optional_reference(order_id or task_id)
        message_type = normalize_optional_reference(message_type)
        message_type = message_type.upper() if message_type else None
        status = normalize_optional_reference(status)
        status = status.upper() if status else None
        action_id = normalize_optional_reference(action_id)
        approval_id = normalize_optional_reference(approval_id)
        typed_kind = normalize_optional_reference(typed_kind)
        typed_kind = typed_kind.upper() if typed_kind else None
        correlation_id = normalize_optional_reference(correlation_id)
        effective_role = normalize_optional_reference(role or role_filter)
        effective_role = effective_role.lower() if effective_role else None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    query = f"SELECT {MESSAGE_SELECT} FROM messages"
    clauses, params = [], []

    if resolved_order_id is not None:
        clauses.append("(order_id = ? OR task_id = ?)")
        params.extend([resolved_order_id, resolved_order_id])
    if effective_role is not None:
        norm_filter = normalize_role(effective_role)
        if norm_filter == "master":
            clauses.append("role IN ('master', 'engineer')")
        elif norm_filter == "agent":
            clauses.append("role IN ('agent', 'worker')")
        else:
            clauses.append("role = ?")
            params.append(effective_role)
    if message_type is not None:
        clauses.append("message_type = ?")
        params.append(message_type)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if action_id is not None:
        clauses.append("action_id = ?")
        params.append(action_id)
    if approval_id is not None:
        clauses.append("approval_id = ?")
        params.append(approval_id)
    if typed_kind is not None:
        clauses.append("typed_kind = ?")
        params.append(typed_kind)
    if correlation_id is not None:
        clauses.append("correlation_id = ?")
        params.append(correlation_id)
    if reply_to_id is not None:
        clauses.append("reply_to_id = ?")
        params.append(reply_to_id)
    if after_id is not None:
        clauses.append("id > ?")
        params.append(after_id)
    if before_id is not None:
        clauses.append("id < ?")
        params.append(before_id)

    if clauses:
        query += " WHERE " + " AND ".join(clauses)

    if latest or before_id is not None:
        query += " ORDER BY id DESC LIMIT ?"
    else:
        query += " ORDER BY id ASC LIMIT ?"
    params.append(limit)

    async with connect_db() as db:
        rows = await db.execute_fetchall(query, tuple(params))

    messages = [_message_out_from_row(row) for row in rows]
    if latest or before_id is not None:
        messages.reverse()
    return messages


@app.post("/api/messages", response_model=MessageOut)
async def post_message(msg: MessageCreate, request: Request):
    """Post an order (Master) or an execution report (Agent).

    Auth token in Authorization Bearer header or request body.
    """
    role = await require_reader(request, msg.token)
    await check_rate_limit(role)

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

    message_key = envelope.message_key if envelope is not None else msg.message_key
    correlation_id = envelope.correlation_id if envelope is not None else None
    reply_to_id = envelope.reply_to_id if envelope is not None else msg.reply_to_id

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
    )
    typed_storage_values = _typed_storage_values(envelope)

    async with connect_db() as db:
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

        if reply_to_id is not None:
            target = await _find_message_by_id(db, reply_to_id)
            target_typed = _typed_envelope_from_row(target) if target is not None else None
            if envelope is not None:
                validate_reply_relationship(envelope, role, target, target_typed)
            else:
                if target is None:
                    raise BridgeProtocolError(
                        "E_BRIDGE_REPLY_NOT_FOUND",
                        f"reply target {reply_to_id} was not found",
                    )
                target_role = target["role"]
                if normalize_role(target_role) == normalize_role(role):
                    raise BridgeProtocolError(
                        "E_BRIDGE_REPLY_ROLE_INVALID",
                        "Replies must target a message authored by the opposite role",
                    )

        created_at = time.time()
        payload_json = typed_storage_values[6]
        if payload_json is None and msg.payload is not None:
            payload_json = _json_dumps(msg.payload)

        try:
            cursor = await db.execute(
                """
                INSERT INTO messages (
                    role, content, created_at, order_id, task_id, message_type, status,
                    action_id, approval_id, next_action, reason_code,
                    typed_schema_version, typed_kind, message_key, correlation_id,
                    reply_to_id, expects_reply, typed_payload_json, canonical_refs_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
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

        msg_id = cursor.lastrowid
        if msg_id is None:
            raise HTTPException(status_code=500, detail="Could not allocate message id")

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
        action_id=msg.action_id,
        approval_id=msg.approval_id,
        next_action=msg.next_action,
        reason_code=msg.reason_code,
    )
    broadcast(out)
    return out


@app.delete("/api/messages/{message_id}")
async def delete_message(message_id: int, request: Request, token: str | None = None):
    """Delete a message. Only its author role can delete it."""
    role = await require_reader(request, token)

    async with connect_db() as db:
        cursor = await db.execute("SELECT id, role FROM messages WHERE id = ?", (message_id,))
        row = await cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Message not found")
        if normalize_role(row["role"]) != normalize_role(role):
            raise HTTPException(status_code=403, detail="Only the author can delete this message")
        await db.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        await db.commit()

    return {"deleted": message_id}


@app.get("/api/whoami")
async def whoami(request: Request, token: str | None = None):
    """Return the role associated with the provided token."""
    role = await require_reader(request, token)
    return {"role": role}


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
                yield f"data: {item.model_dump_json()}\n\n"
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

    return StreamingResponse(event_stream(request, queue), media_type="text/event-stream")


@app.get("/healthz")
async def healthz():
    """Minimal public liveness endpoint for load balancers and process checks."""
    return {"status": "ok"}


@app.get("/api/status")
async def status():
    """Public health check + activity statistics."""
    async with connect_db() as db:
        cursor = await db.execute(
            "SELECT role, created_at FROM messages ORDER BY id DESC LIMIT 1"
        )
        last = await cursor.fetchone()
        cursor = await db.execute("SELECT COUNT(*) as total FROM messages")
        total = (await cursor.fetchone())["total"]

    return {
        "status": "ok",
        "bridge_api_version": BRIDGE_API_VERSION,
        "typed_message_versions": TYPED_MESSAGE_VERSIONS,
        "typed_features": TYPED_FEATURES,
        "total_messages": total,
        "last_message_role": normalize_role(last["role"]) if last and last["role"] else None,
        "last_message_at": last["created_at"] if last else None,
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


@app.get("/api/orders/pending", response_model=list[MessageOut])
async def get_pending_orders(
    request: Request,
    token: str | None = None,
    limit: int = 50,
):
    """Retrieve orders from Master that are not yet marked as COMPLETED. Requires authentication."""
    await require_reader(request, token)
    limit = max(1, min(limit, MAX_PAGE_SIZE))

    async with connect_db() as db:
        # Completed or cancelled order IDs
        cursor = await db.execute(
            """
            SELECT DISTINCT COALESCE(order_id, task_id) as oid
            FROM messages
            WHERE status IN ('COMPLETED', 'CANCELLED', 'CANCELED', 'CANCEL')
              AND COALESCE(order_id, task_id) IS NOT NULL
            """
        )
        completed_rows = await cursor.fetchall()
        completed_oids = {r["oid"] for r in completed_rows if r["oid"]}

        # Recent candidate orders from master
        cursor = await db.execute(
            f"""
            SELECT {MESSAGE_SELECT} FROM messages
            WHERE role IN ('master', 'engineer')
              AND (message_type IN ('ORDER', 'TASK', 'INSTRUCTION') OR order_id IS NOT NULL)
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit * 3,),
        )
        rows = await cursor.fetchall()

    pending: list[MessageOut] = []
    seen_oids: set[str] = set()
    for row in rows:
        msg = _message_out_from_row(row)
        oid = msg.order_id
        if oid:
            if oid in completed_oids or oid in seen_oids:
                continue
            seen_oids.add(oid)
        if msg.status in ("COMPLETED", "CANCELLED", "CANCELED", "CANCEL"):
            continue
        pending.append(msg)
        if len(pending) >= limit:
            break

    return pending


@app.post("/api/orders", response_model=MessageOut)
async def post_order(order: OrderCreate, request: Request):
    """Post an order as Master. Requires Master authentication."""
    role = await require_reader(request, order.token)
    if role != "master":
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
    )
    return await post_message(msg=msg, request=request)


@app.post("/api/executions", response_model=MessageOut)
async def post_execution(execution: ExecutionCreate, request: Request):
    """Post an execution report as Agent. Requires Agent authentication."""
    role = await require_reader(request, execution.token)
    if role != "agent":
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
    )
    result = await post_message(msg=msg, request=request)

    # Automatically create a standardized self-improvement record per execution
    try:
        self_improvement.create_improvement_record(
            task_name=execution.order_id,
            role="agent",
            status=execution.status,
            summary=f"Execution report for order '{execution.order_id}'.",
            improvements=execution.improvements,
            issues=execution.issues,
            action_items=execution.action_items,
            raw_content=execution.content,
            target_dir=IMPROVEMENTS_DIR,
        )
    except Exception as exc:
        logging.getLogger("bridge").warning("Failed to auto-create improvement record: %s", exc)

    return result


# ─── Self-Improvement Endpoints ───────────────────────────────────────────────
@app.post("/api/improvements")
async def post_improvement(data: ImprovementCreate, request: Request):
    """Record a self-improvement log. Authenticated for Master or Agent."""
    role = await require_reader(request, data.token)
    try:
        path = self_improvement.create_improvement_record(
            task_name=data.task_name,
            role=role,
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
            "path": str(path.resolve()),
            "message": f"Self-improvement record saved to {path.name}",
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save improvement record: {exc}") from exc


@app.get("/api/improvements")
async def get_improvements(request: Request, token: str | None = None, limit: int = 50):
    """List recorded self-improvement logs. Authenticated."""
    await require_reader(request, token)
    limit = max(1, min(limit, 200))
    records = self_improvement.list_improvement_records(target_dir=IMPROVEMENTS_DIR, limit=limit)
    return records


@app.get("/api/improvements/{filename}")
async def get_improvement_by_filename(filename: str, request: Request, token: str | None = None):
    """Read specific self-improvement log. Authenticated."""
    await require_reader(request, token)
    try:
        content = self_improvement.read_improvement_record(filename, target_dir=IMPROVEMENTS_DIR)
        return {"filename": filename, "content": content}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Improvement record not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc




# ─── Frontend ─────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR), check_dir=False), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=HOST, port=PORT, reload=RELOAD)
