"""@mention extraction and participant resolution for ForgeLoopBridge."""

from __future__ import annotations

import re
from typing import Any

# Strip fenced code blocks and inline code spans before matching handles.
_CODE_SPAN_RE = re.compile(r"```[\s\S]*?(?:```|$)|`[^`\n]*`")
# Match `@name` only at start of text or after chars that cannot belong to an address/path/handle.
_TAGGED_RE = re.compile(r"(?<![A-Za-z0-9_.@/\\-])@([A-Za-z][A-Za-z0-9_-]{0,63})")
_MAX_TAGS = 64


def extract_mention_candidates(body: str, max_tags: int = _MAX_TAGS) -> list[list[str]]:
    """Extract ordered candidate participant handles from message text.

    Ignores code blocks, inline backticks, emails (e.g. ``ana@example.com``),
    and scoped package names (e.g. ``pkg/@scope``). For trailing punctuation
    like ``@ana-`` or ``@ana_``, returns ``[name, trimmed]`` in order.
    """
    if not body:
        return []

    sanitized = _CODE_SPAN_RE.sub(" ", body)
    seen: set[str] = set()
    found: list[list[str]] = []
    for match in _TAGGED_RE.finditer(sanitized):
        name = match.group(1)
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        trimmed = name.rstrip("-_")
        candidates = [name] if trimmed == name or not trimmed else [name, trimmed]
        found.append(candidates)
        if len(found) >= max_tags:
            break
    return found


async def resolve_mentions(db: Any, body: str) -> list[dict[str, str]]:
    """Resolve @mentions in ``body`` against active (non-removed) participants."""
    candidates = extract_mention_candidates(body)
    if not candidates:
        return []

    flat_lower = list(dict.fromkeys(c.lower() for group in candidates for c in group))
    placeholders = ", ".join("?" for _ in flat_lower)
    rows = await db.execute_fetchall(
        f"""
        SELECT id, name
        FROM participants
        WHERE removed_at IS NULL
          AND lower(name) IN ({placeholders})
        """,
        tuple(flat_lower),
    )
    by_lower = {row["name"].lower(): {"id": row["id"], "name": row["name"]} for row in rows}

    resolved: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for group in candidates:
        for candidate in group:
            hit = by_lower.get(candidate.lower())
            if hit is not None:
                if hit["id"] not in seen_ids:
                    seen_ids.add(hit["id"])
                    resolved.append(hit)
                break
    return resolved
