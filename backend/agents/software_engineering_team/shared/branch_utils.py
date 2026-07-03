"""Compatibility shim: branch-name helpers moved to ``shared_git.branch_utils``.

Aliases ``software_engineering_team.shared.branch_utils`` onto
``shared_git.branch_utils`` via ``sys.modules`` so existing imports keep working
against the same module object. New code imports ``shared_git``.
"""

from __future__ import annotations

import sys

from shared_git import branch_utils as _branch_utils

sys.modules[__name__] = _branch_utils
