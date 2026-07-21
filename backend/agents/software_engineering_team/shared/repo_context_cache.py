"""Incremental repo-context cache for the code-v2 development agents.

The backend/frontend code-v2 orchestrators read the repository briefing once per
``run_workflow`` (once per task). Across the N tasks of a single coding-team job
that is N full re-walks + N re-reads of every eligible file, even though a merge
typically only touches a handful of them. This module lifts the incremental
``(st_mtime_ns, st_size) -> rendered_part`` cache (originally
``coding_team.orchestrator._RepoContextCache``) into a shared, parameterised form
so the v2 development agents can reuse it.

Preconditions:
    - A :class:`RepoContextCache` is constructed with the same
      ``extensions`` / ``exclude_dirs`` / ``max_chars`` / ``empty`` the team's
      ``read_repo_code_budgeted`` call uses, so the cache's output matches the
      fresh-walk output byte-for-byte.

Invariants:
    - For a given on-disk state, :meth:`RepoContextCache.read` returns a string
      byte-identical to
      ``read_repo_code_budgeted(repo_path, extensions=..., exclude_dirs=...,
      max_chars=..., empty=...)``. The cache changes *when* files are re-read,
      never *what* is rendered.
    - ``(st_mtime_ns, st_size)`` is the freshness key; a content change that left
      both identical would not be detected, but a merge/commit always rewrites
      the file (advancing mtime), so this cannot occur in the swarm's usage.
    - The internal ``_entries`` holds an entry for exactly the currently-eligible,
      successfully-rendered files that fall *within* the char budget on this call
      (removed files and files pushed beyond the budget are evicted by the
      wholesale replacement on each ``read``), so the cache cannot grow without
      bound or resurrect stale content — and never stores the hundreds of MB of
      never-emitted tail files a large repo would otherwise render on the first
      task.

Postconditions (``read``):
    - Returns the same value the matching ``read_repo_code_budgeted`` call would,
      applying the same whole-file char budget during rendering (the next chunk
      that would exceed ``max_chars`` stops the briefing — never a partial file).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

logger = logging.getLogger(__name__)

# A cached entry: (mtime_ns, size, rendered_briefing_part).
_Entry = Tuple[int, int, str]

__all__ = ["RepoContextCache"]


class RepoContextCache:
    """Incremental cache over the budgeted repo briefing that re-reads only changed files.

    On each :meth:`read` it re-enumerates eligible files (a cheap ``os.walk`` +
    ``stat``), renders them in sorted order reusing a cached part whenever
    ``(st_mtime_ns, st_size)`` is unchanged (re-reading only when the key differs
    or the file is new), and applies the same whole-file char budget as
    :func:`shared.repo_context.read_repo_code_budgeted` *during* rendering — the
    first chunk that would exceed ``max_chars`` stops the briefing, so tail
    files beyond the budget are never rendered or cached (a large repo never
    reads+stores hundreds of MB of never-emitted tail files on the first task).
    Entries for files no longer eligible, or pushed beyond the budget, are
    dropped by the wholesale ``_entries`` replacement.

    Preconditions (``read``):
        - ``repo_path`` is an existing directory.
    Postconditions (``read``):
        - Returns the same value the matching ``read_repo_code_budgeted`` call
          would for the current on-disk state; ``_entries`` then holds an entry
          for exactly the currently-eligible, successfully-rendered files that
          fall within the char budget (tail files beyond the budget are not
          cached, matching the fresh walk which never reads past the cutoff).
    Invariants:
        - See module docstring: output is byte-identical to the fresh walk; the
          freshness key is ``(st_mtime_ns, st_size)``; ``_entries`` is bounded to
          the currently-eligible set.
    """

    def __init__(
        self,
        *,
        extensions: Iterable[str],
        exclude_dirs: Iterable[str],
        max_chars: int,
        empty: str = "# No code files found",
    ) -> None:
        assert max_chars > 0, "max_chars must be positive"
        self._ext_set = frozenset(extensions)
        self._excl_set = frozenset(exclude_dirs)
        self._max_chars = max_chars
        self._empty = empty
        self._entries: Dict[Path, _Entry] = {}

    def read(self, repo_path: Path) -> str:
        """Return the budgeted repo briefing, re-reading only changed eligible files.

        Preconditions: ``repo_path`` is an existing directory.
        Postconditions: returns the byte-identical-to-fresh-walk briefing; the
          internal cache is wholesale-replaced to evict now-ineligible files.
        """
        eligible = self._enumerate_eligible(repo_path)
        fresh: Dict[Path, _Entry] = {}
        parts: list[str] = []
        total = 0
        for f in eligible:
            try:
                st = f.stat()
                key = (st.st_mtime_ns, st.st_size)
            except OSError:
                # A file that vanished or cannot be stat-ed between walk and stat
                # is skipped, exactly as the fresh walk drops an unreadable file;
                # it also leaves the cache.
                continue
            cached = self._entries.get(f)
            if cached is not None and cached[:2] == key:
                part = cached[2]
            else:
                rendered = self._render(f, repo_path)
                if rendered is None:
                    # Unreadable: drop from cache and skip, mirroring the fresh walk.
                    continue
                part = rendered
            # Whole-file char budget applied *during* rendering — same semantics as
            # read_repo_code_budgeted: the next chunk that would exceed max_chars
            # stops the briefing (never a partial file). We break before caching,
            # so tail files beyond the budget are never stored: a large repo cannot
            # read+cache hundreds of MB of never-emitted tail files on the first
            # task. (An uncached over-budget file is read this call to learn its
            # length, matching the fresh walk which reads it before its budget
            # check; a cached one reuses its cached length. Either is then evicted,
            # never emitted or retained.)
            if total + len(part) > self._max_chars:
                break
            fresh[f] = (*key, part)
            parts.append(part)
            total += len(part)
        # Replace wholesale so entries for now-ineligible/removed/beyond-budget
        # files are evicted; the cache is bounded to the emitted (within-budget) set.
        self._entries = fresh
        return "\n".join(parts) if parts else self._empty

    def _enumerate_eligible(self, repo_path: Path) -> list[Path]:
        """Return the sorted list of context-eligible files under ``repo_path``.

        Mirrors :func:`read_repo_code_budgeted`'s streamed ``os.walk``: prunes
        excluded dirs in place so the traversal never descends into
        node_modules/.git/etc, and selects files whose suffix is in the extension
        set and that are regular files (``is_file()`` guards against special
        files that would block ``read_text``). Sorted for deterministic, fresh-
        walk-matching output. Best-effort: a mid-walk ``OSError`` degrades to the
        entries found so far rather than aborting the caller.

        Preconditions: ``repo_path`` is an existing directory.
        Postconditions: returns the sorted eligible file list (no cap; the char
          budget applied during rendering in :meth:`read` bounds what is emitted
          and cached, not what is enumerated).
        """
        eligible: list[Path] = []
        try:
            for dirpath, dirnames, filenames in os.walk(repo_path):
                dirnames[:] = [d for d in dirnames if d not in self._excl_set]
                for name in filenames:
                    f = Path(dirpath) / name
                    if f.suffix in self._ext_set and f.is_file():
                        eligible.append(f)
        except OSError as exc:
            logger.warning(
                "Repo context cache walk under %s aborted early (%s); using the %d entries found so far",
                repo_path,
                exc,
                len(eligible),
                exc_info=True,
            )
        return sorted(eligible)

    @staticmethod
    def _render(f: Path, repo_path: Path) -> Optional[str]:
        """Render one eligible file as its briefing part, or None on read failure.

        Preconditions: ``f`` is a file under ``repo_path``.
        Postconditions: returns ``"--- {rel} ---\\n{content}\\n"`` with the file's
          COMPLETE contents (never a prefix); returns None when the file cannot be
          read (the caller skips it), matching the fresh walk's skip-on-error.
        """
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        rel = f.relative_to(repo_path)
        return f"--- {rel} ---\n{content}\n"
