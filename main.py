"""
ForgeLoopBridge — Minimalist Markdown communication board
between Engineer and Worker agents.
"""

import asyncio
import logging
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
from pydantic import BaseModel, Field

# ─── Config ───────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("FORGEBRIDGE_DB", str(BASE_DIR / "data" / "forgebridge.db")))
STATIC_DIR = BASE_DIR / "static"
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
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

RATE_LIMIT_POSTS = int(os.getenv("RATE_LIMIT_POSTS", "30"))
RATE_LIMIT_WINDOW = float(os.getenv("RATE_LIMIT_WINDOW", "60"))
DEFAULT_PAGE_SIZE = int(os.getenv("DEFAULT_PAGE_SIZE", "200"))
MAX_PAGE_SIZE = 1000

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("forgebridge")

# ─── Rate limiting (in-memory sliding window) ────────────────────────────────
_post_timestamps: dict[str, deque[float]] = defaultdict(deque)
_rl_lock = asyncio.Lock()


async def check_rate_limit(key: str) -> None:
    now = time.time()
    async with _rl_lock:
        window = _post_timestamps[key]
        while window and now - window[0] > RATE_LIMIT_WINDOW:
            window.popleft()
        if len(window) >= RATE_LIMIT_POSTS:
            raise HTTPException(status_code=429, detail="Rate limit exceeded, slow down")
        window.append(now)


# ─── SSE subscribers ──────────────────────────────────────────────────────────
_subscribers: set[asyncio.Queue] = set()


def broadcast(message: "MessageOut") -> None:
    for q in list(_subscribers):
        try:
            q.put_nowait(message)
        except Exception:
            _subscribers.discard(q)


# ─── Models ───────────────────────────────────────────────────────────────────
class MessageCreate(BaseModel):
    token: str = ""
    content: str = Field(..., min_length=1, max_length=50000)


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    created_at: float


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
                created_at REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_created_at
            ON messages(created_at)
        """)
        await db.commit()
    logger.info("Database ready at %s", DB_PATH)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield
    _subscribers.clear()


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
    after_id: int | None = None,
    before_id: int | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
):
    """Return messages ordered by id ASC. Requires a valid token.

    - `after_id`: only messages with id > after_id (live updates)
    - `before_id`: only messages with id < before_id (history paging)
    - `limit`: max messages returned (default 200, max 1000)
    """
    role = await require_reader(request, token)
    limit = max(1, min(limit, MAX_PAGE_SIZE))

    query = "SELECT id, role, content, created_at FROM messages"
    clauses, params = [], []

    if after_id is not None:
        clauses.append("id > ?")
        params.append(after_id)
    if before_id is not None:
        clauses.append("id < ?")
        params.append(before_id)

    if clauses:
        query += " WHERE " + " AND ".join(clauses)

    # For history paging take the last N; otherwise first N after cursor.
    if before_id is not None:
        query += " ORDER BY id DESC LIMIT ?"
    else:
        query += " ORDER BY id ASC LIMIT ?"
    params.append(limit)

    async with connect_db() as db:
        rows = await db.execute_fetchall(query, tuple(params))

    messages = [MessageOut(**dict(row)) for row in rows]
    if before_id is not None:
        messages.reverse()
    logger.debug("GET /api/messages role=%s count=%d", role, len(messages))
    return messages


@app.post("/api/messages", response_model=MessageOut)
async def post_message(msg: MessageCreate, request: Request):
    """Post a new Markdown message. Token (header preferred, or body) determines the role."""
    role = await require_reader(request, msg.token)
    await check_rate_limit(role)

    content = msg.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Content cannot be empty")

    created_at = time.time()

    async with connect_db() as db:
        cursor = await db.execute(
            "INSERT INTO messages (role, content, created_at) VALUES (?, ?, ?)",
            (role, content, created_at),
        )
        await db.commit()
        msg_id = cursor.lastrowid

    out = MessageOut(id=int(msg_id), role=role, content=content, created_at=created_at)
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


@app.get("/api/stream")
async def stream(request: Request, token: str | None = None):
    """Server-Sent Events stream of new messages (real-time push)."""
    await require_reader(request, token)

    queue: asyncio.Queue = asyncio.Queue()
    _subscribers.add(queue)

    async def event_gen():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {msg.model_dump_json()}\n\n"
                except TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            _subscribers.discard(queue)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


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
