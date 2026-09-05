"""Explicit scope model shared by the Bridge-side proof suites.

A bare text search over the whole repository proves nothing about what the
Bridge consumes: it hits prose that *forbids* a behaviour, captured third-party
evidence, and the proof harness's own vocabulary. Every claim in these suites is
therefore made against a named scope.

  RUNTIME  executable Python the Bridge actually runs:
             root-level *.py (main.py), bridge_protocol/, examples/, scripts/
  TESTS    tests/*.py
  PROSE    tracked text, excluding the frozen scopes below

Frozen scopes, excluded from PROSE by name:

  docs/audits/                      Captured evidence, deliberately not updated
                                    when ForgeLoop moves. It includes a verbatim
                                    copy of ForgeLoop's own protocol-info with
                                    all 275 public error codes, so a text search
                                    would "find" every canonical error name.
                                    This directory also holds the proof harness
                                    itself, which necessarily names the very
                                    surfaces it proves absent from runtime.

  *FORGELOOP_1_5_UPDATE_PLAN.md     Marked HISTORICAL / SUPERSEDED in its own
                                    header; retained for lineage, not guidance.

RUNTIME is an allowlist of real runtime paths rather than a blocklist, so
documentation and proof code can never silently enter it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

RUNTIME_TOP_LEVEL = ("bridge_protocol", "examples", "scripts")
FROZEN = ("docs/audits/", "FORGELOOPBRIDGE_FORGELOOP_1_5_UPDATE_PLAN.md")
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".ico", ".gif", ".woff", ".woff2", ".db", ".pdf"}


def bridge_repo_root(start: Path | None = None) -> Path:
    """Locate the Bridge checkout from this file, so the suites are relocatable."""
    current = (start or Path(__file__)).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "bridge_protocol").is_dir() and (candidate / ".git").exists():
            return candidate
    raise SystemExit(
        "scopes: could not locate the ForgeLoopBridge checkout from "
        f"{current} (expected an ancestor containing bridge_protocol/ and .git)"
    )


def tracked_files(root: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "-C", str(root), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.split()


def _read(root: Path, names: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in names:
        path = root / name
        if not path.is_file() or path.suffix.lower() in SKIP_SUFFIXES:
            continue
        try:
            out[name] = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
    return out


def is_runtime(name: str) -> bool:
    if not name.endswith(".py"):
        return False
    parts = name.split("/")
    return len(parts) == 1 or parts[0] in RUNTIME_TOP_LEVEL


def is_frozen(name: str) -> bool:
    return any(name.startswith(scope) or name == scope for scope in FROZEN)


def load_scopes(root: Path) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    names = tracked_files(root)
    runtime = _read(root, [n for n in names if is_runtime(n)])
    tests = _read(root, [n for n in names if n.startswith("tests/") and n.endswith(".py")])
    prose = _read(root, [n for n in names if not is_frozen(n)])
    return runtime, tests, prose


def absent(scope: dict[str, str], *markers: str) -> tuple[bool, str]:
    """True when no marker occurs anywhere in the scope, with the hits if any."""
    hits = [
        f"{name}:{marker}"
        for name, text in scope.items()
        for marker in markers
        if marker in text
    ]
    return (not hits), ("; ".join(hits[:3]) if hits else "no reference")
