"""Shared utilities for reading repository code and environment helpers.

Consolidates ``_read_repo_code``, ``_truncate_for_context``, and ``_int_env``
that were previously duplicated across backend_agent, orchestrator,
documentation_agent, and frontend_team modules.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

# Directories excluded from repo scans (build artifacts, VCS, dependency caches)
REPO_EXCLUDE_DIRS: frozenset[str] = frozenset({
    ".git", "node_modules", "dist", ".angular",
})

# Default extensions per agent domain
BACKEND_EXTENSIONS: List[str] = [".py", ".java"]
FRONTEND_EXTENSIONS: List[str] = [".ts", ".tsx", ".html", ".scss"]
FULL_STACK_EXTENSIONS: List[str] = [
    ".py", ".ts", ".tsx", ".java", ".yml", ".yaml",
]
DOCUMENTATION_EXTENSIONS: List[str] = [
    ".py", ".ts", ".tsx", ".java", ".yml", ".yaml", ".html", ".scss",
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


def truncate_for_context(text: str, max_chars: int) -> str:
    """Truncate *text* with a notice appended when trimmed."""
    if not text or len(text) <= max_chars:
        return text or ""
    return text[:max_chars] + f"\n\n... [truncated, {len(text) - max_chars} more chars]"


def int_env(name: str, default: int, min_val: int = 1) -> int:
    """Read an integer from environment variable *name*, clamped to *min_val*."""
    try:
        return max(min_val, int(os.environ.get(name) or str(default)))
    except ValueError:
        return default
