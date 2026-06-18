"""Shared filesystem conventions for a coding-team per-issue clone workspace.

Both services that touch a per-issue checkout import from here so the lock-file
name lives in exactly one place:

- the *producer* — unified_api's ``_ensure_repo_clone`` — creates and ``flock``s
  the lock while it clones/fetches;
- the *consumer* — coding_team's ``_cleanup_issue_checkout`` — removes the lock
  after the checkout is reclaimed.

Keeping the name here removes the cross-service duplication that would otherwise
let the two drift and leak stale lock files. The module imports only ``pathlib``
so unified_api can depend on it without pulling in the coding-team app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# The per-issue checkout directory name unified_api's ``_resolve_repo_path``
# builds as ``f"issue-{issue_number}"`` (issue numbers are positive integers).
# Kept here as the single source of truth so the cleanup guard recognises exactly
# the shape the path builder produces and nothing broader (e.g. a repo-level
# checkout). If the segment format changes, change it in both places.
_PER_ISSUE_DIR_RE = re.compile(r"issue-\d+")


def is_per_issue_dir(name: str) -> bool:
    """True iff ``name`` is an auto-derived per-issue checkout directory name.

    Preconditions:
        - ``name`` is a path's final component (``Path.name``), not a full path.
    Postconditions:
        - Returns True iff ``name`` exactly matches ``issue-<digits>``. Pure.
    """
    return _PER_ISSUE_DIR_RE.fullmatch(name) is not None


def clone_lock_path(repo_path: str | Path) -> Path:
    """Return the path of the clone lock that guards ``repo_path``.

    The lock lives *beside* the checkout (in its parent), not inside it, so it
    survives the post-success ``rmtree`` of the checkout directory.

    Preconditions:
        - ``repo_path`` is the per-issue checkout path with a non-empty final
          component (its ``name`` is the directory that gets cloned, e.g.
          ``issue-7``) — not a filesystem root. Callers only ever pass the
          per-issue paths ``_resolve_repo_path`` produces; a root path would
          yield a degenerate ``/.clone.lock`` (such paths are rejected upstream
          by the deletion safety guard before this is reached).
    Postconditions:
        - Returns ``<parent>/.<name>.clone.lock``. Pure: no filesystem access.
    Raises:
        - ``ValueError`` when ``repo_path`` has an empty final component (a
          filesystem root), enforcing the precondition rather than emitting a
          degenerate ``/.clone.lock``.
    """
    p = Path(repo_path)
    if not p.name:
        raise ValueError(f"repo_path must have a non-empty final component: {repo_path!r}")
    return p.parent / f".{p.name}.clone.lock"


def agent_cache_dir() -> str:
    """Return the ``AGENT_CACHE`` root, defaulting to the relative ``.agent_cache``.

    Single source of truth for the ``AGENT_CACHE`` default + whitespace handling,
    shared by unified_api's ``_resolve_repo_path`` and
    ``ephemeral_workspace_roots`` so the derived checkout path and the cleanup
    safety root can never diverge.

    Postconditions:
        - Returns the stripped ``AGENT_CACHE`` value, or ``".agent_cache"`` when
          it is unset/blank. Pure: no filesystem access.
    """
    return os.environ.get("AGENT_CACHE", "").strip() or ".agent_cache"


def ephemeral_workspace_roots() -> list[Path]:
    """Roots under which unified_api auto-derives platform-owned per-issue checkouts.

    Mirrors the auto-derived branches of unified_api's ``_resolve_repo_path`` so
    the coding team can confirm a checkout is platform-owned (and therefore safe
    to delete) rather than an operator-pinned or attacker-supplied path. Read
    from the same env vars both services share:

    - ``SE_WORKSPACE_DIR`` / ``WORKSPACE_ROOT`` → ``<root>/<owner>_<repo>/issue-N``
    - ``AGENT_CACHE`` (default ``.agent_cache``) → ``<cache>/github_workspaces/...``

    Postconditions:
        - Returns the resolved candidate roots (de-duplicated, order preserved).
    """
    roots: list[Path] = []
    for var in ("SE_WORKSPACE_DIR", "WORKSPACE_ROOT"):
        val = os.environ.get(var, "").strip()
        if val:
            roots.append(Path(val).resolve())
    roots.append((Path(agent_cache_dir()) / "github_workspaces").resolve())
    # dict.fromkeys de-duplicates while preserving first-seen order (Path is hashable).
    return list(dict.fromkeys(roots))


def is_within_ephemeral_workspace(repo_path: str | Path) -> bool:
    """True iff ``repo_path`` resolves to a path strictly under an ephemeral root.

    Defense-in-depth for the destructive cleanup: even if a caller sets the
    cleanup flag, a checkout is only removable when it lives under a root this
    deployment auto-derives into (see ``ephemeral_workspace_roots``). An
    operator-pinned or arbitrary path is therefore never eligible for deletion.

    Postconditions:
        - Returns True iff the resolved path is a strict descendant of one of
          ``ephemeral_workspace_roots`` (the root itself is excluded); False on
          any resolution error or when no root contains it.
    """
    try:
        p = Path(repo_path).resolve()
    except (OSError, ValueError):
        return False
    return any(root in p.parents for root in ephemeral_workspace_roots())
