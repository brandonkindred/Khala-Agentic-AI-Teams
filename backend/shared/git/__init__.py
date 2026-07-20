"""Neutral, team-agnostic git operations.

Git branch/commit/merge/diff helpers (``git_utils``) and branch-name helpers
(``branch_utils``) used by both the software-engineering team and the coding team.
Git ops were the original proof that cross-team sharing works; this package gives
them a neutral home so the sharing is no longer "one team importing the other's
internals."

Layout:
    - ``git_utils``   — was ``software_engineering_team/shared/git_utils.py``
    - ``branch_utils`` — was ``software_engineering_team/shared/branch_utils.py``

Both submodules are stdlib-only (``subprocess``/``re``/``hashlib``). The public
API is large and stable; import the specific names you need from the submodule,
e.g. ``from shared_git.git_utils import branch_diff, checkout_branch``.

Preconditions:
    - ``backend/agents`` is on ``sys.path`` (the ``shared_*`` convention).
Postconditions:
    - Importing the package has no side effects and runs no git commands.
"""

from __future__ import annotations

from shared_git import branch_utils, git_utils

__all__ = ["git_utils", "branch_utils"]
