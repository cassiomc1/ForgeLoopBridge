"""
ForgeLoopBridge — Minimalist Markdown communication board
between Engineer and Worker agents.
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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

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
DB_PATH = Path(os.getenv("FORGEBRIDGE_DB", str(BASE_DIR / "data" / "forgebridge.db")))
STATIC_DIR = BASE_DIR / "static"
HOST = os.getenv("HOST", "0.0.0.0")
RELOAD = os.getenv("RELOAD") == "1"

ENGINEER_TOKEN = os.getenv("ENGINEER_TOKEN")
WORKER_TOKEN = os.getenv("WORKER_TOKEN")

if not ENGINEER_TOKEN or not WORKER_TOKEN:
    raise RuntimeError(
        "ENGINEER_TOKEN and WORKER_TOKEN must be set in the environment. "
        "Generate strong tokens with: openssl rand -hex 32"
    )
if ENGINEER_TOKEN == WORKER_TOKEN:
    raise RuntimeError("ENGINEER_TOKEN and WORKER_TOKEN must be different")
if len(ENGINEER_TOKEN) < 16 or len(WORKER_TOKEN) < 16:
    logging.getLogger("forgebridge").warning(
        "Tokens shorter than 16 chars are easy to brute-force; "
        "use `openssl rand -hex 32` to generate strong ones."
    )

MAX_PAGE_SIZE = 1000
BRIDGE_API_VERSION = "2.1.2"
TYPED_MESSAGE_VERSIONS = [1]
TYPED_FEATURES = {
    "idempotency": True,
    "correlation": True,
    "reply_linkage": True,
    "canonical_refs": True,
    "outbox_safe_retry": True,
    "typed_integrity_status": True,
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
RATE_LIMIT_POSTS = _env_int("RATE_LIMIT_POSTS", 30, 1)
RATE_LIMIT_WINDOW = _env_float("RATE_LIMIT_WINDOW", 60, 0)
DEFAULT_PAGE_SIZE = _env_int("DEFAULT_PAGE_SIZE", 200, 1, MAX_PAGE_SIZE)
SSE_QUEUE_SIZE = _env_int("SSE_QUEUE_SIZE", 256, 16, 10000)
SSE_TICKET_TTL = _env_float("SSE_TICKET_TTL", 30, 1, 300, minimum_inclusive=True)
SSE_TICKET_RATE_LIMIT = _env_int("SSE_TICKET_RATE_LIMIT", 30, 1)
SSE_TICKET_RATE_WINDOW = _env_float("SSE_TICKET_RATE_WINDOW", 60, 0)
MAX_TYPED_ENVELOPE_BYTES = _env_int("MAX_TYPED_ENVELOPE_BYTES", 65536, 1)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("forgebridge")

# ─── Rate limiting (in-memory sliding window) ────────────────────────────────
_post_timestamps: dict[str, deque[float]] = defaultdict(deque)
_rl_lock = asyncio.Lock()
_sse_ticket_timestamps: dict[str, deque[float]] = defaultdict(deque)
_sse_ticket_rl_lock = asyncio.Lock()


def _retry_after_seconds(window: deque[float], now: float, window_seconds: float) -> int:
    """Return bounded integer delta-seconds for a full sliding-window budget."""
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
    """Create a bounded queue so one stalled client cannot grow memory forever."""
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
            # Disconnect slow subscribers; they recover through GET /api/messages?after_id=.
            disconnect_slow_subscriber(q)
        except Exception:
            disconnect_slow_subscriber(q)


# ─── Models ───────────────────────────────────────────────────────────────────
VALID_MESSAGE_TYPES = frozenset({
    "TASK",
    "STATUS",
    "DECISION_NEEDED",
    "DECISION_RESOLVED",
    "DECISION_TAKEN",
    "BLOCKED",
    "REVIEW",
    "GENERAL",
    "ACTION_REQUIRED",
    "APPROVAL_REQUIRED",
    "AUTHORITY_REQUIRED",
    "ACTION_RECONCILIATION_REQUIRED",
    "ACTION_RECONCILED",
    "DIAGNOSTIC",
    "POLICY_BLOCKED",
})


def normalize_optional_reference(value):
    """Trim optional coordination references without interpreting ForgeLoop state."""
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
    task_id: str | None = Field(default=None, min_length=1, max_length=200)
    message_type: str | None = Field(default=None, min_length=1, max_length=40)
    action_id: str | None = Field(default=None, min_length=1, max_length=200)
    approval_id: str | None = Field(default=None, min_length=1, max_length=200)
    next_action: str | None = Field(default=None, min_length=1, max_length=100)
    reason_code: str | None = Field(default=None, min_length=1, max_length=160)
    typed: Any | None = None

    @field_validator("task_id", "action_id", "approval_id", "next_action", "reason_code", mode="before")
    @classmethod
    def normalize_references(cls, value):
        return normalize_optional_reference(value)

    @field_validator("message_type", mode="before")
    @classmethod
    def normalize_message_type(cls, value):
        normalized = normalize_optional_reference(value)
        return normalized.upper() if normalized else None


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    created_at: float
    task_id: str | None = None
    message_type: str | None = None
    action_id: str | None = None
    approval_id: str | None = None
    next_action: str | None = None
    reason_code: str | None = None
    typed: dict[str, Any] | None = None
    typed_integrity: Literal["INVALID", "NOT_APPLICABLE", "VALID"] = "NOT_APPLICABLE"
    typed_error: dict[str, str] | None = None


# ─── Auth helpers ─────────────────────────────────────────────────────────────
def resolve_role(token: str) -> str:
    if secrets.compare_digest(token, ENGINEER_TOKEN):
        return "engineer"
    if secrets.compare_digest(token, WORKER_TOKEN):
        return "worker"
    raise HTTPException(status_code=401, detail="Invalid token")


def extract_bearer_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer ") :].strip()
    return ""


async def require_reader(request: Request, token: str | None) -> str:
    """Authenticate read access via Bearer header or ?token= query param."""
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
                task_id TEXT,
                message_type TEXT,
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
        # Safe schema migration for existing databases:
        cursor = await db.execute("PRAGMA table_info(messages)")
        columns = {row["name"] for row in await cursor.fetchall()}
        legacy_columns = (
            "task_id",
            "message_type",
            "action_id",
            "approval_id",
            "next_action",
            "reason_code",
        )
        for column in legacy_columns:
            if column not in columns:
                await db.execute(f"ALTER TABLE messages ADD COLUMN {column} TEXT")

        typed_columns = {
            "typed_schema_version": "INTEGER",
            "typed_kind": "TEXT",
            "message_key": "TEXT",
            "correlation_id": "TEXT",
            "reply_to_id": "INTEGER",
            "expects_reply": "INTEGER",
            "typed_payload_json": "TEXT",
            "canonical_refs_json": "TEXT",
        }
        for column, column_type in typed_columns.items():
            if column not in columns:
                await db.execute(f"ALTER TABLE messages ADD COLUMN {column} {column_type}")

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_created_at
            ON messages(created_at)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_task_id
            ON messages(task_id)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_action_id
            ON messages(action_id)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_approval_id
            ON messages(approval_id)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_message_type
            ON messages(message_type)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_typed_kind
            ON messages(typed_kind)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_correlation_id
            ON messages(correlation_id)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_reply_to_id
            ON messages(reply_to_id)
        """)
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
    title="ForgeLoopBridge",
    description="Minimalist Markdown communication hub between Engineer and Worker agents",
    version=BRIDGE_API_VERSION,
    lifespan=lifespan,
)


