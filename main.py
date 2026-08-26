"""
ForgeLoopBridge — Minimalist Markdown communication board
between Engineer and Worker agents.
"""

import asyncio
import logging
import math
import os
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

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


async def check_rate_limit(key: str) -> None:
    now = time.time()
    async with _rl_lock:
        window = _post_timestamps[key]
        while window and now - window[0] > RATE_LIMIT_WINDOW:
            window.popleft()
        if len(window) >= RATE_LIMIT_POSTS:
            raise HTTPException(status_code=429, detail="Rate limit exceeded, slow down")
        window.append(now)


async def check_sse_ticket_rate_limit(role: str) -> None:
    now = time.time()
    async with _sse_ticket_rl_lock:
        window = _sse_ticket_timestamps[role]
        while window and now - window[0] > SSE_TICKET_RATE_WINDOW:
            window.popleft()
        if len(window) >= SSE_TICKET_RATE_LIMIT:
            raise HTTPException(status_code=429, detail="Rate limit exceeded, slow down")
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
        entry = _sse_tickets.get(ticket)
    if entry is None:
        raise HTTPException(status_code=401, detail="Invalid or expired SSE ticket")
    return entry[0]


def disconnect_slow_subscriber(queue: asyncio.Queue) -> None:
    _subscribers.discard(queue)
    try:
        queue.get_nowait()
    except asyncio.QueueEmpty:
        pass
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
                reason_code TEXT
            )
        """)
        # Safe schema migration for existing databases:
        cursor = await db.execute("PRAGMA table_info(messages)")
        columns = {row["name"] for row in await cursor.fetchall()}
        for column in (
            "task_id",
            "message_type",
            "action_id",
            "approval_id",
            "next_action",
            "reason_code",
        ):
            if column not in columns:
                await db.execute(f"ALTER TABLE messages ADD COLUMN {column} TEXT")

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
    version="2.0.0",
    lifespan=lifespan,
)


# ─── API ──────────────────────────────────────────────────────────────────────
@app.get("/api/messages", response_model=list[MessageOut])
async def get_messages(
    request: Request,
    token: str | None = None,
    task_id: str | None = None,
    message_type: str | None = None,
    action_id: str | None = None,
    approval_id: str | None = None,
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
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    query = (
        "SELECT id, role, content, created_at, task_id, message_type, "
        "action_id, approval_id, next_action, reason_code FROM messages"
    )
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

    messages = [MessageOut(**dict(row)) for row in rows]
    if latest or before_id is not None:
        messages.reverse()
    logger.debug(
        "GET /api/messages role=%s count=%d task_id=%s message_type=%s action_id=%s approval_id=%s latest=%s",
        role,
        len(messages),
        task_id,
        message_type,
        action_id,
        approval_id,
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

    created_at = time.time()

    async with connect_db() as db:
        cursor = await db.execute(
            """
            INSERT INTO messages (
                role, content, created_at, task_id, message_type,
                action_id, approval_id, next_action, reason_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        await db.commit()
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
