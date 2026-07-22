"""
Deliver phase: write files, commit, and merge to development.

Uses only ``shared.git_utils`` and ``shared.repo_writer`` — no team-specific code.

The orchestration is shared (``shared/phases/deliver.py``); this module keeps the
git-function imports so tests can monkeypatch git operations at this module
boundary, and binds team models / commit template via ``make_run_deliver``.
"""

from __future__ import annotations

import logging
import sys

from software_engineering_team.shared.git_utils import (  # noqa: F401  # re-exported patch surface
    DEVELOPMENT_BRANCH,
    abort_merge,
    checkout_branch,
    commit_working_tree,
    create_feature_branch,
    delete_branch,
    merge_branch,
)
from software_engineering_team.shared.phases.deliver import make_run_deliver
from software_engineering_team.shared.repo_writer import write_agent_output  # noqa: F401

from .. import models as _models
from ..prompts import DELIVER_COMMIT_MSG_TEMPLATE

logger = logging.getLogger(__name__)
__all__ = ["DEVELOPMENT_BRANCH", "run_deliver"]

run_deliver = make_run_deliver(
    git_ns=sys.modules[__name__],
    models=_models,
    commit_msg_template=DELIVER_COMMIT_MSG_TEMPLATE,
    logger=logger,
)