@app.exception_handler(BridgeProtocolError)
async def bridge_protocol_error_handler(_request: Request, exc: BridgeProtocolError):
    """Keep Bridge transport errors separate from ForgeLoop reason codes."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


MESSAGE_SELECT = """
    id, role, content, created_at, task_id, message_type,
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
            logger.warning("Partial typed representation for message id=%s", data.get("id"))
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
        canonical_refs = json.loads(data["canonical_refs_json"])
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
        logger.warning("Malformed typed representation for message id=%s", data.get("id"))
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
    return MessageOut(
        id=int(data["id"]),
        role=data["role"],
        content=data["content"],
        created_at=data["created_at"],
        task_id=data.get("task_id"),
        message_type=data.get("message_type"),
        action_id=data.get("action_id"),
        approval_id=data.get("approval_id"),
        next_action=data.get("next_action"),
        reason_code=data.get("reason_code"),
        typed=envelope_to_dict(envelope) if envelope is not None else None,
        typed_integrity=typed_integrity,
        typed_error=typed_error,
    )


def _submission_fingerprint(
    *,
    content: str,
    task_id: str | None,
    message_type: str | None,
    action_id: str | None,
    approval_id: str | None,
    next_action: str | None,
    reason_code: str | None,
    envelope,
) -> str:
    return _json_dumps(
        {
            "content": content,
            "task_id": task_id,
            "message_type": message_type,
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
        data.get("message_key") is not None and envelope is None
    ):
        return None
    return _submission_fingerprint(
        content=data["content"],
        task_id=data.get("task_id"),
        message_type=data.get("message_type"),
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


# ─── API ──────────────────────────────────────────────────────────────────────
@app.get("/api/messages", response_model=list[MessageOut])
async def get_messages(
    request: Request,
    token: str | None = None,
    task_id: str | None = None,
    message_type: str | None = None,
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
    """Return messages ordered by id ASC. Requires a valid token.

    - `task_id`: filter by task identity (exact match)
    - `message_type`: filter by normalized coordination type
    - `action_id`: filter by action reference (exact match)
    - `approval_id`: filter by approval reference (exact match)
    - `typed_kind`: filter by typed message kind
    - `correlation_id`: filter by typed exchange correlation
    - `reply_to_id`: filter by concrete replied-to message id
    - `after_id`: only messages with id > after_id (live updates)
    - `before_id`: only messages with id < before_id (history paging)
    - `latest`: return newest page of messages (cannot combine with after_id / before_id)
    - `limit`: max messages returned (default 200, max 1000)
    """
    role = await require_reader(request, token)
    limit = max(1, min(limit, MAX_PAGE_SIZE))

    if latest and (after_id is not None or before_id is not None):
        raise HTTPException(
            status_code=400,
            detail="latest cannot be combined with after_id or before_id",
        )

    try:
        task_id = normalize_optional_reference(task_id)
        message_type = normalize_optional_reference(message_type)
        message_type = message_type.upper() if message_type else None
        action_id = normalize_optional_reference(action_id)
        approval_id = normalize_optional_reference(approval_id)
        typed_kind = normalize_optional_reference(typed_kind)
        typed_kind = typed_kind.upper() if typed_kind else None
        correlation_id = normalize_optional_reference(correlation_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    query = f"SELECT {MESSAGE_SELECT} FROM messages"
    clauses, params = [], []

    if task_id is not None:
        clauses.append("task_id = ?")
        params.append(task_id)
    if message_type is not None:
        clauses.append("message_type = ?")
        params.append(message_type)
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

    # For latest or history paging take the last N; otherwise first N after cursor.
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
    logger.debug(
        "GET /api/messages role=%s count=%d task_id=%s message_type=%s action_id=%s approval_id=%s typed_kind=%s correlation_id=%s reply_to_id=%s latest=%s",
        role,
        len(messages),
        task_id,
        message_type,
        action_id,
        approval_id,
        typed_kind,
        correlation_id,
        reply_to_id,
        latest,
    )
    return messages


@app.post("/api/messages", response_model=MessageOut)
async def post_message(msg: MessageCreate, request: Request):
    """Post a new Markdown message. Token (header preferred, or body) determines the role."""
    role = await require_reader(request, msg.token)
    await check_rate_limit(role)

    content = msg.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Content cannot be empty")

    task_id = msg.task_id
    message_type = None
    if msg.message_type:
        normalized_type = msg.message_type
        if normalized_type not in VALID_MESSAGE_TYPES:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid message_type '{msg.message_type}'. Allowed types: {sorted(VALID_MESSAGE_TYPES)}",
            )
        message_type = normalized_type

    envelope = None
    if msg.typed is not None:
        envelope = parse_typed_envelope(msg.typed)
        validate_legacy_kind_consistency(message_type, envelope)
        _validate_typed_envelope_size(envelope)

    submission_fingerprint = _submission_fingerprint(
        content=content,
        task_id=msg.task_id,
        message_type=message_type,
        action_id=msg.action_id,
        approval_id=msg.approval_id,
        next_action=msg.next_action,
        reason_code=msg.reason_code,
        envelope=envelope,
    )
    typed_storage_values = _typed_storage_values(envelope)

    async with connect_db() as db:
        if envelope is not None:
            existing = await _find_message_by_key(db, role, envelope.message_key)
            if existing is not None:
                existing_fingerprint = _row_submission_fingerprint(existing)
                if existing_fingerprint == submission_fingerprint:
                    return _message_out_from_row(existing)
                raise BridgeProtocolError(
                    E_BRIDGE_IDEMPOTENCY_CONFLICT,
                    "The message key already exists with different content.",
                    status_code=409,
                )

            target = None
            target_typed = None
            if envelope.reply_to_id is not None:
                target = await _find_message_by_id(db, envelope.reply_to_id)
                target_typed = _typed_envelope_from_row(target) if target is not None else None
            validate_reply_relationship(envelope, role, target, target_typed)

        created_at = time.time()
        try:
            cursor = await db.execute(
                """
                INSERT INTO messages (
                    role, content, created_at, task_id, message_type,
                    action_id, approval_id, next_action, reason_code,
                    typed_schema_version, typed_kind, message_key, correlation_id,
                    reply_to_id, expects_reply, typed_payload_json, canonical_refs_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    role,
                    content,
                    created_at,
                    task_id,
                    message_type,
                    msg.action_id,
                    msg.approval_id,
                    msg.next_action,
                    msg.reason_code,
                    *typed_storage_values,
                ),
            )
            await db.commit()
        except aiosqlite.IntegrityError:
            await db.rollback()
            if envelope is None:
                raise
            existing = await _find_message_by_key(db, role, envelope.message_key)
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
        task_id=task_id,
        message_type=message_type,
        action_id=msg.action_id,
        approval_id=msg.approval_id,
        next_action=msg.next_action,
        reason_code=msg.reason_code,
        typed=envelope_to_dict(envelope) if envelope is not None else None,
        typed_integrity="VALID" if envelope is not None else "NOT_APPLICABLE",
        typed_error=None,
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
        if row["role"] != role:
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
    """Server-Sent Events stream of new messages (real-time push).

    Browser clients use a short-lived ticket. The legacy token query parameter
    remains available for non-browser clients for backward compatibility.
    """
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
    """Public health check + last activity (no message contents exposed)."""
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
        "last_message_role": last["role"] if last else None,
        "last_message_at": last["created_at"] if last else None,
    }


# ─── Frontend ─────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR), check_dir=False), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=HOST, port=PORT, reload=RELOAD)
