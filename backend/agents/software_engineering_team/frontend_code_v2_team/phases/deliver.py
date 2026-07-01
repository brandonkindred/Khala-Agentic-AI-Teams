"""
Deliver phase: write files, commit, and merge to development.

Uses only shared.git_utils and shared.repo_writer. No frontend_team code.

The orchestration is shared across the code-v2 teams (see
``shared/phases/deliver.py``); this module keeps the git-function imports and
``_git_ops()`` so tests can monkeypatch git operations at this module boundary,
and wires in the frontend team's models.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from software_engineering_team.shared.deliver_utils import DeliverGitOps
from software_engineering_team.shared.git_utils import (
    DEVELOPMENT_BRANCH,
    abort_merge,
    checkout_branch,
    commit_working_tree,
    create_feature_branch,
    delete_branch,
    merge_branch,
)
from software_engineering_team.shared.phases.deliver import run_deliver_impl
from software_engineering_team.shared.repo_writer import write_agent_output

from .. import models as _models
from ..models import DeliverResult, ToolAgentKind
from ..prompts import DELIVER_COMMIT_MSG_TEMPLATE

logger = logging.getLogger(__name__)
__all__ = ["DEVELOPMENT_BRANCH", "run_deliver"]


def _git_ops() -> DeliverGitOps:
    """Return current module git functions so tests can monkeypatch this boundary."""
    return DeliverGitOps(
        abort_merge=abort_merge,
        checkout_branch=checkout_branch,
        commit_working_tree=commit_working_tree,
        create_feature_branch=create_feature_branch,
        delete_branch=delete_branch,
        merge_branch=merge_branch,
        write_agent_output=write_agent_output,
    )


def run_deliver(
    *,
    task_id: str,
    repo_path: Path,
    files: Dict[str, str],
    summary: str,
    task_title: str = "",
    tool_agents: Optional[Dict[ToolAgentKind, Any]] = None,
    task_description: str = "",
    feature_branch_name: Optional[str] = None,
    merge_to_development: bool = True,
) -> DeliverResult:
    """
    Create feature branch, write files, commit, merge to development.
    When Git branch management agent is present, delegate to it; else inline git.
    When merge_to_development is False, commit the feature branch and leave it
    ready for an external Tech Lead review instead of merging/deleting it.

    Preconditions:
        ``repo_path`` is a git repo; ``files`` maps relative paths to content.
    Postconditions:
        Returns a ``DeliverResult``; git side effects run through ``_git_ops()``.
    """
    return run_deliver_impl(
        task_id=task_id,
        repo_path=repo_path,
        files=files,
        summary=summary,
        task_title=task_title,
        tool_agents=tool_agents,
        task_description=task_description,
        feature_branch_name=feature_branch_name,
        merge_to_development=merge_to_development,
        ops=_git_ops(),
        commit_msg_template=DELIVER_COMMIT_MSG_TEMPLATE,
        models=_models,
        logger=logger,
    )
