"""Core domain services for participants, channels, DMs, updates, FTS5 search, and signals."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
import uuid
from typing import Any

from . import GENERAL_CONVERSATION_ID
from .resilience import format_checkpoint, parse_checkpoint
from .schema import get_db_generation, sanitize_fts_query

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
VALID_WAKE_CADENCES = frozenset(
    {"right_away", "every_minute", "every_5_minutes", "every_15_minutes"}
)
VALID_PARTICIPANT_KINDS = frozenset({"agent", "person"})
ONLINE_WINDOW_SECONDS = 300.0
DEFAULT_SIGNAL_TTL_SECONDS = 86_400.0
MAX_SIGNAL_TTL_SECONDS = 7 * 86_400.0
SIGNAL_COUNT_PAGE_LIMIT = 200


class HubServiceError(Exception):
    """Domain validation or state error raised by hub service helpers."""

    def __init__(self, code: str, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def validate_handle_name(raw: str, *, label: str = "name") -> str:
    value = raw.strip()
    if not NAME_RE.match(value):
        raise HubServiceError(
            "E_HUB_INVALID_NAME",
            f"{label} must be 1-64 chars, start with an ASCII letter, and use only letters, digits, '_' or '-'",
            status_code=422,
        )
    return value


def normalize_role_value(role: str) -> str:
    r = role.strip().lower()
    if r in {"master", "engineer"}:
        return "master"
    if r in {"agent", "worker"}:
        return "agent"
    return r


def _participant_dict(row: Any, now: float | None = None) -> dict[str, Any]:
    current = now if now is not None else time.time()
    data = dict(row)
    last_seen = data.get("last_seen_at")
    removed_at = data.get("removed_at")
    return {
        "id": data["id"],
        "name": data["name"],
        "role": data["role"],
        "kind": data["kind"],
        "agent_type": data["agent_type"],
        "working_directory": data.get("working_directory"),
        "joined_note": data.get("joined_note"),
        "plugin_name": data.get("plugin_name"),
        "plugin_version": data.get("plugin_version"),
        "wake_cadence": data.get("wake_cadence") or "every_minute",
        "created_at": data["created_at"],
        "last_seen_at": last_seen,
        "last_message_at": data.get("last_message_at"),
        "message_count": int(data.get("message_count") or 0),
        "removed_at": removed_at,
        "online": bool(
            removed_at is None
            and last_seen is not None
            and (current - float(last_seen)) <= ONLINE_WINDOW_SECONDS
        ),
    }


def _conversation_dict(
    row: Any,
    participants_by_id: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    data = dict(row)
    result: dict[str, Any] = {
        "id": data["id"],
        "kind": data["kind"],
        "name": data.get("name"),
        "description": data.get("description"),
        "is_general": bool(data.get("is_general")),
        "participant_low_id": data.get("participant_low_id"),
        "participant_high_id": data.get("participant_high_id"),
        "created_by_participant_id": data.get("created_by_participant_id"),
        "created_at": data["created_at"],
        "archived_at": data.get("archived_at"),
        "revision": int(data.get("revision") or 0),
        "message_count": int(data.get("message_count") or 0),
        "last_message_id": data.get("last_message_id"),
        "last_message_at": data.get("last_message_at"),
    }
    if data["kind"] == "direct" and participants_by_id is not None:
        low = participants_by_id.get(data.get("participant_low_id") or "")
        high = participants_by_id.get(data.get("participant_high_id") or "")
        result["participants"] = [p for p in (low, high) if p is not None]
    return result


async def register_participant(
    db: Any,
    *,
    name: str,
    role: str,
    kind: str = "agent",
    agent_type: str = "generic",
    working_directory: str | None = None,
    joined_note: str | None = None,
    plugin_name: str | None = None,
    plugin_version: str | None = None,
    wake_cadence: str = "every_minute",
    request_key: str | None = None,
    issue_token: bool = True,
) -> dict[str, Any]:
    clean_name = validate_handle_name(name, label="participant name")
    norm_role = normalize_role_value(role)
    if norm_role not in {"master", "agent"}:
        raise HubServiceError("E_HUB_INVALID_ROLE", "role must be 'master' or 'agent'", status_code=422)
    clean_kind = kind.strip().lower()
    if clean_kind not in VALID_PARTICIPANT_KINDS:
        raise HubServiceError("E_HUB_INVALID_KIND", "kind must be 'agent' or 'person'", status_code=422)
    clean_agent_type = validate_handle_name(agent_type, label="agent_type")
    if wake_cadence not in VALID_WAKE_CADENCES:
        raise HubServiceError(
            "E_HUB_INVALID_CADENCE",
            f"wake_cadence must be one of {sorted(VALID_WAKE_CADENCES)}",
            status_code=422,
        )

    if request_key:
        cur = await db.execute(
            "SELECT * FROM participants WHERE request_key = ?",
            (request_key,),
        )
        existing_req = await cur.fetchone()
        if existing_req is not None:
            return {"participant": _participant_dict(existing_req), "created": False}

    cur = await db.execute(
        "SELECT * FROM participants WHERE lower(name) = lower(?)",
        (clean_name,),
    )
    existing_name = await cur.fetchone()
    if existing_name is not None:
        raise HubServiceError(
            "E_HUB_PARTICIPANT_EXISTS",
            f"Participant '{clean_name}' already exists",
            status_code=409,
        )

    participant_id = str(uuid.uuid4())
    now = time.time()
    raw_token = f"flb_{secrets.token_urlsafe(32)}" if issue_token else None
    token_hash = hash_secret(raw_token) if raw_token else None

    await db.execute(
        """
        INSERT INTO participants (
            id, name, role, kind, agent_type, working_directory, joined_note,
            token_hash, request_key, plugin_name, plugin_version, wake_cadence,
            created_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            participant_id,
            clean_name,
            norm_role,
            clean_kind,
            clean_agent_type,
            working_directory,
            joined_note,
            token_hash,
            request_key,
            plugin_name,
            plugin_version,
            wake_cadence,
            now,
            now,
        ),
    )
    await db.commit()
    cur = await db.execute("SELECT * FROM participants WHERE id = ?", (participant_id,))
    row = await cur.fetchone()
    result: dict[str, Any] = {"participant": _participant_dict(row, now=now), "created": True}
    if raw_token is not None:
        result["token"] = raw_token
    return result


