"""Resilience primitives: long-poll message notifier, stuck-reader tracker, and cursor formatting."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("bridge.resilience")


class MessageNotifier:
    """In-memory pub/sub notifier for long-polling readers."""

    def __init__(self) -> None:
        self._waiters: set[asyncio.Event] = set()

    def notify(self) -> None:
        for waiter in list(self._waiters):
            waiter.set()

    def clear(self) -> None:
        self._waiters.clear()

    async def wait_for_message(self, timeout: float, request: Any = None) -> bool:
        """Wait up to ``timeout`` seconds for a new message notification.

        Returns True if notified, False on timeout or client disconnect.
        """
        if timeout <= 0:
            return False
        event = asyncio.Event()
        self._waiters.add(event)
        deadline = time.monotonic() + timeout
        try:
            while not event.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                if request is not None and hasattr(request, "is_disconnected"):
                    if await request.is_disconnected():
                        return False
                    step = min(remaining, 1.0)
                else:
                    step = remaining
                try:
                    await asyncio.wait_for(event.wait(), timeout=step)
                    return True
                except TimeoutError:
                    continue
            return True
        finally:
            self._waiters.discard(event)


@dataclass
class _ReaderState:
    checkpoint: str
    polls: int
    reported: bool
    first_seen_at: float
    last_seen_at: float
    plugin: str | None = None


class StuckReadersTracker:
    """Detect readers polling the same checkpoint repeatedly while messages wait."""

    def __init__(self, threshold: int = 5) -> None:
        self.threshold = max(2, threshold)
        self._readers: dict[str, _ReaderState] = {}

    def observe(
        self,
        reader_key: str,
        checkpoint: str,
        waiting_count: int,
        plugin: str | None = None,
    ) -> dict[str, Any] | None:
        now = time.time()
        if waiting_count <= 0:
            self._readers.pop(reader_key, None)
            return None

        state = self._readers.get(reader_key)
        if state is None or state.checkpoint != checkpoint:
            self._readers[reader_key] = _ReaderState(
                checkpoint=checkpoint,
                polls=1,
                reported=False,
                first_seen_at=now,
                last_seen_at=now,
                plugin=plugin,
            )
            return None

        state.polls += 1
        state.last_seen_at = now
        if plugin is not None:
            state.plugin = plugin

        if state.polls < self.threshold:
            return None

        newly_reported = not state.reported
        state.reported = True
        alert = {
            "reader_key": reader_key,
            "checkpoint": state.checkpoint,
            "polls": state.polls,
            "plugin": state.plugin,
            "first_seen_at": state.first_seen_at,
            "last_seen_at": state.last_seen_at,
        }
        if newly_reported:
            logger.warning(
                "Stuck reader detected: %s polled checkpoint %s %d times with pending messages",
                reader_key,
                checkpoint,
                state.polls,
            )
        return alert

    def list_stuck(self) -> list[dict[str, Any]]:
        return [
            {
                "reader_key": key,
                "checkpoint": state.checkpoint,
                "polls": state.polls,
                "plugin": state.plugin,
                "first_seen_at": state.first_seen_at,
                "last_seen_at": state.last_seen_at,
            }
            for key, state in self._readers.items()
            if state.reported
        ]

    def clear(self) -> None:
        self._readers.clear()


def format_checkpoint(generation: str, after_id: int) -> str:
    return f"{generation}:{max(0, int(after_id))}"


def parse_checkpoint(checkpoint: str | None) -> tuple[str | None, int]:
    """Parse ``<generation>:<after_id>`` or integer string into ``(generation, after_id)``."""
    if not checkpoint:
        return None, 0
    raw = checkpoint.strip()
    if ":" in raw:
        gen_part, _, id_part = raw.partition(":")
        try:
            parsed_id = max(0, int(id_part))
        except ValueError as exc:
            raise ValueError("checkpoint message id must be a non-negative integer") from exc
        if not gen_part:
            raise ValueError("checkpoint generation prefix cannot be empty")
        return gen_part, parsed_id
    try:
        return None, max(0, int(raw))
    except ValueError as exc:
        raise ValueError("checkpoint must be an integer or '<generation>:<id>'") from exc
