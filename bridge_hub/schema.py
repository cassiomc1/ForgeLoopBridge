"""SQLite schema extensions, FTS5 full-text search, and migrations for ForgeLoopBridge Hub."""

from __future__ import annotations

import secrets
import time
from typing import Any

from . import GENERAL_CONVERSATION_ID

MESSAGE_ADDITIVE_COLUMNS: dict[str, str] = {
    "conversation_id": "TEXT",
    "sender_participant_id": "TEXT",
    "sender_name": "TEXT",
    "priority": "TEXT",
    "mentions_json": "TEXT",
    "transfer_id": "TEXT",
    "is_system_notice": "INTEGER DEFAULT 0",
    "deleted_at": "REAL",
}


def sanitize_fts_query(raw: str) -> str:
    """Convert arbitrary user input into safe FTS5 literal phrase tokens.

    Punctuation, hyphens, colons, and FTS boolean keywords are escaped by quoting
    each whitespace-delimited token so malformed queries never raise SQLite errors.
    """
    tokens: list[str] = []
    for part in raw.strip().split():
        cleaned = part.strip()
        if not cleaned:
            continue
        escaped = cleaned.replace('"', '""')
        if not escaped.replace('"', ""):
            continue
        tokens.append(f'"{escaped}"')
    return " ".join(tokens)


async def _ensure_message_columns(db: Any) -> None:
    cursor = await db.execute("PRAGMA table_info(messages)")
    existing = {row["name"] for row in await cursor.fetchall()}
    for col_name, col_type in MESSAGE_ADDITIVE_COLUMNS.items():
        if col_name not in existing:
            await db.execute(f"ALTER TABLE messages ADD COLUMN {col_name} {col_type}")

    indexes = [
        ("idx_messages_conversation_id_id", "messages(conversation_id, id)"),
        ("idx_messages_sender_participant_id", "messages(sender_participant_id)"),
        ("idx_messages_priority", "messages(priority)"),
        ("idx_messages_deleted_at", "messages(deleted_at)"),
        ("idx_messages_transfer_id", "messages(transfer_id)"),
    ]
    for idx_name, idx_target in indexes:
        await db.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {idx_target}")


