"""Compatibility shim: branch-name helpers moved to ``shared.git.branch_utils``.

Aliases ``software_engineering_team.shared.branch_utils`` onto
``shared.git.branch_utils`` via ``sys.modules`` so existing imports keep working
against the same module object. New code imports ``shared.git``.
"""

from __future__ import annotations

import sys

from shared.git import branch_utils as _branch_utils

sys.modules[__name__] = _branch_utils
