"""Auto-start / outbound wakes scheduler with debounce, cadence, budgets, and SSRF protection."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import shlex
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from .service import HubServiceError, compute_is_waking, get_participant, normalize_role_value

logger = logging.getLogger("bridge.wakes")

CADENCE_SECONDS: dict[str, float] = {
    "right_away": 0.0,
    "every_minute": 60.0,
    "every_5_minutes": 300.0,
    "every_15_minutes": 900.0,
}

VALID_WAKE_KINDS = frozenset({"webhook", "claude_routine", "local_command"})


def _validate_wake_url(url: str, *, allow_private: bool) -> str:
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HubServiceError("E_WAKE_INVALID_URL", "Wake URL must be an http(s) URL with a hostname", status_code=422)
    host = parsed.hostname.lower()
    if not allow_private:
        if host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".local"):
            raise HubServiceError(
                "E_WAKE_PRIVATE_URL_BLOCKED",
                "Private or loopback wake URLs require WAKE_ALLOW_PRIVATE_URLS=1",
                status_code=422,
            )
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                raise HubServiceError(
                    "E_WAKE_PRIVATE_URL_BLOCKED",
                    "Private IP wake URLs require WAKE_ALLOW_PRIVATE_URLS=1",
                    status_code=422,
                )
        except ValueError:
            try:
                infos = socket.getaddrinfo(host, None)
                for info in infos:
                    ip = ipaddress.ip_address(info[4][0])
                    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                        raise HubServiceError(
                            "E_WAKE_PRIVATE_URL_BLOCKED",
                            "Resolved private IP wake URL requires WAKE_ALLOW_PRIVATE_URLS=1",
                            status_code=422,
                        )
            except socket.gaierror:
                # Hostname may only be resolvable from the target runtime; allow DNS deferral unless obviously private.
                pass
    return parsed.geturl()


def backoff_seconds(failures: int) -> float:
    return float(min(30 * (2 ** max(0, failures - 1)), 480))


def _wake_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    return {
        "id": data["id"],
        "participant_id": data["participant_id"],
        "kind": data["kind"],
        "url": data.get("url"),
        "command": json.loads(data["command_json"]) if data.get("command_json") else None,
        "secret_hint": data.get("secret_hint"),
        "channel_ids": json.loads(data["channel_ids_json"]) if data.get("channel_ids_json") else [],
        "direct_chats": data.get("direct_chats") or "own",
        "delivered_through": int(data.get("delivered_through") or 0),
        "min_interval_seconds": float(data.get("min_interval_seconds") or 60.0),
        "debounce_seconds": float(data.get("debounce_seconds") or 10.0),
        "due_at": data.get("due_at"),
        "failures": int(data.get("failures") or 0),
        "max_attempts": int(data.get("max_attempts") or 5),
        "last_attempt_at": data.get("last_attempt_at"),
        "last_sent_at": data.get("last_sent_at"),
        "last_error": data.get("last_error"),
        "disabled_at": data.get("disabled_at"),
        "disabled_reason": data.get("disabled_reason"),
        "created_at": data["created_at"],
    }


def _http_post_sync(url: str, payload: dict[str, Any], secret: str | None, timeout: float) -> tuple[int, str]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "ForgeLoopBridge/4.0",
    }
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
        headers["X-Bridge-Wake-Secret"] = secret
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), "ok"
    except urllib.error.HTTPError as exc:
        return int(exc.code), f"http_{exc.code}"
    except urllib.error.URLError as exc:
        return 0, f"connection:{type(exc.reason).__name__}"
    except TimeoutError:
        return 0, "timeout"


async def _default_http_sender(url: str, payload: dict[str, Any], secret: str | None, timeout: float) -> tuple[bool, str]:
    status, reason = await asyncio.to_thread(_http_post_sync, url, payload, secret, timeout)
    return (200 <= status < 300), reason


async def _default_command_runner(command: list[str], payload: dict[str, Any], timeout: float) -> tuple[bool, str]:
    env = os.environ.copy()
    env["BRIDGE_WAKE_PAYLOAD"] = json.dumps(payload)
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        _, _stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return False, "timeout"
    return (proc.returncode == 0), ("ok" if proc.returncode == 0 else f"exit_{proc.returncode}")


HttpSender = Callable[[str, dict[str, Any], str | None, float], Awaitable[tuple[bool, str]]]
CommandRunner = Callable[[list[str], dict[str, Any], float], Awaitable[tuple[bool, str]]]


class WakeScheduler:
    """Manages outbound auto-start wake scheduling, rate budgets, and retries."""

    def __init__(
        self,
        db_connector: Any,
        *,
        allow_local_commands: Callable[[], bool] | None = None,
        allow_private_urls: Callable[[], bool] | None = None,
        hourly_budget: Callable[[], int] | None = None,
        daily_budget: Callable[[], int] | None = None,
        timeout_seconds: Callable[[], float] | None = None,
        http_sender: HttpSender | None = None,
        command_runner: CommandRunner | None = None,
    ) -> None:
        self._connect_db = db_connector
        self._allow_local = allow_local_commands or (lambda: False)
        self._allow_private = allow_private_urls or (lambda: False)
        self._hourly_budget = hourly_budget or (lambda: 60)
        self._daily_budget = daily_budget or (lambda: 480)
        self._timeout = timeout_seconds or (lambda: 5.0)
        self._http_sender: HttpSender = http_sender or _default_http_sender
        self._command_runner: CommandRunner = command_runner or _default_command_runner
        self._wakeup = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopped = False

    def set_transports(
        self,
        *,
        http_sender: HttpSender | None = None,
        command_runner: CommandRunner | None = None,
    ) -> None:
        if http_sender is not None:
            self._http_sender = http_sender
        if command_runner is not None:
            self._command_runner = command_runner

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopped = False
        self._task = asyncio.create_task(self._loop(), name="bridge-wake-scheduler")

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

    async def _loop(self) -> None:
        while not self._stopped:
            try:
                await self.dispatch_due()
                next_due = await self._next_due_delay()
                self._wakeup.clear()
                if next_due is None:
                    await self._wakeup.wait()
                else:
                    try:
                        await asyncio.wait_for(self._wakeup.wait(), timeout=max(0.05, min(next_due, 30.0)))
                    except TimeoutError:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Wake loop iteration error: %s", exc)
                await asyncio.sleep(1.0)

    async def _next_due_delay(self) -> float | None:
        async with self._connect_db() as db:
            cur = await db.execute(
                """
                SELECT MIN(due_at) AS next_due
                FROM wake_targets
                WHERE disabled_at IS NULL AND due_at IS NOT NULL
                """
            )
            row = await cur.fetchone()
        if row is None or row["next_due"] is None:
            return None
        return max(0.0, float(row["next_due"]) - time.time())

    async def create_target(
        self,
        db: Any,
        *,
        participant_id: str,
        kind: str,
        url: str | None = None,
        command: list[str] | str | None = None,
        secret: str | None = None,
        channel_ids: list[str] | None = None,
        direct_chats: str = "own",
        debounce_seconds: float = 10.0,
        min_interval_seconds: float = 60.0,
        max_attempts: int = 5,
    ) -> dict[str, Any]:
        part = await get_participant(db, participant_id)
        if part is None:
            raise HubServiceError("E_HUB_PARTICIPANT_NOT_FOUND", "Participant not found", status_code=404)
        if part["removed_at"] is not None:
            raise HubServiceError("E_HUB_PARTICIPANT_REMOVED", "Participant is removed", status_code=403)

        clean_kind = kind.strip().lower()
        if clean_kind not in VALID_WAKE_KINDS:
            raise HubServiceError(
                "E_WAKE_INVALID_KIND",
                f"Wake kind must be one of {sorted(VALID_WAKE_KINDS)}",
                status_code=422,
            )
        if direct_chats not in {"own", "none"}:
            raise HubServiceError("E_WAKE_INVALID_DIRECT", "direct_chats must be 'own' or 'none'", status_code=422)

        validated_url: str | None = None
        cmd_list: list[str] | None = None
        if clean_kind in {"webhook", "claude_routine"}:
            if not url:
                raise HubServiceError("E_WAKE_URL_REQUIRED", "url is required for webhook/claude_routine wakes", status_code=422)
            validated_url = _validate_wake_url(url, allow_private=self._allow_private())
        else:
            if not self._allow_local():
                raise HubServiceError(
                    "E_WAKE_LOCAL_DISABLED",
                    "Local wake commands are disabled (set ENABLE_LOCAL_WAKE_COMMANDS=1 to enable)",
                    status_code=403,
                )
            if isinstance(command, str):
                cmd_list = shlex.split(command)
            elif isinstance(command, list):
                cmd_list = [str(item) for item in command if str(item)]
            if not cmd_list:
                raise HubServiceError("E_WAKE_COMMAND_REQUIRED", "command must be a non-empty argv list", status_code=422)

        secret_hint = secret[-4:] if secret else None
        wake_id = str(uuid.uuid4())
        now = time.time()
        await db.execute(
            """
            INSERT INTO wake_targets (
                id, participant_id, kind, url, command_json, secret_encrypted, secret_hint,
                channel_ids_json, direct_chats, min_interval_seconds, debounce_seconds,
                max_attempts, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                wake_id,
                participant_id,
                clean_kind,
                validated_url,
                json.dumps(cmd_list) if cmd_list is not None else None,
                secret,
                secret_hint,
                json.dumps(list(dict.fromkeys(channel_ids or ["general"]))),
                direct_chats,
                max(0.0, float(min_interval_seconds)),
                max(0.0, float(debounce_seconds)),
                max(1, min(int(max_attempts), 20)),
                now,
            ),
        )
        await db.commit()
        cur = await db.execute("SELECT * FROM wake_targets WHERE id = ?", (wake_id,))
        return _wake_dict(await cur.fetchone())

    async def list_targets(self, db: Any, *, participant_id: str | None = None) -> list[dict[str, Any]]:
        if participant_id:
            rows = await db.execute_fetchall(
                "SELECT * FROM wake_targets WHERE participant_id = ? ORDER BY created_at DESC",
                (participant_id,),
            )
        else:
            rows = await db.execute_fetchall("SELECT * FROM wake_targets ORDER BY created_at DESC")
        return [_wake_dict(r) for r in rows]

    async def delete_target(self, db: Any, wake_id: str) -> int:
        cur = await db.execute("DELETE FROM wake_targets WHERE id = ?", (wake_id,))
        await db.commit()
        return int(cur.rowcount or 0)

    async def schedule_for_message(
        self,
        db: Any,
        *,
        msg_row: dict[str, Any],
        conv_kind: str,
        dm_low_id: str | None,
        dm_high_id: str | None,
        now: float | None = None,
    ) -> int:
        current = now if now is not None else time.time()
        targets = await db.execute_fetchall(
            """
            SELECT w.*, p.role AS participant_role, p.wake_cadence, p.removed_at
            FROM wake_targets w
            JOIN participants p ON p.id = w.participant_id
            WHERE w.disabled_at IS NULL AND p.removed_at IS NULL
            """
        )
        scheduled = 0
        conv_id = msg_row.get("conversation_id") or "general"
        is_urgent = (msg_row.get("priority") or "NORMAL").upper() == "URGENT"
        sender_pid = msg_row.get("sender_participant_id")

        mentions_ids: set[str] = set()
        if msg_row.get("mentions_json"):
            try:
                for item in json.loads(msg_row["mentions_json"]):
                    if isinstance(item, dict) and item.get("id"):
                        mentions_ids.add(item["id"])
            except Exception:
                pass

        for target in targets:
            pid = target["participant_id"]
            if sender_pid and sender_pid == pid:
                continue

            watched_channels = set(json.loads(target["channel_ids_json"] or "[]"))
            in_dm = conv_kind == "direct" and pid in {dm_low_id, dm_high_id}
            if conv_kind == "direct":
                if not in_dm or target["direct_chats"] == "none":
                    continue
                matches = True
            else:
                is_mentioned = pid in mentions_ids
                is_waking_msg = compute_is_waking(
                    msg_row,
                    recipient_participant_id=pid,
                    recipient_role=normalize_role_value(target["participant_role"]),
                    conv_kind=conv_kind,
                    dm_low_id=dm_low_id,
                    dm_high_id=dm_high_id,
                )
                matches = is_mentioned or (conv_id in watched_channels) or is_waking_msg

            if not matches:
                continue

            if is_urgent and int(target["failures"]) == 0 and target["last_error"] != "budget":
                candidate_due = current
            else:
                cadence_gap = CADENCE_SECONDS.get(target["wake_cadence"] or "every_minute", 60.0)
                effective_gap = max(float(target["min_interval_seconds"]), cadence_gap)
                last_sent = float(target["last_sent_at"] or 0.0)
                candidate_due = max(current + float(target["debounce_seconds"]), last_sent + effective_gap)

            existing_due = target["due_at"]
            next_due = candidate_due if existing_due is None else min(float(existing_due), candidate_due)
            await db.execute(
                "UPDATE wake_targets SET due_at = ? WHERE id = ?",
                (next_due, target["id"]),
            )
            scheduled += 1

        if scheduled:
            await db.commit()
            self._wakeup.set()
        return scheduled

    async def _within_budget(self, db: Any, participant_id: str, now: float) -> bool:
        hour_bucket = int(now // 3600)
        cur = await db.execute(
            "SELECT sent FROM wake_sends WHERE participant_id = ? AND hour_bucket = ?",
            (participant_id, hour_bucket),
        )
        hour_row = await cur.fetchone()
        hourly_used = int(hour_row["sent"]) if hour_row else 0
        if hourly_used >= self._hourly_budget():
            return False

        day_start_bucket = hour_bucket - 23
        cur = await db.execute(
            "SELECT COALESCE(SUM(sent), 0) AS total FROM wake_sends WHERE participant_id = ? AND hour_bucket >= ?",
            (participant_id, day_start_bucket),
        )
        daily_used = int((await cur.fetchone())["total"])
        return daily_used < self._daily_budget()

    async def _record_budget_send(self, db: Any, participant_id: str, now: float) -> None:
        hour_bucket = int(now // 3600)
        await db.execute(
            """
            INSERT INTO wake_sends (participant_id, hour_bucket, sent)
            VALUES (?, ?, 1)
            ON CONFLICT(participant_id, hour_bucket) DO UPDATE SET sent = sent + 1
            """,
            (participant_id, hour_bucket),
        )

    async def dispatch_due(
        self,
        *,
        now: float | None = None,
        force_target_id: str | None = None,
    ) -> list[dict[str, Any]]:
        current = now if now is not None else time.time()
        results: list[dict[str, Any]] = []
        async with self._connect_db() as db:
            if force_target_id is not None:
                targets = await db.execute_fetchall(
                    """
                    SELECT w.*, p.name AS participant_name
                    FROM wake_targets w
                    JOIN participants p ON p.id = w.participant_id
                    WHERE w.id = ? AND p.removed_at IS NULL
                    """,
                    (force_target_id,),
                )
            else:
                targets = await db.execute_fetchall(
                    """
                    SELECT w.*, p.name AS participant_name
                    FROM wake_targets w
                    JOIN participants p ON p.id = w.participant_id
                    WHERE w.disabled_at IS NULL
                      AND p.removed_at IS NULL
                      AND w.due_at IS NOT NULL
                      AND w.due_at <= ?
                    ORDER BY w.due_at ASC
                    LIMIT 16
                    """,
                    (current,),
                )

            cur = await db.execute(
                "SELECT COALESCE(MAX(id), 0) AS max_id FROM messages WHERE deleted_at IS NULL"
            )
            latest_msg_id = int((await cur.fetchone())["max_id"])

            for target in targets:
                pid = target["participant_id"]
                if force_target_id is None and not await self._within_budget(db, pid, current):
                    next_hour = (int(current // 3600) + 1) * 3600.0
                    await db.execute(
                        "UPDATE wake_targets SET due_at = ?, last_error = 'budget', last_attempt_at = ? WHERE id = ?",
                        (next_hour, current, target["id"]),
                    )
                    results.append({"id": target["id"], "ok": False, "error": "budget"})
                    continue

                payload = {
                    "event": "bridge.wake",
                    "wake_id": target["id"],
                    "participant_id": pid,
                    "participant_name": target["participant_name"],
                    "latest_message_id": latest_msg_id,
                    "delivered_through": int(target["delivered_through"] or 0),
                    "test": force_target_id is not None,
                    "timestamp": current,
                }
                await self._record_budget_send(db, pid, current)

                if target["kind"] in {"webhook", "claude_routine"}:
                    ok, reason = await self._http_sender(
                        target["url"],
                        payload,
                        target["secret_encrypted"],
                        self._timeout(),
                    )
                else:
                    if not self._allow_local():
                        ok, reason = False, "local_commands_disabled"
                    else:
                        cmd = json.loads(target["command_json"] or "[]")
                        ok, reason = await self._command_runner(cmd, payload, self._timeout())

                if ok:
                    await db.execute(
                        """
                        UPDATE wake_targets
                        SET due_at = NULL,
                            delivered_through = ?,
                            failures = 0,
                            last_attempt_at = ?,
                            last_sent_at = ?,
                            last_error = NULL
                        WHERE id = ?
                        """,
                        (latest_msg_id, current, current, target["id"]),
                    )
                    results.append({"id": target["id"], "ok": True, "reason": reason})
                else:
                    failures = int(target["failures"] or 0) + 1
                    max_attempts = int(target["max_attempts"] or 5)
                    if failures >= max_attempts:
                        await db.execute(
                            """
                            UPDATE wake_targets
                            SET due_at = NULL,
                                failures = ?,
                                last_attempt_at = ?,
                                last_error = ?,
                                disabled_at = ?,
                                disabled_reason = ?
                            WHERE id = ?
                            """,
                            (failures, current, reason[:64], current, f"max_attempts:{reason}"[:64], target["id"]),
                        )
                    else:
                        retry_at = current + backoff_seconds(failures)
                        await db.execute(
                            """
                            UPDATE wake_targets
                            SET due_at = ?,
                                failures = ?,
                                last_attempt_at = ?,
                                last_error = ?
                            WHERE id = ?
                            """,
                            (retry_at, failures, current, reason[:64], target["id"]),
                        )
                    results.append({"id": target["id"], "ok": False, "error": reason, "failures": failures})

            await db.commit()
        return results