async def _ensure_fts(db: Any) -> None:
    cur = await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'messages_fts'"
    )
    fts_existed = (await cur.fetchone()) is not None

    await db.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            content,
            order_id,
            content='messages',
            content_rowid='id',
            tokenize='unicode61'
        )
        """
    )

    await db.execute(
        """
        CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages
        WHEN NEW.deleted_at IS NULL
        BEGIN
            INSERT INTO messages_fts(rowid, content, order_id)
            VALUES (NEW.id, NEW.content, COALESCE(NEW.order_id, ''));
        END
        """
    )
    await db.execute(
        """
        CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages
        WHEN OLD.deleted_at IS NULL
        BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content, order_id)
            VALUES ('delete', OLD.id, OLD.content, COALESCE(OLD.order_id, ''));
        END
        """
    )
    await db.execute(
        """
        CREATE TRIGGER IF NOT EXISTS messages_fts_au
        AFTER UPDATE OF content, order_id, deleted_at ON messages
        BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content, order_id)
            SELECT 'delete', OLD.id, OLD.content, COALESCE(OLD.order_id, '')
            WHERE OLD.deleted_at IS NULL;
            INSERT INTO messages_fts(rowid, content, order_id)
            SELECT NEW.id, NEW.content, COALESCE(NEW.order_id, '')
            WHERE NEW.deleted_at IS NULL;
        END
        """
    )

    if not fts_existed:
        await db.execute(
            """
            INSERT INTO messages_fts(rowid, content, order_id)
            SELECT id, content, COALESCE(order_id, '')
            FROM messages
            WHERE deleted_at IS NULL
            """
        )


async def get_db_generation(db: Any) -> str:
    cur = await db.execute("SELECT value FROM bridge_meta WHERE key = 'db_generation'")
    row = await cur.fetchone()
    if row is not None and row["value"]:
        return str(row["value"])
    generation = secrets.token_hex(16)
    await db.execute(
        "INSERT OR IGNORE INTO bridge_meta (key, value) VALUES ('db_generation', ?)",
        (generation,),
    )
    cur = await db.execute("SELECT value FROM bridge_meta WHERE key = 'db_generation'")
    row = await cur.fetchone()
    return str(row["value"]) if row else generation


async def init_hub_schema(db: Any) -> str:
    """Create and upgrade all hub extension tables, indexes, and FTS5 triggers."""
    await _ensure_message_columns(db)

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS bridge_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    generation = secrets.token_hex(16)
    await db.execute(
        "INSERT OR IGNORE INTO bridge_meta (key, value) VALUES ('db_generation', ?)",
        (generation,),
    )

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS participants (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            role TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'agent',
            agent_type TEXT NOT NULL DEFAULT 'generic',
            working_directory TEXT,
            joined_note TEXT,
            token_hash TEXT UNIQUE,
            request_key TEXT UNIQUE,
            plugin_name TEXT,
            plugin_version TEXT,
            wake_cadence TEXT NOT NULL DEFAULT 'every_minute',
            created_at REAL NOT NULL,
            last_seen_at REAL,
            last_message_at REAL,
            message_count INTEGER NOT NULL DEFAULT 0,
            removed_at REAL
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_participants_removed_at ON participants(removed_at)"
    )

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS participant_renames (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            participant_id TEXT NOT NULL,
            previous_name TEXT NOT NULL,
            new_name TEXT NOT NULL,
            renamed_by TEXT,
            created_at REAL NOT NULL
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_participant_renames_pid ON participant_renames(participant_id, id)"
    )

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK (kind IN ('channel', 'direct')),
            name TEXT COLLATE NOCASE,
            description TEXT,
            is_general INTEGER NOT NULL DEFAULT 0,
            participant_low_id TEXT,
            participant_high_id TEXT,
            created_by_participant_id TEXT,
            created_at REAL NOT NULL,
            archived_at REAL,
            revision INTEGER NOT NULL DEFAULT 0,
            message_count INTEGER NOT NULL DEFAULT 0,
            last_message_id INTEGER,
            last_message_at REAL
        )
        """
    )
    await db.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_conversations_channel_name
        ON conversations(name COLLATE NOCASE)
        WHERE kind = 'channel'
        """
    )
    await db.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_conversations_direct_pair
        ON conversations(participant_low_id, participant_high_id)
        WHERE kind = 'direct'
        """
    )

    now = time.time()
    await db.execute(
        """
        INSERT OR IGNORE INTO conversations (
            id, kind, name, description, is_general, created_at
        ) VALUES (?, 'channel', 'General', 'Default server-wide channel', 1, ?)
        """,
        (GENERAL_CONVERSATION_ID, now),
    )

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS participant_signals (
            id TEXT PRIMARY KEY,
            participant_id TEXT NOT NULL UNIQUE,
            secret_hash TEXT NOT NULL,
            conversation_ids_json TEXT NOT NULL,
            include_direct INTEGER NOT NULL DEFAULT 1,
            after_id INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            last_checked_at REAL
        )
        """
    )

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS file_transfers (
            id TEXT PRIMARY KEY,
            sender_role TEXT NOT NULL,
            sender_participant_id TEXT,
            name TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('file', 'folder')),
            size_bytes INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            content_type TEXT,
            file_count INTEGER,
            storage_relpath TEXT NOT NULL,
            status TEXT NOT NULL,
            message_id INTEGER,
            conversation_id TEXT,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_file_transfers_created_at ON file_transfers(created_at DESC)"
    )

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS file_transfer_receipts (
            transfer_id TEXT NOT NULL,
            reader_key TEXT NOT NULL,
            participant_id TEXT,
            first_fetched_at REAL NOT NULL,
            fetch_count INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (transfer_id, reader_key)
        )
        """
    )

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS wake_targets (
            id TEXT PRIMARY KEY,
            participant_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            url TEXT,
            command_json TEXT,
            secret_encrypted TEXT,
            secret_hint TEXT,
            channel_ids_json TEXT NOT NULL,
            direct_chats TEXT NOT NULL DEFAULT 'own',
            delivered_through INTEGER NOT NULL DEFAULT 0,
            min_interval_seconds REAL NOT NULL DEFAULT 60.0,
            debounce_seconds REAL NOT NULL DEFAULT 10.0,
            due_at REAL,
            failures INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 5,
            last_attempt_at REAL,
            last_sent_at REAL,
            last_error TEXT,
            disabled_at REAL,
            disabled_reason TEXT,
            created_at REAL NOT NULL
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_wake_targets_due ON wake_targets(due_at) WHERE due_at IS NOT NULL"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_wake_targets_participant ON wake_targets(participant_id)"
    )

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS wake_sends (
            participant_id TEXT NOT NULL,
            hour_bucket INTEGER NOT NULL,
            sent INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (participant_id, hour_bucket)
        )
        """
    )

    await _ensure_fts(db)
    return await get_db_generation(db)
