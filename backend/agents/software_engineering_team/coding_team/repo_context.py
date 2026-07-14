"""Repo-structure briefing for coding-team implementation workers.

Extracted from ``coding_team/orchestrator.py`` (decompose the orchestrator god-file
into named collaborators) — pure structural move, no behavior change.

Distinct from ``software_engineering_team.shared.repo_context_cache.RepoContextCache``,
which serves the char-budgeted briefing consumed by the code-v2 development agents.
This module uses a file-count ceiling and full-file contents (never a char budget).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Repo-context file selection. The shared full-stack code extensions / exclude dirs live in
# shared_repo_context.repo_utils; this summariser additionally surfaces the doc and
# config formats below (so a docs/spec task is not blind to specs, plans, and READMEs). The
# directories it skips come from repo_utils.REPO_INSPECT_EXCLUDE_DIRS (imported in
# `_context_file_filters`), shared with the active inspection tools so the two views of the repo
# cannot drift.
_CONTEXT_EXTRA_EXTENSIONS: frozenset[str] = frozenset(
    {".js", ".html", ".json", ".md", ".txt", ".rst"}
)

# Full file-selection sets for repo-context scanning, built once from the shared repo_utils
# constants + the extras above and cached (the import lives below to keep the SE dependency
# function-level; the sets are static so there is no need to rebuild them on every call).
_CONTEXT_EXTENSIONS: Optional[frozenset[str]] = None
_CONTEXT_EXCLUDE_DIRS: Optional[frozenset[str]] = None


def _context_file_filters() -> tuple[frozenset[str], frozenset[str]]:
    """Return (extensions, exclude_dirs) for repo-context scanning, computed once and cached.

    Reuses the shared full-stack code extensions / exclude dirs (so adding a code file type in one
    place keeps every repo scanner consistent), unioned with this summariser's doc/config extras.
    """
    global _CONTEXT_EXTENSIONS, _CONTEXT_EXCLUDE_DIRS
    if _CONTEXT_EXTENSIONS is None or _CONTEXT_EXCLUDE_DIRS is None:
        from shared_repo_context.repo_utils import (
            FULL_STACK_EXTENSIONS,
            REPO_INSPECT_EXCLUDE_DIRS,
        )

        _CONTEXT_EXTENSIONS = frozenset(FULL_STACK_EXTENSIONS) | _CONTEXT_EXTRA_EXTENSIONS
        _CONTEXT_EXCLUDE_DIRS = REPO_INSPECT_EXCLUDE_DIRS
    return _CONTEXT_EXTENSIONS, _CONTEXT_EXCLUDE_DIRS


# Ceiling on how many eligible files the repo briefing covers (a cap on breadth,
# never a truncation of any single file's content — see ``_read_repo_context``).
_CONTEXT_FILE_CEILING = 80


def _enumerate_context_files(repo_path: Path) -> List[Path]:
    """Return the sorted, capped list of context-eligible files under ``repo_path``.

    Walks with ``os.walk`` and prunes excluded dirs in place so the traversal never
    descends into node_modules/.git/etc. The old ``sorted(repo_path.rglob("*"))``
    stat-ed the *entire* tree (tens of thousands of files for any frontend repo)
    and sorted it before slicing — and worse, those excluded entries consumed the
    file budget, starving real source files. Collecting eligible files first, then
    sorting and capping, both fixes the stat storm and guarantees the cap covers
    real files.

    Preconditions:
        - ``repo_path`` is an existing directory.
    Postconditions:
        - Returns at most ``_CONTEXT_FILE_CEILING`` files, sorted deterministically;
          every entry matches ``_context_file_filters`` and ``is_file()`` is True.
    """
    extensions, exclude_dirs = _context_file_filters()
    eligible: List[Path] = []
    try:
        for dirpath, dirnames, filenames in os.walk(repo_path):
            dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
            for name in filenames:
                f = Path(dirpath) / name
                # is_file() (not just suffix) guards against special files: a FIFO /
                # socket / device named e.g. ``pipe.py`` would otherwise pass the
                # suffix check and block read_text() forever (a hang the try/except
                # in the renderer cannot catch). is_file() is False for those and for
                # broken symlinks, matching the previous rglob path's filter.
                if f.suffix in extensions and f.is_file():
                    eligible.append(f)
    except Exception:
        # Best-effort repo scan: a walk error (e.g. a permission-denied directory)
        # must not abort context-building, but log it at debug so it is diagnosable
        # rather than silently swallowed.
        logger.debug("os.walk failed while building repo context", exc_info=True)
    return sorted(eligible)[:_CONTEXT_FILE_CEILING]


def _render_context_file(f: Path, repo_path: Path) -> Optional[str]:
    """Render one eligible file as its full-contents briefing part, or None on read failure.

    Preconditions:
        - ``f`` is a file under ``repo_path``.
    Postconditions:
        - Returns ``"--- {rel} ---\\n{content}\\n"`` with the file's COMPLETE contents (never a
          prefix); returns None when the file cannot be read (the caller skips it), matching the
          prior behavior where an unreadable file was silently dropped.
    """
    try:
        content = f.read_text(encoding="utf-8", errors="replace")
    except Exception:
        logger.debug("failed to read context file %s", f, exc_info=True)
        return None
    rel = str(f.relative_to(repo_path))
    return f"--- {rel} ---\n{content}\n"


def _join_context_parts(parts: List[str]) -> str:
    """Join rendered briefing parts, or return the empty-repo sentinel.

    The single source of the "No files found" sentinel and the part separator, so the pure
    ``_read_repo_context`` and the incremental ``_RepoContextCache`` cannot drift apart (the cache's
    byte-identical invariant depends on them producing the same joined form).

    Postconditions:
        - Returns ``"No files found"`` for an empty list, else the parts joined by a blank line.
    """
    return "\n".join(parts) if parts else "No files found"


def _read_repo_context(repo_path: Path) -> str:
    """Read the repo structure/code briefing for implementation-worker context.

    Every file the briefing includes is rendered with its FULL contents — the
    engineer reasons over this to implement a task, and clipping a file would
    hide code from it (mirroring the team's "inputs are never truncated"
    contract for the plan text, task description, and review diff). The file
    ceiling on the eligible-file list is a deliberate cap on how many files the
    briefing covers, not truncation of any file's content.

    Preconditions:
        - ``repo_path`` is an existing directory.
    Postconditions:
        - Each context-eligible file (matching ``_context_file_filters`` and
          within the file-count ceiling) appears with its complete contents,
          never a prefix; no eligible file is dropped to fit a size budget.
        - Returns ``"No files found"`` when no eligible file is present.
    """
    parts: List[str] = []
    for f in _enumerate_context_files(repo_path):
        part = _render_context_file(f, repo_path)
        if part is not None:
            parts.append(part)
    return _join_context_parts(parts)


class _RepoContextCache:
    """Incremental cache over ``_read_repo_context`` that re-reads only changed files.

    The repo briefing is rebuilt whenever merged work lands (see ``run()``), but a merge typically
    touches only a handful of the (up to ceiling) files. Re-reading every file each time is the cost
    this cache removes. It keeps the rendered briefing part per file keyed by ``(st_mtime_ns,
    st_size)``: on ``read`` it re-enumerates eligible files (a cheap ``os.walk`` + ``stat``) and
    reuses a cached part whenever the key is unchanged, re-rendering (reading the file) only when the
    key differs or the file is new. Entries for files no longer eligible are dropped so the cache
    cannot grow without bound or resurrect stale content.

    Invariants:
        - The string returned by ``read`` is byte-identical to ``_read_repo_context(repo_path)`` for
          the same on-disk state — the cache changes *when* files are read, never *what* is rendered.
        - ``st_mtime_ns`` (nanosecond resolution) plus size is the freshness key; a content change
          that leaves both identical would not be detected, but a merge always rewrites the file
          (advancing mtime), so this cannot occur in the swarm's usage.

    Preconditions (``read``):
        - ``repo_path`` is an existing directory.
    Postconditions (``read``):
        - Returns the same value ``_read_repo_context(repo_path)`` would; the internal cache holds an
          entry for exactly the currently-eligible, successfully-rendered files.
    """

    def __init__(self) -> None:
        # path -> (mtime_ns, size, rendered_part)
        self._entries: Dict[Path, tuple[int, int, str]] = {}

    def read(self, repo_path: Path) -> str:
        files = _enumerate_context_files(repo_path)
        fresh: Dict[Path, tuple[int, int, str]] = {}
        parts: List[str] = []
        for f in files:
            try:
                st = f.stat()
                key = (st.st_mtime_ns, st.st_size)
            except Exception:
                # A file that vanished or cannot be stat-ed between walk and stat is skipped, exactly
                # as _render_context_file would drop an unreadable file; it also leaves the cache.
                logger.debug("failed to stat context file %s", f, exc_info=True)
                continue
            cached = self._entries.get(f)
            if cached is not None and cached[:2] == key:
                part = cached[2]
            else:
                rendered = _render_context_file(f, repo_path)
                if rendered is None:
                    # Unreadable: drop from cache and skip, mirroring _read_repo_context.
                    continue
                part = rendered
            fresh[f] = (*key, part)
            parts.append(part)
        # Replace wholesale so entries for now-ineligible/removed files are evicted.
        self._entries = fresh
        return _join_context_parts(parts)
