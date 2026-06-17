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

from pathlib import Path


def clone_lock_path(repo_path: str | Path) -> Path:
    """Return the path of the clone lock that guards ``repo_path``.

    The lock lives *beside* the checkout (in its parent), not inside it, so it
    survives the post-success ``rmtree`` of the checkout directory.

    Preconditions:
        - ``repo_path`` is the per-issue checkout path (its ``name`` is the
          directory that gets cloned, e.g. ``issue-7``).
    Postconditions:
        - Returns ``<parent>/.<name>.clone.lock``. Pure: no filesystem access.
    """
    p = Path(repo_path)
    return p.parent / f".{p.name}.clone.lock"
