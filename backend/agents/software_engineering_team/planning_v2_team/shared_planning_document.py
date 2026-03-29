"""
Blackboard-pattern shared planning document.

All planning-v2 tool agents read from and write to a single shared markdown file
(plan/planning_team/planning_document.md). Each agent owns a named section and can
read all other sections to cross-reference other agents' work before producing its own.

Thread safety: a module-level threading.Lock serialises all writes (agents run in
a ThreadPoolExecutor within the same process).
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional

from .models import PLAN_PLANNING_TEAM_DIR, ToolAgentKind

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SHARED_DOC_FILENAME = "planning_document.md"

AGENT_SECTION_MAP: Dict[ToolAgentKind, str] = {
    ToolAgentKind.SYSTEM_DESIGN: "System Design",
    ToolAgentKind.ARCHITECTURE: "Architecture",
    ToolAgentKind.USER_STORY: "User Stories",
    ToolAgentKind.DEVOPS: "DevOps",
    ToolAgentKind.UI_DESIGN: "UI Design",
    ToolAgentKind.UX_DESIGN: "UX Design",
    ToolAgentKind.TASK_CLASSIFICATION: "Task Classification",
    ToolAgentKind.TASK_DEPENDENCY: "Task Dependencies",
}

_SECTION_RE = re.compile(r"<!-- SECTION:(.+?) -->")

_write_lock = threading.Lock()

DOC_HEADER = "# Planning Document\n\n"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def get_shared_doc_path(repo_path: Path) -> Path:
    """Return the absolute path to the shared planning document."""
    return repo_path / PLAN_PLANNING_TEAM_DIR / SHARED_DOC_FILENAME


def shared_doc_asset_path() -> str:
    """Return the relative path (from repo root) used as a key in current_files dicts."""
    return f"{PLAN_PLANNING_TEAM_DIR}/{SHARED_DOC_FILENAME}"


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


def read_full_document(repo_path: Path) -> str:
    """Read the entire shared planning document. Returns empty string if not exists."""
    doc_path = get_shared_doc_path(repo_path)
    if not doc_path.exists():
        return ""
    try:
        return doc_path.read_text(encoding="utf-8")
    except (IOError, OSError, UnicodeDecodeError) as e:
        logger.warning("Failed to read shared planning document: %s", e)
        return ""


def list_sections(repo_path: Path) -> List[str]:
    """Return the names of all sections present in the shared document."""
    content = read_full_document(repo_path)
    return _SECTION_RE.findall(content)


def read_section(repo_path: Path, section_name: str) -> Optional[str]:
    """Extract a single section's content from the shared document.

    Returns None if the section does not exist.
    """
    content = read_full_document(repo_path)
    if not content:
        return None
    return _extract_section(content, section_name)


def read_other_sections(repo_path: Path, exclude_section: str) -> str:
    """Blackboard read: return all sections *except* ``exclude_section``.

    Useful for injecting cross-agent context into prompts.
    Returns a concatenated string of all other sections (including their headings).
    """
    content = read_full_document(repo_path)
    if not content:
        return ""

    sections = _SECTION_RE.findall(content)
    parts: List[str] = []
    for name in sections:
        if name == exclude_section:
            continue
        section_text = _extract_section(content, name)
        if section_text:
            parts.append(f"## {name}\n{section_text}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------


def write_section(repo_path: Path, section_name: str, content: str) -> None:
    """Insert or replace a section in the shared planning document (thread-safe).

    The section is wrapped with HTML comment markers so it can be reliably located:

        <!-- SECTION:Architecture -->
        ## Architecture
        (content)
        <!-- END_SECTION:Architecture -->

    If the section already exists it is replaced in-place; otherwise it is appended.
    """
    with _write_lock:
        _write_section_locked(repo_path, section_name, content)


def _write_section_locked(repo_path: Path, section_name: str, content: str) -> None:
    """Internal write — must be called while holding ``_write_lock``."""
    doc_path = get_shared_doc_path(repo_path)
    doc_path.parent.mkdir(parents=True, exist_ok=True)

    existing = ""
    if doc_path.exists():
        try:
            existing = doc_path.read_text(encoding="utf-8")
        except (IOError, OSError, UnicodeDecodeError):
            existing = ""

    section_block = _build_section_block(section_name, content)

    if not existing.strip():
        # Brand-new document
        new_content = DOC_HEADER + section_block + "\n"
    elif _has_section(existing, section_name):
        # Replace existing section in-place
        new_content = _replace_section(existing, section_name, section_block)
    else:
        # Append new section at end
        new_content = existing.rstrip("\n") + "\n\n" + section_block + "\n"

    doc_path.write_text(new_content, encoding="utf-8")
    logger.info(
        "Shared planning doc: wrote section '%s' (%d chars) to %s",
        section_name,
        len(content),
        doc_path.name,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_section_block(section_name: str, content: str) -> str:
    """Build a complete section block with markers."""
    return (
        f"<!-- SECTION:{section_name} -->\n"
        f"## {section_name}\n"
        f"{content.strip()}\n"
        f"<!-- END_SECTION:{section_name} -->"
    )


def _has_section(doc: str, section_name: str) -> bool:
    return f"<!-- SECTION:{section_name} -->" in doc


def _extract_section(doc: str, section_name: str) -> Optional[str]:
    """Extract content between section markers (excluding the ``## heading`` line).

    The ``## heading`` line is always generated by ``_build_section_block`` and is
    stripped so that callers receive only the body content.
    """
    start_marker = f"<!-- SECTION:{section_name} -->"
    end_marker = f"<!-- END_SECTION:{section_name} -->"
    start_idx = doc.find(start_marker)
    if start_idx == -1:
        return None
    end_idx = doc.find(end_marker, start_idx)
    if end_idx == -1:
        logger.warning("Section '%s' has start marker but no end marker — skipping", section_name)
        return None

    inner = doc[start_idx + len(start_marker) : end_idx].strip()
    # Strip the ## heading line if present
    lines = inner.split("\n")
    if lines and lines[0].startswith("## "):
        lines = lines[1:]
    return "\n".join(lines).strip() or None


def _replace_section(doc: str, section_name: str, new_block: str) -> str:
    """Replace an existing section block with a new one."""
    start_marker = f"<!-- SECTION:{section_name} -->"
    end_marker = f"<!-- END_SECTION:{section_name} -->"
    start_idx = doc.find(start_marker)
    end_idx = doc.find(end_marker, start_idx)
    if start_idx == -1 or end_idx == -1:
        return doc

    before = doc[:start_idx].rstrip("\n")
    after = doc[end_idx + len(end_marker) :].lstrip("\n")

    parts = [before] if before else []
    parts.append(new_block)
    if after.strip():
        parts.append(after)

    return "\n\n".join(parts) + "\n"
