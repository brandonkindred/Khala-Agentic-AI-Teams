"""
Deliver phase: write files, commit, and merge to development.

Uses only ``shared.git_utils`` — no code from ``backend_agent``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from software_engineering_team.shared.deliver_utils import (
    DeliverGitOps,
    deliver_inline_merge,
    prepare_handoff_branch,
)
from software_engineering_team.shared.git_utils import (
    DEVELOPMENT_BRANCH,
    abort_merge,
    checkout_branch,
    commit_working_tree,
    create_feature_branch,
    delete_branch,
    merge_branch,
)
from software_engineering_team.shared.repo_writer import write_agent_output

from ..models import DeliverResult, Phase, ToolAgentKind, ToolAgentPhaseInput
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

    If the Git branch management agent is present, delegate all git operations to it
    (merge to development when feature_branch_name is set, or create/write/commit/merge
    when not). When merge_to_development is False, prepare and commit the feature branch
    but leave it unmerged for an external Tech Lead review.
    """
    result = DeliverResult()
    deliver_files = dict(files)

    if tool_agents:  # pragma: no cover  # integration-only: dispatches tool agents that run real git/build/deploy
        phase_inp = ToolAgentPhaseInput(
            phase=Phase.DELIVER,
            repo_path=str(repo_path),
            current_files=deliver_files,
            task_title=task_title,
            task_description=task_description,
            task_id=task_id,
        )
        for kind, agent in tool_agents.items():
            if kind == ToolAgentKind.GIT_BRANCH_MANAGEMENT:
                continue
            if not hasattr(agent, "deliver"):
                continue
            try:
                out = agent.deliver(phase_inp)
                if out.files:
                    deliver_files.update(out.files)
            except Exception as exc:
                logger.warning("[%s] Tool agent %s deliver() failed: %s", task_id, kind.value, exc)

        if not deliver_files:
            result.summary = "No files to deliver."
            return result

        if not merge_to_development:
            return prepare_handoff_branch(
                task_id=task_id,
                repo_path=repo_path,
                deliver_files=deliver_files,
                summary=summary,
                task_title=task_title,
                feature_branch_name=feature_branch_name,
                commit_msg_template=DELIVER_COMMIT_MSG_TEMPLATE,
                ops=_git_ops(),
                logger=logger,
            )

        git_agent = tool_agents.get(ToolAgentKind.GIT_BRANCH_MANAGEMENT)
        if git_agent is not None and hasattr(git_agent, "deliver"):
            phase_inp = ToolAgentPhaseInput(
                phase=Phase.DELIVER,
                repo_path=str(repo_path),
                current_files=deliver_files,
                task_title=task_title,
                task_description=task_description,
                task_id=task_id,
                feature_branch_name=feature_branch_name,
            )
            try:
                out = git_agent.deliver(phase_inp)
                result.merged = out.success
                result.branch_ready = bool(out.success)
                result.summary = out.summary or result.summary
                result.branch_name = feature_branch_name or ""
                if out.success:
                    result.commit_messages.append(out.summary or "Merged to development")
                    result.delivered_files = sorted(deliver_files)
                logger.info("[%s] Deliver (Git agent): %s", task_id, result.summary)
                return result
            except Exception as exc:
                logger.warning(
                    "[%s] Git agent deliver() failed, falling back to inline: %s", task_id, exc
                )

    if not deliver_files:
        result.summary = "No files to deliver."
        return result
    result.delivered_files = sorted(deliver_files)

    if not merge_to_development:
        return prepare_handoff_branch(
            task_id=task_id,
            repo_path=repo_path,
            deliver_files=deliver_files,
            summary=summary,
            task_title=task_title,
            feature_branch_name=feature_branch_name,
            commit_msg_template=DELIVER_COMMIT_MSG_TEMPLATE,
            ops=_git_ops(),
            logger=logger,
        )

    return deliver_inline_merge(
        task_id=task_id,
        repo_path=repo_path,
        deliver_files=deliver_files,
        summary=summary,
        task_title=task_title,
        commit_msg_template=DELIVER_COMMIT_MSG_TEMPLATE,
        ops=_git_ops(),
        logger=logger,
    )