async def list_participants(
    db: Any, *, include_removed: bool = False
) -> list[dict[str, Any]]:
    now = time.time()
    where = "" if include_removed else "WHERE removed_at IS NULL"
    rows = await db.execute_fetchall(
        f"SELECT * FROM participants {where} ORDER BY created_at ASC, name ASC"
    )
    return [_participant_dict(row, now=now) for row in rows]


async def get_participant(db: Any, participant_id: str) -> dict[str, Any] | None:
    cur = await db.execute("SELECT * FROM participants WHERE id = ?", (participant_id,))
    row = await cur.fetchone()
    return _participant_dict(row) if row is not None else None


async def update_participant(
    db: Any,
    participant_id: str,
    *,
    name: str | None = None,
    working_directory: str | None = None,
    joined_note: str | None = None,
    plugin_name: str | None = None,
    plugin_version: str | None = None,
    wake_cadence: str | None = None,
    renamed_by: str | None = None,
) -> dict[str, Any]:
    cur = await db.execute("SELECT * FROM participants WHERE id = ?", (participant_id,))
    row = await cur.fetchone()
    if row is None:
        raise HubServiceError("E_HUB_PARTICIPANT_NOT_FOUND", "Participant not found", status_code=404)
    if row["removed_at"] is not None:
        raise HubServiceError(
            "E_HUB_PARTICIPANT_REMOVED",
            "Cannot modify a removed participant; restore it first",
            status_code=409,
        )

    now = time.time()
    new_name = row["name"]
    if name is not None:
        candidate = validate_handle_name(name, label="participant name")
        if candidate != row["name"]:
            conflict = await (
                await db.execute(
                    "SELECT id FROM participants WHERE lower(name) = lower(?) AND id <> ?",
                    (candidate, participant_id),
                )
            ).fetchone()
            if conflict is not None:
                raise HubServiceError(
                    "E_HUB_PARTICIPANT_EXISTS",
                    f"Participant '{candidate}' already exists",
                    status_code=409,
                )
            await db.execute(
                """
                INSERT INTO participant_renames (
                    participant_id, previous_name, new_name, renamed_by, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (participant_id, row["name"], candidate, renamed_by, now),
            )
            new_name = candidate

    if wake_cadence is not None and wake_cadence not in VALID_WAKE_CADENCES:
        raise HubServiceError(
            "E_HUB_INVALID_CADENCE",
            f"wake_cadence must be one of {sorted(VALID_WAKE_CADENCES)}",
            status_code=422,
        )

    await db.execute(
        """
        UPDATE participants
        SET name = ?,
            working_directory = COALESCE(?, working_directory),
            joined_note = COALESCE(?, joined_note),
            plugin_name = COALESCE(?, plugin_name),
            plugin_version = COALESCE(?, plugin_version),
            wake_cadence = COALESCE(?, wake_cadence),
            last_seen_at = ?
        WHERE id = ?
        """,
        (
            new_name,
            working_directory,
            joined_note,
            plugin_name,
            plugin_version,
            wake_cadence,
            now,
            participant_id,
        ),
    )
    await db.commit()
    cur = await db.execute("SELECT * FROM participants WHERE id = ?", (participant_id,))
    return _participant_dict(await cur.fetchone(), now=now)


async def remove_participant(db: Any, participant_id: str) -> dict[str, Any]:
    cur = await db.execute("SELECT * FROM participants WHERE id = ?", (participant_id,))
    row = await cur.fetchone()
    if row is None:
        raise HubServiceError("E_HUB_PARTICIPANT_NOT_FOUND", "Participant not found", status_code=404)
    now = row["removed_at"] or time.time()
    if row["removed_at"] is None:
        await db.execute(
            "UPDATE participants SET removed_at = ? WHERE id = ?",
            (now, participant_id),
        )
        await db.execute(
            "DELETE FROM participant_signals WHERE participant_id = ?",
            (participant_id,),
        )
        await db.execute(
            """
            UPDATE wake_targets
            SET disabled_at = COALESCE(disabled_at, ?),
                disabled_reason = COALESCE(disabled_reason, 'participant_removed'),
                due_at = NULL
            WHERE participant_id = ?
            """,
            (now, participant_id),
        )
        await db.commit()
    cur = await db.execute("SELECT * FROM participants WHERE id = ?", (participant_id,))
    return _participant_dict(await cur.fetchone())


async def restore_participant(db: Any, participant_id: str) -> dict[str, Any]:
    cur = await db.execute("SELECT * FROM participants WHERE id = ?", (participant_id,))
    row = await cur.fetchone()
    if row is None:
        raise HubServiceError("E_HUB_PARTICIPANT_NOT_FOUND", "Participant not found", status_code=404)
    if row["removed_at"] is not None:
        await db.execute(
            "UPDATE participants SET removed_at = NULL WHERE id = ?",
            (participant_id,),
        )
        await db.commit()
    cur = await db.execute("SELECT * FROM participants WHERE id = ?", (participant_id,))
    return _participant_dict(await cur.fetchone())


async def rotate_participant_token(db: Any, participant_id: str) -> dict[str, Any]:
    cur = await db.execute("SELECT * FROM participants WHERE id = ?", (participant_id,))
    row = await cur.fetchone()
    if row is None:
        raise HubServiceError("E_HUB_PARTICIPANT_NOT_FOUND", "Participant not found", status_code=404)
    if row["removed_at"] is not None:
        raise HubServiceError(
            "E_HUB_PARTICIPANT_REMOVED",
            "Cannot rotate token for a removed participant",
            status_code=409,
        )
    raw_token = f"flb_{secrets.token_urlsafe(32)}"
    await db.execute(
        "UPDATE participants SET token_hash = ? WHERE id = ?",
        (hash_secret(raw_token), participant_id),
    )
    await db.commit()
    return {"participant_id": participant_id, "name": row["name"], "token": raw_token}


async def list_conversations(
    db: Any, *, include_archived: bool = False
) -> list[dict[str, Any]]:
    participants = await list_participants(db, include_removed=True)
    by_id = {p["id"]: p for p in participants}
    where = "" if include_archived else "WHERE archived_at IS NULL"
    rows = await db.execute_fetchall(
        f"""
        SELECT * FROM conversations
        {where}
        ORDER BY is_general DESC, kind ASC, created_at ASC
        """
    )
    return [_conversation_dict(row, by_id) for row in rows]


async def create_channel(
    db: Any,
    *,
    name: str,
    description: str | None = None,
    created_by_participant_id: str | None = None,
) -> dict[str, Any]:
    clean_name = validate_handle_name(name, label="channel name")
    if clean_name.lower() == "general":
        raise HubServiceError(
            "E_HUB_CHANNEL_RESERVED",
            "Channel name 'General' is reserved",
            status_code=409,
        )
    existing = await (
        await db.execute(
            "SELECT id FROM conversations WHERE kind = 'channel' AND lower(name) = lower(?)",
            (clean_name,),
        )
    ).fetchone()
    if existing is not None:
        raise HubServiceError(
            "E_HUB_CHANNEL_EXISTS",
            f"Channel '{clean_name}' already exists",
            status_code=409,
        )

    channel_id = str(uuid.uuid4())
    now = time.time()
    clean_desc = description.strip() if description and description.strip() else None
    await db.execute(
        """
        INSERT INTO conversations (
            id, kind, name, description, is_general, created_by_participant_id, created_at
        ) VALUES (?, 'channel', ?, ?, 0, ?, ?)
        """,
        (channel_id, clean_name, clean_desc, created_by_participant_id, now),
    )
    await db.commit()
    cur = await db.execute("SELECT * FROM conversations WHERE id = ?", (channel_id,))
    return _conversation_dict(await cur.fetchone())


async def set_channel_archived(
    db: Any,
    conversation_id: str,
    *,
    archived: bool,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    cur = await db.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
    row = await cur.fetchone()
    if row is None:
        raise HubServiceError("E_HUB_CONVERSATION_NOT_FOUND", "Conversation not found", status_code=404)
    if row["kind"] != "channel" or row["is_general"]:
        raise HubServiceError(
            "E_HUB_CHANNEL_CANNOT_ARCHIVE",
            "General and direct chats cannot be archived",
            status_code=400,
        )
    if expected_revision is not None and int(row["revision"]) != expected_revision:
        raise HubServiceError(
            "E_HUB_REVISION_CONFLICT",
            f"Conversation revision mismatch (expected {expected_revision}, got {row['revision']})",
            status_code=409,
        )
    new_archived_at = time.time() if archived else None
    await db.execute(
        """
        UPDATE conversations
        SET archived_at = ?, revision = revision + 1
        WHERE id = ?
        """,
        (new_archived_at, conversation_id),
    )
    await db.commit()
    cur = await db.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
    return _conversation_dict(await cur.fetchone())


async def open_or_create_direct_chat(
    db: Any,
    *,
    participant_a_id: str,
    participant_b_id: str,
) -> tuple[dict[str, Any], bool]:
    if participant_a_id == participant_b_id:
        raise HubServiceError(
            "E_HUB_DIRECT_SELF",
            "Direct chat requires two distinct participants",
            status_code=400,
        )
    rows = await db.execute_fetchall(
        "SELECT id, removed_at FROM participants WHERE id IN (?, ?)",
        (participant_a_id, participant_b_id),
    )
    found = {r["id"]: r for r in rows}
    if participant_a_id not in found or participant_b_id not in found:
        raise HubServiceError(
            "E_HUB_PARTICIPANT_NOT_FOUND",
            "Both participants must exist to open a direct chat",
            status_code=404,
        )
    if found[participant_a_id]["removed_at"] is not None or found[participant_b_id]["removed_at"] is not None:
        raise HubServiceError(
            "E_HUB_PARTICIPANT_REMOVED",
            "Removed participants cannot open direct chats",
            status_code=403,
        )

    low_id, high_id = sorted((participant_a_id, participant_b_id))
    cur = await db.execute(
        """
        SELECT * FROM conversations
        WHERE kind = 'direct' AND participant_low_id = ? AND participant_high_id = ?
        """,
        (low_id, high_id),
    )
    existing = await cur.fetchone()
    if existing is not None:
        participants = await list_participants(db, include_removed=True)
        return _conversation_dict(existing, {p["id"]: p for p in participants}), False

    conv_id = str(uuid.uuid4())
    now = time.time()
    await db.execute(
        """
        INSERT INTO conversations (
            id, kind, participant_low_id, participant_high_id,
            created_by_participant_id, created_at
        ) VALUES (?, 'direct', ?, ?, ?, ?)
        """,
        (conv_id, low_id, high_id, participant_a_id, now),
    )
    await db.commit()
    cur = await db.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,))
    participants = await list_participants(db, include_removed=True)
    return _conversation_dict(await cur.fetchone(), {p["id"]: p for p in participants}), True


async def resolve_participant_by_id_or_name(
    db: Any, identifier: str
) -> dict[str, Any] | None:
    cur = await db.execute(
        "SELECT * FROM participants WHERE id = ? OR lower(name) = lower(?)",
        (identifier, identifier),
    )
    row = await cur.fetchone()
    return _participant_dict(row) if row is not None else None


async def resolve_conversation_by_id_or_name(
    db: Any, identifier: str
) -> dict[str, Any] | None:
    cur = await db.execute(
        """
        SELECT * FROM conversations
        WHERE id = ? OR (kind = 'channel' AND lower(name) = lower(?))
        """,
        (identifier, identifier),
    )
    row = await cur.fetchone()
    return _conversation_dict(row) if row is not None else None


async def create_or_rotate_signal(
    db: Any,
    *,
    participant_id: str,
    conversation_ids: list[str] | None = None,
    include_direct: bool = True,
    after_id: int | None = None,
    ttl_seconds: float = DEFAULT_SIGNAL_TTL_SECONDS,
) -> dict[str, Any]:
    part = await get_participant(db, participant_id)
    if part is None:
        raise HubServiceError("E_HUB_PARTICIPANT_NOT_FOUND", "Participant not found", status_code=404)
    if part["removed_at"] is not None:
        raise HubServiceError("E_HUB_PARTICIPANT_REMOVED", "Participant is removed", status_code=403)

    clamped_ttl = max(60.0, min(float(ttl_seconds), MAX_SIGNAL_TTL_SECONDS))
    now = time.time()
    expires_at = now + clamped_ttl
    if after_id is None:
        cur = await db.execute(
            "SELECT COALESCE(MAX(id), 0) AS max_id FROM messages WHERE deleted_at IS NULL"
        )
        after_id = int((await cur.fetchone())["max_id"])

    clean_conv_ids = list(
        dict.fromkeys(c.strip() for c in (conversation_ids or [GENERAL_CONVERSATION_ID]) if c.strip())
    )
    signal_id = str(uuid.uuid4())
    secret = f"sig_{secrets.token_urlsafe(32)}"
    secret_hash = hash_secret(secret)

    await db.execute(
        "DELETE FROM participant_signals WHERE participant_id = ?",
        (participant_id,),
    )
    await db.execute(
        """
        INSERT INTO participant_signals (
            id, participant_id, secret_hash, conversation_ids_json,
            include_direct, after_id, created_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            signal_id,
            participant_id,
            secret_hash,
            json.dumps(clean_conv_ids),
            int(bool(include_direct)),
            max(0, int(after_id)),
            now,
            expires_at,
        ),
    )
    await db.commit()
    return {
        "signal_id": signal_id,
        "participant_id": participant_id,
        "url": f"/api/signal/{signal_id}",
        "secret": secret,
        "conversation_ids": clean_conv_ids,
        "include_direct": bool(include_direct),
        "after_id": max(0, int(after_id)),
        "expires_at": expires_at,
    }


