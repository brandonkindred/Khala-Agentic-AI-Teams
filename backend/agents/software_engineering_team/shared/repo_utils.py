"""Shared utilities for reading repository code and environment helpers.

Consolidates ``_read_repo_code``, ``_truncate_for_context``, and ``_int_env``
that were previously duplicated across backend_agent, orchestrator,
documentation_agent, and frontend_team modules.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Iterable, List, Optional

# Directories excluded from repo scans (build artifacts, VCS, dependency caches)
REPO_EXCLUDE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        "dist",
        ".angular",
    }
)

# Extra interpreter/venv caches excluded by repo-inspection views on top of
# REPO_EXCLUDE_DIRS, plus the combined set those views use. Single-sourced here so
# the active repo-inspection tools (agent_repo_tools) and the passive context
# scanner (coding_team._read_repo_context) cannot drift.
REPO_INSPECT_EXTRA_EXCLUDE_DIRS: frozenset[str] = frozenset({"__pycache__", "venv", ".venv"})
REPO_INSPECT_EXCLUDE_DIRS: frozenset[str] = REPO_EXCLUDE_DIRS | REPO_INSPECT_EXTRA_EXCLUDE_DIRS

# Default extensions per agent domain
BACKEND_EXTENSIONS: List[str] = [".py", ".java"]
FRONTEND_EXTENSIONS: List[str] = [".ts", ".tsx", ".html", ".scss"]
FULL_STACK_EXTENSIONS: List[str] = [
    ".py",
    ".ts",
    ".tsx",
    ".java",
    ".yml",
    ".yaml",
]
DOCUMENTATION_EXTENSIONS: List[str] = [
    ".py",
    ".ts",
    ".tsx",
    ".java",
    ".yml",
    ".yaml",
    ".html",
    ".scss",
]


def read_repo_code(
    repo_path: Path,
    extensions: Optional[List[str]] = None,
    *,
    exclude_dirs: Optional[frozenset[str]] = None,
) -> str:
    """Read source files from *repo_path*, concatenated with path headers.

    Parameters
    ----------
    repo_path:
        Root of the repository to scan.
    extensions:
        File suffixes to include (e.g. ``[".py", ".java"]``).
        Defaults to :data:`FULL_STACK_EXTENSIONS`.
    exclude_dirs:
        Directory names to skip.  Defaults to :data:`REPO_EXCLUDE_DIRS`.
        ``.git`` is *always* excluded regardless of this parameter.
    """
    if extensions is None:
        extensions = FULL_STACK_EXTENSIONS
    if exclude_dirs is None:
        exclude_dirs = REPO_EXCLUDE_DIRS

    always_exclude = exclude_dirs | {".git"}

    parts: List[str] = []
    for f in repo_path.rglob("*"):
        if always_exclude & set(f.parts):
            continue
        if f.is_file() and f.suffix in extensions:
            try:
                parts.append(
                    f"### {f.relative_to(repo_path)} ###\n"
                    f"{f.read_text(encoding='utf-8', errors='replace')}"
                )
            except (OSError, UnicodeDecodeError):
                pass
    return "\n\n".join(parts) if parts else "# No code files found"


def read_files_as_dict(
    repo_path: Path,
    paths: Iterable[str],
    extensions: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Read *paths* under *repo_path* into a ``{path: content}`` mapping.

    Parameters
    ----------
    repo_path:
        Root the paths are resolved against.
    paths:
        Repo-relative paths to read (e.g. the output of ``list_changed_files``).
    extensions:
        When given, only paths whose suffix is in this list are included.
        ``None`` means no extension filter (so files without a code suffix,
        such as ``Dockerfile`` or ``requirements.txt``, pass through).

    Preconditions:
        - *paths* are repo-relative; the caller has already scoped them.
    Postconditions:
        - Returns a mapping in the iteration order of *paths*, skipping any path
          that is filtered out by *extensions*, is missing, or cannot be read as
          UTF-8 text (these are dropped silently so review still runs on the
          readable remainder).
    """
    result: Dict[str, str] = {}
    for rel_path in paths:
        candidate = Path(rel_path)
        if extensions is not None and candidate.suffix not in extensions:
            continue
        full_path = repo_path / candidate
        try:
            result[rel_path] = full_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return result


def truncate_for_context(
    text: str, max_chars: int, llm: object = None, content_description: str = "content"
) -> str:
    """Compact *text* with LLM when over budget; pass full text when no LLM available."""
    if not text or len(text) <= max_chars:
        return text or ""
    if llm is not None:
        from llm_service import compact_text

        return compact_text(text, max_chars, llm, content_description)
    return text


def int_env(name: str, default: int, min_val: int = 1) -> int:
    """Read an integer from environment variable *name*, clamped to *min_val*."""
    try:
        return max(min_val, int(os.environ.get(name) or str(default)))
    except ValueError:
        return default
