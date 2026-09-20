#!/usr/bin/env python3
"""Master-Agent Bridge — Self-Improvement Subsystem.

Automatically generates standardized `<task_name>-<date>-<time>.md` logs
for every system execution, capturing actionable fixes, architectural learnings,
and performance insights identified by the Master or Agent in English.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_IMPROVEMENTS_DIR = "improvements"


def get_improvements_dir(custom_dir: Path | str | None = None) -> Path:
    """Resolve the directory where self-improvement markdown files are stored."""
    if custom_dir:
        path = Path(custom_dir)
    else:
        path = Path(os.getenv("IMPROVEMENTS_DIR", DEFAULT_IMPROVEMENTS_DIR))
    path.mkdir(parents=True, exist_ok=True)
    return path


def sanitize_task_name(task_name: str | None) -> str:
    """Normalize a task or order name into a safe, bounded filesystem slug."""
    if not task_name:
        return "general-task"

    # Replace non-alphanumeric chars with hyphen
    slug = re.sub(r"[^a-zA-Z0-9_\-]+", "-", str(task_name).strip())
    # Collapse multiple hyphens/underscores
    slug = re.sub(r"-{2,}", "-", slug).strip("-_").lower()
    # Strictly retain ASCII alphanumeric characters, hyphens, and underscores
    clean = "".join(c for c in slug if c.isascii() and (c.isalnum() or c in ("-", "_")))
    if not clean:
        return "general-task"
    return clean[:50]


def generate_improvement_filename(task_name: str | None, timestamp: datetime | None = None) -> str:
    """Generate a standard `<task_name>-<date>-<time>.md` filename."""
    slug = sanitize_task_name(task_name)
    now = timestamp or datetime.now(UTC)
    # Format: <task_name>-YYYY-MM-DD-HH-MM-SS.md
    date_time_str = now.strftime("%Y-%m-%d-%H-%M-%S")
    safe_name = f"{slug}-{date_time_str}.md"
    return os.path.basename(safe_name)



def _format_list_or_str(items: list[str] | str | None, default_msg: str) -> str:
    """Helper to format bullet lists or string sections."""
    if not items:
        return f"- {default_msg}"
    if isinstance(items, str):
        cleaned = items.strip()
        if not cleaned:
            return f"- {default_msg}"
        # If already formatted as bullets, return directly
        if "\n" in cleaned or cleaned.startswith("-") or cleaned.startswith("*"):
            return cleaned
        return f"- {cleaned}"

    filtered = [i.strip() for i in items if i and i.strip()]
    if not filtered:
        return f"- {default_msg}"
    return "\n".join(f"- {i.lstrip('-* ')}" for i in filtered)


def extract_improvement_sections(text: str) -> dict[str, list[str]]:
    """Parse common improvement, issue, and action item sections from Markdown text."""
    results: dict[str, list[str]] = {
        "improvements": [],
        "issues": [],
        "action_items": [],
    }
    if not text:
        return results

    current_section: str | None = None
    lines = text.splitlines()

    for line in lines:
        stripped = line.strip()
        upper = stripped.upper()

        # Check section headers
        if any(keyword in upper for keyword in ("IMPROVEMENT", "SUGGESTION", "WHAT TO IMPROVE")):
            current_section = "improvements"
            continue
        elif any(keyword in upper for keyword in ("ISSUE", "BLOCKER", "FRICTION", "WHAT WENT WRONG")):
            current_section = "issues"
            continue
        elif any(keyword in upper for keyword in ("ACTION ITEM", "NEXT STEP", "TODO")):
            current_section = "action_items"
            continue
        elif stripped.startswith("#") or (upper.endswith(":") and not stripped.startswith("-")):
            # Some other header
            current_section = None
            continue

        if current_section and (stripped.startswith("-") or stripped.startswith("*") or stripped.startswith("1.")):
            item = re.sub(r"^[-*0-9.]+\s*", "", stripped).strip()
            if item:
                results[current_section].append(item)

    return results


def create_improvement_record(
    task_name: str | None,
    *,
    role: str = "agent",
    status: str = "COMPLETED",
    summary: str = "",
    improvements: list[str] | str | None = None,
    issues: list[str] | str | None = None,
    action_items: list[str] | str | None = None,
    raw_content: str | None = None,
    target_dir: Path | str | None = None,
    timestamp: datetime | None = None,
) -> Path:
    """Create and write a standard `<task_name>-<date>-<time>.md` self-improvement record.

    Returns the Path to the created file.
    """
    now = timestamp or datetime.now(UTC)
    directory = get_improvements_dir(target_dir).resolve()
    filename = generate_improvement_filename(task_name, now)
    safe_basename = os.path.basename(filename)
    filepath = (directory / safe_basename).resolve()

    real_dir = os.path.realpath(directory)
    real_filepath = os.path.realpath(filepath)
    if not real_filepath.startswith(real_dir + os.path.sep) and not filepath.is_relative_to(directory):
        raise ValueError("Invalid target path outside improvements directory")


    # Extract sections from raw_content if explicit lists were omitted
    if raw_content and (not improvements or not issues or not action_items):
        extracted = extract_improvement_sections(raw_content)
        if not improvements and extracted["improvements"]:
            improvements = extracted["improvements"]
        if not issues and extracted["issues"]:
            issues = extracted["issues"]
        if not action_items and extracted["action_items"]:
            action_items = extracted["action_items"]

    display_role = role.capitalize()
    effective_task = task_name.strip() if task_name else "General Execution"
    effective_summary = summary.strip() if summary else (
        f"Execution of task '{effective_task}' by {display_role} finished with status {status}."
    )

    improvements_text = _format_list_or_str(
        improvements, "No immediate system improvements requested for this execution."
    )
    issues_text = _format_list_or_str(
        issues, "No blockers, bottlenecks, or friction points observed."
    )
    action_items_text = _format_list_or_str(
        action_items, "Continue adhering to established standards and automated test verifications."
    )

    content = f"""# Self-Improvement Record: {effective_task}

