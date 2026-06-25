"""
Deliver phase: write files, commit, and merge to development.

Uses only shared.git_utils and shared.repo_writer. No frontend_team code.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional

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


class _FilesPayload:
    def __init__(self, files: Dict[str, str], summary: str, commit_msg: str) -> None:
        self.files = files
        self.summary = summary
        self.suggested_commit_message = commit_msg
        self.gitignore_entries: list[str] = []


def _make_slug(task_id: str, task_title: str) -> str:
    """Return the stable branch/commit scope slug for a delivery task."""
    return re.sub(r"[^a-z0-9-]+", "-", (task_title or task_id).lower()).strip("-")[:40] or "task"


def _cleanup_handoff_failure(repo_path: Path, branch_name: str, *, created_branch: bool) -> None:
    """Return to development and remove a newly-created failed handoff branch."""
    checkout_branch(repo_path, DEVELOPMENT_BRANCH)
    if created_branch and branch_name:
        delete_branch(repo_path, branch_name)


def _prepare_handoff_branch(
    *,
    task_id: str,
    repo_path: Path,
    deliver_files: Dict[str, str],
    summary: str,
    task_title: str,
    feature_branch_name: Optional[str],
) -> DeliverResult:
    """Commit a feature branch and leave it ready for external Tech Lead review."""
    result = DeliverResult()
    slug = _make_slug(task_id, task_title)
    branch_name = feature_branch_name
    created_branch = False
    if branch_name:
        ok, checkout_msg = checkout_branch(repo_path, branch_name)
        if not ok:
            result.summary = f"Feature branch checkout failed: {checkout_msg}"
            logger.error("[%s] Deliver: %s", task_id, result.summary)
            return result
    else:
        ok, branch_msg = create_feature_branch(repo_path, DEVELOPMENT_BRANCH, f"{task_id}-{slug}")
        if not ok:
            result.summary = f"Feature branch creation failed: {branch_msg}"
            logger.error("[%s] Deliver: %s", task_id, result.summary)
            checkout_branch(repo_path, DEVELOPMENT_BRANCH)
            return result
        branch_name = branch_msg or f"feature/{task_id}-{slug}"
        created_branch = True

    result.branch_name = branch_name or ""
    commit_msg = DELIVER_COMMIT_MSG_TEMPLATE.format(scope=slug[:20], summary=summary[:72])
    payload = _FilesPayload(deliver_files, summary, commit_msg)
    write_ok, write_msg = write_agent_output(repo_path, payload, subdir="")
    if not write_ok:
        result.summary = f"Write failed: {write_msg}"
        logger.error("[%s] Deliver: %s", task_id, result.summary)
        _cleanup_handoff_failure(repo_path, result.branch_name, created_branch=created_branch)
        return result

    commit_ok, commit_msg_out = commit_working_tree(repo_path, commit_msg)
    if not commit_ok:
        result.summary = f"Commit failed: {commit_msg_out}"
        logger.error("[%s] Deliver: %s", task_id, result.summary)
        _cleanup_handoff_failure(repo_path, result.branch_name, created_branch=created_branch)
        return result

    result.commit_messages.append(commit_msg)
    result.branch_ready = True
    result.summary = f"Prepared {result.branch_name} for Tech Lead review."
    logger.info("[%s] Deliver: %s", task_id, result.summary)
    return result


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
    """
    result = DeliverResult()
    deliver_files = dict(files)

    if tool_agents:
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

        if not merge_to_development:
            return _prepare_handoff_branch(
                task_id=task_id,
                repo_path=repo_path,
                deliver_files=deliver_files,
                summary=summary,
                task_title=task_title,
                feature_branch_name=feature_branch_name,
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
                logger.info("[%s] Deliver (Git agent): %s", task_id, result.summary)
                return result
            except Exception as exc:
                logger.warning(
                    "[%s] Git agent deliver() failed, falling back to inline: %s", task_id, exc
                )

    if not deliver_files:
        result.summary = "No files to deliver."
        return result

    if not merge_to_development:
        return _prepare_handoff_branch(
            task_id=task_id,
            repo_path=repo_path,
            deliver_files=deliver_files,
            summary=summary,
            task_title=task_title,
            feature_branch_name=feature_branch_name,
        )

    slug = _make_slug(task_id, task_title)
    ok, branch_msg = create_feature_branch(repo_path, DEVELOPMENT_BRANCH, f"{task_id}-{slug}")
    if not ok:
        result.summary = f"Feature branch creation failed: {branch_msg}"
        logger.error("[%s] Deliver: %s", task_id, result.summary)
        checkout_branch(repo_path, DEVELOPMENT_BRANCH)
        return result
    result.branch_name = branch_msg or f"feature/{task_id}-{slug}"

    scope = slug[:20]
    commit_msg = DELIVER_COMMIT_MSG_TEMPLATE.format(scope=scope, summary=summary[:72])
    payload = _FilesPayload(deliver_files, summary, commit_msg)
    write_ok, write_msg = write_agent_output(repo_path, payload, subdir="")
    if not write_ok:
        result.summary = f"Write failed: {write_msg}"
        logger.error("[%s] Deliver: %s", task_id, result.summary)
        checkout_branch(repo_path, DEVELOPMENT_BRANCH)
        return result
    result.commit_messages.append(commit_msg)

    merge_ok, merge_msg = merge_branch(repo_path, result.branch_name, DEVELOPMENT_BRANCH)
    if not merge_ok:
        result.summary = f"Merge failed: {merge_msg}"
        logger.error("[%s] Deliver: %s", task_id, result.summary)
        abort_merge(repo_path)
        checkout_branch(repo_path, DEVELOPMENT_BRANCH)
        return result

    result.merged = True
    result.branch_ready = True
    delete_branch(repo_path, result.branch_name)
    checkout_branch(repo_path, DEVELOPMENT_BRANCH)
    result.summary = f"Merged {result.branch_name} → {DEVELOPMENT_BRANCH}."
    logger.info("[%s] Deliver: %s", task_id, result.summary)
    return result
