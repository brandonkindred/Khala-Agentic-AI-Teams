"""
Load and update style guide and brand spec file contents for draft and copy editor agents.

Callers load files before instantiating agents; on failure log an error and return empty string.
``append_guidelines`` allows the interactive draft review loop to persist
writing guideline updates derived from user feedback.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Union

logger = logging.getLogger(__name__)


def load_style_file(path: Union[str, Path], label: str = "file") -> str:
    """
    Load a style/guideline file as UTF-8 text and render any Jinja2 placeholders
    against the runtime author profile.

    Style guides under ``backend/agents/blogging/docs/`` are Jinja2 templates that
    reference the user's identity via ``{{ author.* }}`` (see
    ``backend.agents.shared.author_profile``). This loader renders them on read so
    callers always see fully resolved markdown.

    On failure (missing file, read error, render error), log and return "".

    Args:
        path: Path to the file.
        label: Human-readable label for log messages.

    Returns:
        Rendered file content stripped of surrounding whitespace, or "" on any error.
    """
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as e:
        logger.error("Could not load %s from %s: %s", label, p, e)
        return ""

    try:
        from author_profile import load_author_profile, render_template

        return render_template(raw, load_author_profile()).strip()
    except Exception as e:  # noqa: BLE001 — render failure shouldn't crash agents
        logger.error("Could not render %s from %s: %s", label, p, e)
        return raw.strip()


def save_style_file(path: Union[str, Path], content: str, label: str = "file") -> bool:
    """
    Write content to a style file (UTF-8). Creates parent directories if needed.

    Returns True on success, False on failure.
    """
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        logger.info("Saved %s to %s", label, p)
        return True
    except OSError as e:
        logger.error("Could not save %s to %s: %s", label, p, e)
        return False


def append_guidelines(
    path: Union[str, Path],
    updates: List[dict],
    label: str = "writing style guide",
) -> bool:
    """
    Append writing guideline updates to the end of the style guide file.

    Each update dict should have ``category``, ``description``, and ``guideline_text``.
    Updates are appended under a dated section header so the history is traceable.

    Returns True on success, False on failure.
    """
    if not updates:
        return True
    p = Path(path)
    try:
        existing = p.read_text(encoding="utf-8") if p.exists() else ""
    except OSError:
        existing = ""

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "",
        "",
        f"## Editor-Derived Guidelines ({timestamp})",
        "",
        "The following rules were extracted from editor feedback during interactive draft review.",
        "",
    ]
    for update in updates:
        category = update.get("category", "other")
        description = update.get("description", "")
        rule = update.get("guideline_text", "")
        lines.append(f"- **[{category}]** {description}")
        lines.append(f"  - Rule: {rule}")
        lines.append("")

    new_content = existing.rstrip() + "\n".join(lines)
    return save_style_file(p, new_content, label)
