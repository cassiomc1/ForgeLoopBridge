"""Out-of-band file transfer management for ForgeLoopBridge."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from .service import HubServiceError

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def validate_transfer_name(raw: str) -> str:
    name = raw.strip()
    if not name:
        raise HubServiceError("E_TRANSFER_INVALID_NAME", "Transfer filename cannot be empty", status_code=422)
    if len(name.encode("utf-8")) > 255:
        raise HubServiceError("E_TRANSFER_INVALID_NAME", "Transfer filename exceeds 255 bytes", status_code=422)
    if "/" in name or "\\" in name or ".." in name:
        raise HubServiceError("E_TRANSFER_INVALID_NAME", "Transfer filename cannot contain path separators", status_code=422)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise HubServiceError("E_TRANSFER_INVALID_NAME", "Transfer filename contains invalid control characters", status_code=422)
    return name


def validate_sha256(raw: str) -> str:
    digest = raw.strip().lower()
    if not _SHA256_RE.match(digest):
        raise HubServiceError("E_TRANSFER_INVALID_SHA256", "sha256 must be 64 lowercase hex characters", status_code=422)
    return digest


def resolve_safe_blob_path(uploads_dir: Path, storage_relpath: str) -> Path:
    base = uploads_dir.resolve()
    candidate = (base / storage_relpath).resolve()
    if candidate == base or not candidate.is_relative_to(base):
        raise HubServiceError("E_TRANSFER_PATH_INVALID", "Invalid transfer storage path", status_code=400)
    return candidate


def get_pack_command(folder: str = "<folder>") -> dict[str, str]:
    safe_folder = folder.strip() or "<folder>"
    cmd = (
        f'OUT="/tmp/bridge-pack-$(date +%s).tar.gz" && '
        f'tar -czf "$OUT" -C "{safe_folder}" . && '
        f'BYTES=$(wc -c < "$OUT" | tr -d " ") && '
        f'FILES=$(tar -tzf "$OUT" | wc -l | tr -d " ") && '
        f'SHA=$( (shasum -a 256 "$OUT" 2>/dev/null || sha256sum "$OUT") | awk "{{print \\$1}}" ) && '
        f'printf "archive=%s\\nsize_bytes=%s\\nfile_count=%s\\nsha256=%s\\n" "$OUT" "$BYTES" "$FILES" "$SHA"'
    )
    return {
        "command": cmd,
        "notes": "Run locally to pack a folder into .tar.gz and obtain size_bytes, file_count, and sha256 for prepare_transfer.",
    }


def sanitize_content_disposition_filename(name: str) -> str:
    cleaned = re.sub(r'["\\\r\n;]', "_", name)
    return cleaned or "download.bin"


class TransferTicketStore:
    """Short-lived in-memory download tickets (never embeds auth tokens in URLs)."""

    def __init__(self) -> None:
        self._tickets: dict[str, tuple[str, float]] = {}

    def _purge(self, now: float) -> None:
        for key, (_, exp) in list(self._tickets.items()):
            if exp <= now:
                self._tickets.pop(key, None)

    def issue(self, transfer_id: str, ttl_seconds: float) -> tuple[str, float]:
        now = time.monotonic()
        self._purge(now)
        ticket = secrets.token_urlsafe(32)
        self._tickets[ticket] = (transfer_id, now + ttl_seconds)
        return ticket, ttl_seconds

    def validate(self, ticket: str, transfer_id: str) -> bool:
        now = time.monotonic()
        self._purge(now)
        entry = self._tickets.get(ticket)
        if entry is None:
            return False
        stored_id, expires_at = entry
        return stored_id == transfer_id and expires_at > now

    def clear(self) -> None:
        self._tickets.clear()


def _transfer_dict(row: Any, *, now: float | None = None) -> dict[str, Any]:
    current = now if now is not None else time.time()
    data = dict(row)
    expired = float(data["expires_at"]) <= current
    status = "EXPIRED" if expired and data["status"] != "EXPIRED" else data["status"]
    return {
        "id": data["id"],
        "sender_role": data["sender_role"],
        "sender_participant_id": data.get("sender_participant_id"),
        "name": data["name"],
        "kind": data["kind"],
        "size_bytes": int(data["size_bytes"]),
        "sha256": data["sha256"],
        "content_type": data.get("content_type"),
        "file_count": data.get("file_count"),
        "status": status,
        "message_id": data.get("message_id"),
        "conversation_id": data.get("conversation_id"),
        "created_at": data["created_at"],
        "expires_at": data["expires_at"],
        "expired": expired,
    }


async def prepare_transfer(
    db: Any,
    *,
    sender_role: str,
    sender_participant_id: str | None,
    name: str,
    kind: str,
    size_bytes: int,
    sha256: str,
    content_type: str | None = None,
    file_count: int | None = None,
    conversation_id: str | None = None,
    max_transfer_bytes: int,
    ttl_days: int,
) -> dict[str, Any]:
    clean_name = validate_transfer_name(name)
    clean_kind = kind.strip().lower()
    if clean_kind not in {"file", "folder"}:
        raise HubServiceError("E_TRANSFER_INVALID_KIND", "kind must be 'file' or 'folder'", status_code=422)
    if size_bytes <= 0:
        raise HubServiceError("E_TRANSFER_INVALID_SIZE", "size_bytes must be positive", status_code=422)
    if size_bytes > max_transfer_bytes:
        raise HubServiceError(
            "E_TRANSFER_TOO_LARGE",
            f"Transfer size {size_bytes} exceeds maximum {max_transfer_bytes} bytes",
            status_code=413,
        )
    clean_sha = validate_sha256(sha256)
    if clean_kind == "folder" and file_count is not None and file_count < 0:
        raise HubServiceError("E_TRANSFER_INVALID_COUNT", "file_count cannot be negative", status_code=422)

    transfer_id = str(uuid.uuid4())
    now = time.time()
    expires_at = now + ttl_days * 86_400
    relpath = f"{transfer_id}.bin"

    await db.execute(
        """
        INSERT INTO file_transfers (
            id, sender_role, sender_participant_id, name, kind, size_bytes, sha256,
            content_type, file_count, storage_relpath, status, conversation_id,
            created_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PREPARED', ?, ?, ?)
        """,
        (
            transfer_id,
            sender_role,
            sender_participant_id,
            clean_name,
            clean_kind,
            int(size_bytes),
            clean_sha,
            content_type,
            file_count,
            relpath,
            conversation_id,
            now,
            expires_at,
        ),
    )
    await db.commit()
    return {
        "transfer_id": transfer_id,
        "name": clean_name,
        "kind": clean_kind,
        "size_bytes": int(size_bytes),
        "sha256": clean_sha,
        "status": "PREPARED",
        "upload_url": f"/api/transfers/{transfer_id}/upload",
        "max_bytes": max_transfer_bytes,
        "expires_at": expires_at,
        "upload_command": (
            f'curl -fsS -X PUT -H "Authorization: Bearer $BRIDGE_TOKEN" '
            f'--data-binary "@<local_file_path>" "$BRIDGE_URL/api/transfers/{transfer_id}/upload"'
        ),
    }


async def stream_upload_transfer(
    db: Any,
    *,
    transfer_id: str,
    uploads_dir: Path,
    chunks: AsyncIterator[bytes],
    max_transfer_bytes: int,
) -> dict[str, Any]:
    cur = await db.execute("SELECT * FROM file_transfers WHERE id = ?", (transfer_id,))
    row = await cur.fetchone()
    if row is None:
        raise HubServiceError("E_TRANSFER_NOT_FOUND", "Transfer not found", status_code=404)
    now = time.time()
    if float(row["expires_at"]) <= now or row["status"] == "EXPIRED":
        raise HubServiceError("E_TRANSFER_EXPIRED", "Transfer has expired", status_code=410)
    if row["status"] != "PREPARED":
        raise HubServiceError(
            "E_TRANSFER_ALREADY_UPLOADED",
            f"Transfer is already in state '{row['status']}'",
            status_code=412,
        )

    expected_size = int(row["size_bytes"])
    expected_sha = row["sha256"]
    uploads_dir.mkdir(parents=True, exist_ok=True)
    target_path = resolve_safe_blob_path(uploads_dir, row["storage_relpath"])
    tmp_path = resolve_safe_blob_path(uploads_dir, f".{transfer_id}.{secrets.token_hex(6)}.tmp")

    hasher = hashlib.sha256()
    received = 0
    try:
        with tmp_path.open("wb") as handle:
            async for chunk in chunks:
                if not chunk:
                    continue
                received += len(chunk)
                if received > max_transfer_bytes or received > expected_size:
                    raise HubServiceError(
                        "E_TRANSFER_SIZE_EXCEEDED",
                        f"Uploaded bytes exceed declared size ({expected_size})",
                        status_code=413,
                    )
                hasher.update(chunk)
                handle.write(chunk)

        if received != expected_size:
            raise HubServiceError(
                "E_TRANSFER_SIZE_MISMATCH",
                f"Expected {expected_size} bytes, received {received}",
                status_code=400,
            )
        digest = hasher.hexdigest()
        if digest != expected_sha:
            raise HubServiceError(
                "E_TRANSFER_SHA256_MISMATCH",
                f"SHA-256 mismatch (expected {expected_sha}, got {digest})",
                status_code=400,
            )
        os.replace(tmp_path, target_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    await db.execute(
        "UPDATE file_transfers SET status = 'UPLOADED' WHERE id = ?",
        (transfer_id,),
    )
    await db.commit()
    cur = await db.execute("SELECT * FROM file_transfers WHERE id = ?", (transfer_id,))
    return _transfer_dict(await cur.fetchone(), now=now)


async def cleanup_expired_transfers(db: Any, *, uploads_dir: Path) -> int:
    now = time.time()
    rows = await db.execute_fetchall(
        "SELECT id, storage_relpath FROM file_transfers WHERE expires_at <= ? AND status <> 'EXPIRED'",
        (now,),
    )
    cleaned = 0
    for row in rows:
        try:
            path = resolve_safe_blob_path(uploads_dir, row["storage_relpath"])
            path.unlink(missing_ok=True)
        except Exception:
            pass
        await db.execute(
            "UPDATE file_transfers SET status = 'EXPIRED' WHERE id = ?",
            (row["id"],),
        )
        cleaned += 1
    if cleaned:
        await db.commit()
    return cleaned


async def list_transfers(
    db: Any,
    *,
    uploads_dir: Path,
    conversation_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    await cleanup_expired_transfers(db, uploads_dir=uploads_dir)
    now = time.time()
    clamped = max(1, min(limit, 500))
    if conversation_id:
        rows = await db.execute_fetchall(
            """
            SELECT * FROM file_transfers
            WHERE conversation_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (conversation_id, clamped),
        )
    else:
        rows = await db.execute_fetchall(
            """
            SELECT * FROM file_transfers
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (clamped,),
        )
    return [_transfer_dict(row, now=now) for row in rows]


async def get_transfer_details(
    db: Any,
    *,
    transfer_id: str,
    uploads_dir: Path,
) -> dict[str, Any]:
    await cleanup_expired_transfers(db, uploads_dir=uploads_dir)
    cur = await db.execute("SELECT * FROM file_transfers WHERE id = ?", (transfer_id,))
    row = await cur.fetchone()
    if row is None:
        raise HubServiceError("E_TRANSFER_NOT_FOUND", "Transfer not found", status_code=404)
    receipts = await db.execute_fetchall(
        """
        SELECT reader_key, participant_id, first_fetched_at, fetch_count
        FROM file_transfer_receipts
        WHERE transfer_id = ?
        ORDER BY first_fetched_at ASC
        """,
        (transfer_id,),
    )
    info = _transfer_dict(row)
    info["receipts"] = [dict(r) for r in receipts]
    return info


async def record_transfer_fetch(
    db: Any,
    *,
    transfer_id: str,
    reader_key: str,
    participant_id: str | None,
) -> None:
    now = time.time()
    await db.execute(
        """
        INSERT INTO file_transfer_receipts (transfer_id, reader_key, participant_id, first_fetched_at, fetch_count)
        VALUES (?, ?, ?, ?, 1)
        ON CONFLICT(transfer_id, reader_key) DO UPDATE SET fetch_count = fetch_count + 1
        """,
        (transfer_id, reader_key, participant_id, now),
    )
    await db.commit()