async def revoke_signals(
    db: Any,
    *,
    signal_id: str | None = None,
    participant_id: str | None = None,
) -> int:
    if signal_id:
        cur = await db.execute("DELETE FROM participant_signals WHERE id = ?", (signal_id,))
    elif participant_id:
        cur = await db.execute(
            "DELETE FROM participant_signals WHERE participant_id = ?",
            (participant_id,),
        )
    else:
        return 0
    await db.commit()
    return int(cur.rowcount or 0)


def compute_is_waking(
    msg_row: Any,
    *,
    recipient_participant_id: str | None,
    recipient_role: str,
    conv_kind: str | None,
    dm_low_id: str | None,
    dm_high_id: str | None,
) -> bool:
    """Classify whether a message should wake/continue an agent turn."""
    data = dict(msg_row)
    sender_pid = data.get("sender_participant_id")
    if recipient_participant_id and sender_pid == recipient_participant_id:
        return False

    if (data.get("priority") or "NORMAL").upper() == "URGENT":
        return True
    if bool(data.get("is_system_notice")):
        return True

    if conv_kind == "direct" and recipient_participant_id in {dm_low_id, dm_high_id}:
        return True

    if recipient_participant_id and data.get("mentions_json"):
        try:
            mentions = json.loads(data["mentions_json"])
            if any(m.get("id") == recipient_participant_id for m in mentions if isinstance(m, dict)):
                return True
        except Exception:
            pass

    sender_role = normalize_role_value(str(data.get("role") or ""))
    msg_type = (data.get("message_type") or "").upper()
    typed_kind = (data.get("typed_kind") or "").upper()
    if recipient_role == "agent" and sender_role == "master":
        if msg_type in {"ORDER", "TASK", "INSTRUCTION", "CANCEL"} or typed_kind in {
            "ORDER",
            "TASK_REQUEST",
        }:
            return True
    if recipient_role == "master" and sender_role == "agent":
        if msg_type in {"EXECUTION", "BLOCKER", "DECISION_NEEDED", "ACTION_REQUIRED"} or typed_kind in {
            "EXECUTION",
            "BLOCKER",
            "DECISION_REQUEST",
        }:
            return True
    return False


