"""Compatibility shim: git utilities moved to the neutral ``shared_git`` package.

Aliases ``software_engineering_team.shared.git_utils`` onto
``shared_git.git_utils`` via ``sys.modules`` so existing imports and
``@patch("software_engineering_team.shared.git_utils.…")`` calls keep working (the
old path resolves to the *same* module object). New code imports ``shared_git``.
"""

from __future__ import annotations

import sys

from shared_git import git_utils as _git_utils

sys.modules[__name__] = _git_utils