- **Date & Time:** {now.isoformat()}
- **Source Role:** {display_role}
- **Execution Status:** {status}
- **Task / Order ID:** {effective_task}

---

## 1. Executive Summary
{effective_summary}

## 2. Friction Points & Identified Issues
{issues_text}

## 3. Recommended System Improvements
{improvements_text}

## 4. Concrete Action Items & Next Steps
{action_items_text}

---
*Generated automatically by Master-Agent Bridge Self-Improvement Subsystem.*
"""

    filepath.write_text(content, encoding="utf-8")
    return filepath


def list_improvement_records(
    target_dir: Path | str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List recorded improvement markdown files sorted by newest first."""
    directory = get_improvements_dir(target_dir)
    records: list[dict[str, Any]] = []

    files = [
        f for f in directory.iterdir()
        if f.is_file() and f.suffix == ".md" and f.name.lower() != "readme.md"
    ]

    # Sort descending by modification time
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

    for f in files[:limit]:
        stat = f.stat()
        # Read snippet (first 6 lines)
        try:
            head_lines = f.read_text(encoding="utf-8", errors="replace").splitlines()[:8]
            snippet = "\n".join(head_lines)
        except Exception:
            snippet = ""

        records.append({
            "filename": f.name,
            "size_bytes": stat.st_size,
            "created_at": stat.st_mtime,
            "snippet": snippet,
        })

    return records


def read_improvement_record(
    filename: str,
    target_dir: Path | str | None = None,
) -> str:
    """Read a specific improvement record markdown, preventing directory traversal."""
    # Strict format check: must be an exact safe filename without any directory separators
    if not re.match(r"^[a-zA-Z0-9_\-]+\.md$", filename):
        raise FileNotFoundError(f"Improvement record not found: {filename}")

    safe_name = os.path.basename(filename)
    if safe_name != filename:
        raise FileNotFoundError(f"Improvement record not found: {filename}")

    directory = get_improvements_dir(target_dir).resolve()
    real_dir = os.path.realpath(directory)

    # Search directory entries so we only read pre-existing files without constructing arbitrary paths
    matched_entry: Path | None = None
    for entry in directory.iterdir():
        if entry.is_file() and entry.name == safe_name:
            matched_entry = entry
            break

    if matched_entry is None:
        raise FileNotFoundError(f"Improvement record not found: {filename}")

    real_entry = os.path.realpath(matched_entry)
    if not real_entry.startswith(real_dir + os.path.sep) and real_entry != real_dir:
        raise FileNotFoundError(f"Improvement record not found: {filename}")

    return matched_entry.read_text(encoding="utf-8")