def _matches_subscription(
    msg_row: Any,
    *,
    recipient_participant_id: str | None,
    subscribed_conv_ids: set[str] | None,
    include_direct: bool,
    conv_kind: str | None,
    dm_low_id: str | None,
    dm_high_id: str | None,
) -> bool:
    data = dict(msg_row)
    conv_id = data.get("conversation_id") or GENERAL_CONVERSATION_ID
    if conv_kind == "direct":
        if not include_direct:
            return False
        if recipient_participant_id:
            return recipient_participant_id in {dm_low_id, dm_high_id}
        return True

    # Channel message: matches if channel is subscribed OR participant is explicitly @mentioned
    if subscribed_conv_ids is None or conv_id in subscribed_conv_ids:
        return True
    if recipient_participant_id and data.get("mentions_json"):
        try:
            mentions = json.loads(data["mentions_json"])
            if any(m.get("id") == recipient_participant_id for m in mentions if isinstance(m, dict)):
                return True
        except Exception:
            pass
    return False


async def check_signal(
    db: Any,
    *,
    signal_id: str,
    bearer_secret: str,
) -> dict[str, Any]:
    cur = await db.execute(
        """
        SELECT s.*, p.role AS participant_role, p.removed_at AS participant_removed_at
        FROM participant_signals s
        JOIN participants p ON p.id = s.participant_id
        WHERE s.id = ?
        """,
        (signal_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise HubServiceError("E_HUB_SIGNAL_NOT_FOUND", "Signal link not found", status_code=404)
    if not secrets.compare_digest(hash_secret(bearer_secret), row["secret_hash"]):
        raise HubServiceError("E_HUB_SIGNAL_UNAUTHORIZED", "Invalid signal secret", status_code=401)
    now = time.time()
    if float(row["expires_at"]) <= now:
        raise HubServiceError("E_HUB_SIGNAL_EXPIRED", "Signal link expired", status_code=401)
    if row["participant_removed_at"] is not None:
        raise HubServiceError("E_HUB_PARTICIPANT_REMOVED", "Participant removed", status_code=403)

    conv_ids = set(json.loads(row["conversation_ids_json"])) if row["conversation_ids_json"] else None
    include_direct = bool(row["include_direct"])
    after_id = int(row["after_id"])
    participant_id = row["participant_id"]
    participant_role = normalize_role_value(row["participant_role"])

    msg_rows = await db.execute_fetchall(
        """
        SELECT m.id, m.role, m.message_type, m.typed_kind, m.conversation_id,
               m.sender_participant_id, m.priority, m.mentions_json, m.is_system_notice,
               c.kind AS conv_kind, c.participant_low_id, c.participant_high_id
        FROM messages m
        LEFT JOIN conversations c ON c.id = COALESCE(m.conversation_id, 'general')
        WHERE m.deleted_at IS NULL
          AND m.id > ?
        ORDER BY m.id ASC
        LIMIT ?
        """,
        (after_id, SIGNAL_COUNT_PAGE_LIMIT + 1),
    )

    pending = 0
    urgent = 0
    waking = 0
    more = False
    newest_id = after_id
    for item in msg_rows:
        if not _matches_subscription(
            item,
            recipient_participant_id=participant_id,
            subscribed_conv_ids=conv_ids,
            include_direct=include_direct,
            conv_kind=item["conv_kind"] or "channel",
            dm_low_id=item["participant_low_id"],
            dm_high_id=item["participant_high_id"],
        ):
            continue
        if item["sender_participant_id"] == participant_id:
            continue
        if pending >= SIGNAL_COUNT_PAGE_LIMIT:
            more = True
            break
        pending += 1
        newest_id = max(newest_id, int(item["id"]))
        if (item["priority"] or "NORMAL").upper() == "URGENT":
            urgent += 1
        if compute_is_waking(
            item,
            recipient_participant_id=participant_id,
            recipient_role=participant_role,
            conv_kind=item["conv_kind"] or "channel",
            dm_low_id=item["participant_low_id"],
            dm_high_id=item["participant_high_id"],
        ):
            waking += 1

    await db.execute(
        "UPDATE participant_signals SET last_checked_at = ? WHERE id = ?",
        (now, signal_id),
    )
    await db.execute(
        "UPDATE participants SET last_seen_at = ? WHERE id = ?",
        (now, participant_id),
    )
    await db.commit()

    generation = await get_db_generation(db)
    return {
        "pending": pending,
        "urgent": urgent,
        "waking": waking,
        "more": more,
        "after_id": newest_id if pending > 0 else after_id,
        "cursor": format_checkpoint(generation, newest_id if pending > 0 else after_id),
        "expires_at": row["expires_at"],
    }


async def ack_signal(
    db: Any,
    *,
    signal_id: str,
    bearer_secret: str,
    after_id: int,
) -> dict[str, Any]:
    cur = await db.execute("SELECT * FROM participant_signals WHERE id = ?", (signal_id,))
    row = await cur.fetchone()
    if row is None:
        raise HubServiceError("E_HUB_SIGNAL_NOT_FOUND", "Signal link not found", status_code=404)
    if not secrets.compare_digest(hash_secret(bearer_secret), row["secret_hash"]):
        raise HubServiceError("E_HUB_SIGNAL_UNAUTHORIZED", "Invalid signal secret", status_code=401)
    new_after = max(int(row["after_id"]), max(0, int(after_id)))
    await db.execute(
        "UPDATE participant_signals SET after_id = ? WHERE id = ?",
        (new_after, signal_id),
    )
    await db.commit()
    return {"signal_id": signal_id, "after_id": new_after}


async def fetch_updates_page(
    db: Any,
    *,
    after_id: int = 0,
    checkpoint: str | None = None,
    generation: str | None = None,
    recipient_participant_id: str | None = None,
    recipient_role: str = "agent",
    conversation_ids: list[str] | None = None,
    include_direct: bool = True,
    waking_only: bool = False,
    limit: int = 100,
    message_row_converter: Any,
) -> dict[str, Any]:
    current_gen = await get_db_generation(db)
    effective_after = max(0, int(after_id))
    req_gen = generation
    if checkpoint:
        parsed_gen, parsed_id = parse_checkpoint(checkpoint)
        if parsed_gen is not None:
            req_gen = parsed_gen
        effective_after = max(effective_after, parsed_id)

    if req_gen is not None and req_gen != current_gen:
        return {
            "generation_mismatch": True,
            "generation": current_gen,
            "expected_generation": req_gen,
            "after_id": 0,
            "checkpoint": format_checkpoint(current_gen, 0),
            "messages": [],
            "renames": [],
            "pending_count": 0,
            "waking_count": 0,
            "urgent_count": 0,
            "has_more": False,
        }

    subscribed = set(conversation_ids) if conversation_ids is not None else None
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
        (effective_after, limit * 2 + 1),
    )

    matched: list[dict[str, Any]] = []
    waking_count = 0
    urgent_count = 0
    has_more = False
    max_seen_id = effective_after

    for row in rows:
        max_seen_id = max(max_seen_id, int(row["id"]))
        if recipient_participant_id and row["sender_participant_id"] == recipient_participant_id:
            continue
        if not _matches_subscription(
            row,
            recipient_participant_id=recipient_participant_id,
            subscribed_conv_ids=subscribed,
            include_direct=include_direct,
            conv_kind=row["_conv_kind"] or "channel",
            dm_low_id=row["_dm_low"],
            dm_high_id=row["_dm_high"],
        ):
            continue
        is_waking = compute_is_waking(
            row,
            recipient_participant_id=recipient_participant_id,
            recipient_role=recipient_role,
            conv_kind=row["_conv_kind"] or "channel",
            dm_low_id=row["_dm_low"],
            dm_high_id=row["_dm_high"],
        )
        if waking_only and not is_waking:
            continue
        if len(matched) >= limit:
            has_more = True
            break
        if is_waking:
            waking_count += 1
        if (row["priority"] or "NORMAL").upper() == "URGENT":
            urgent_count += 1
        msg_obj = message_row_converter(row)
        msg_dict = msg_obj.model_dump(mode="json") if hasattr(msg_obj, "model_dump") else dict(msg_obj)
        msg_dict["waking"] = is_waking
        matched.append(msg_dict)

    next_after = max_seen_id if rows else effective_after
    if recipient_participant_id and next_after > effective_after:
        await db.execute(
            """
            UPDATE participant_signals
            SET after_id = MAX(after_id, ?)
            WHERE participant_id = ?
            """,
            (next_after, recipient_participant_id),
        )
        await db.commit()

    renames_rows = await db.execute_fetchall(
        """
        SELECT id, participant_id, previous_name, new_name, renamed_by, created_at
        FROM participant_renames
        ORDER BY id DESC
        LIMIT 25
        """
    )

    return {
        "generation_mismatch": False,
        "generation": current_gen,
        "after_id": next_after,
        "checkpoint": format_checkpoint(current_gen, next_after),
        "messages": matched,
        "renames": [dict(r) for r in renames_rows],
        "pending_count": len(matched),
        "waking_count": waking_count,
        "urgent_count": urgent_count,
        "has_more": has_more,
    }


async def search_messages_fts(
    db: Any,
    *,
    query: str,
    conversation_id: str | None = None,
    role: str | None = None,
    order_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    message_select: str,
    message_row_converter: Any,
) -> list[Any]:
    fts_expr = sanitize_fts_query(query)
    if not fts_expr:
        return []

    select_cols = ", ".join(f"m.{field.strip()}" for field in message_select.split(","))
    clauses = ["messages_fts MATCH ?", "m.deleted_at IS NULL"]
    params: list[Any] = [fts_expr]

    if conversation_id:
        clauses.append("COALESCE(m.conversation_id, 'general') = ?")
        params.append(conversation_id)
    if role:
        norm = normalize_role_value(role)
        if norm == "master":
            clauses.append("m.role IN ('master', 'engineer')")
        elif norm == "agent":
            clauses.append("m.role IN ('agent', 'worker')")
        else:
            clauses.append("m.role = ?")
            params.append(norm)
    if order_id:
        clauses.append("(m.order_id = ? OR m.task_id = ?)")
        params.extend([order_id, order_id])
    if status:
        clauses.append("m.status = ?")
        params.append(status.upper())

    params.append(max(1, min(limit, 200)))
    where_sql = " AND ".join(clauses)
    rows = await db.execute_fetchall(
        f"""
        SELECT {select_cols}
        FROM messages_fts
        JOIN messages m ON m.id = messages_fts.rowid
        WHERE {where_sql}
        ORDER BY bm25(messages_fts), m.id DESC
        LIMIT ?
        """,
        tuple(params),
    )
    return [message_row_converter(row) for row in rows]


async def get_hub_context(
    db: Any,
    *,
    participant_id: str | None = None,
    message_select: str,
    message_row_converter: Any,
) -> dict[str, Any]:
    generation = await get_db_generation(db)
    participants = await list_participants(db, include_removed=False)
    conversations = await list_conversations(db, include_archived=True)
    general_rows = await db.execute_fetchall(
        f"""
        SELECT {message_select}
        FROM messages
        WHERE deleted_at IS NULL
          AND COALESCE(conversation_id, 'general') = 'general'
        ORDER BY id DESC
        LIMIT 10
        """
    )
    general_latest = [message_row_converter(r).model_dump(mode="json") for r in reversed(general_rows)]
    cur = await db.execute("SELECT COALESCE(MAX(id), 0) AS max_id FROM messages WHERE deleted_at IS NULL")
    max_id = int((await cur.fetchone())["max_id"])
    result: dict[str, Any] = {
        "generation": generation,
        "latest_message_id": max_id,
        "checkpoint": format_checkpoint(generation, max_id),
        "participants": participants,
        "channels": [c for c in conversations if c["kind"] == "channel"],
        "direct_chats": [
            c
            for c in conversations
            if c["kind"] == "direct"
            and (
                participant_id is None
                or participant_id in {c.get("participant_low_id"), c.get("participant_high_id")}
            )
        ],
        "general_latest": general_latest,
    }
    if participant_id:
        result["self"] = await get_participant(db, participant_id)
    return result
