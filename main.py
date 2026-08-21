"""
ForgeLoopBridge — Minimalist Markdown communication board
between Engineer and Worker agents.
"""

import os
import time
from contextlib import asynccontextmanager
from typing import Optional

import aiosqlite
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ─── Config ───────────────────────────────────────────────────────────────────
DB_PATH = os.getenv("FORGEBRIDGE_DB", "data/forgebridge.db")
ENGINEER_TOKEN = os.getenv("ENGINEER_TOKEN", "engineer_secret")
WORKER_TOKEN = os.getenv("WORKER_TOKEN", "worker_secret")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

# ─── Models ───────────────────────────────────────────────────────────────────
class MessageCreate(BaseModel):
    token: str
    content: str = Field(..., min_length=1, max_length=50000)


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    created_at: float


# ─── Database ─────────────────────────────────────────────────────────────────
async def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title="ForgeLoopBridge",
    description="Minimalist Markdown communication hub between Engineer and Worker agents",
    version="1.0.0",
    lifespan=lifespan,
)


# ─── API ──────────────────────────────────────────────────────────────────────
@app.get("/api/messages", response_model=list[MessageOut])
async def get_messages(since: Optional[float] = None):
    """Return messages ordered by creation time. Optional `since` unix timestamp filter."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if since is not None:
            cursor = await db.execute(
                "SELECT id, role, content, created_at FROM messages WHERE created_at > ? ORDER BY created_at ASC",
                (since,),
            )
        else:
            cursor = await db.execute(
                "SELECT id, role, content, created_at FROM messages ORDER BY created_at ASC"
            )
        rows = await cursor.fetchall()
        return [MessageOut(**dict(row)) for row in rows]


@app.post("/api/messages", response_model=MessageOut)
async def post_message(msg: MessageCreate):
    """Post a new Markdown message. Token determines the role."""
    if msg.token == ENGINEER_TOKEN:
        role = "engineer"
    elif msg.token == WORKER_TOKEN:
        role = "worker"
    else:
        raise HTTPException(status_code=401, detail="Invalid token")

    content = msg.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Content cannot be empty")

    created_at = time.time()

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO messages (role, content, created_at) VALUES (?, ?, ?)",
            (role, content, created_at),
        )
        await db.commit()
        msg_id = cursor.lastrowid

    return MessageOut(id=msg_id, role=role, content=content, created_at=created_at)


@app.get("/api/status")
async def status():
    """Simple health + last activity."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT role, created_at FROM messages ORDER BY created_at DESC LIMIT 1"
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
    return FileResponse("static/index.html")


# Mount static if needed later (css/js separate)
app.mount("/static", StaticFiles(directory="static"), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=HOST, port=PORT, reload=True)
