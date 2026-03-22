"""
Deliver phase: write files, commit, and merge to development.

Uses only ``shared.git_utils`` — no code from ``backend_agent``.
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
    create_feature_branch,
    delete_branch,
    merge_branch,
)
from software_engineering_team.shared.repo_writer import write_agent_output

from ..models import DeliverResult, Phase, ToolAgentKind, ToolAgentPhaseInput
from ..prompts import DELIVER_COMMIT_MSG_TEMPLATE

logger = logging.getLogger(__name__)


class _FilesPayload:
    """Minimal duck-type wrapper so ``write_agent_output`` can consume it."""

    def __init__(self, files: Dict[str, str], summary: str, commit_msg: str) -> None:
        self.files = files
        self.summary = summary
        self.suggested_commit_message = commit_msg
        self.gitignore_entries: list[str] = []


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
) -> DeliverResult:
    """
    Create feature branch, write files, commit, merge to development.

    If the Git branch management agent is present, delegate all git operations to it
    (merge to development when feature_branch_name is set, or create/write/commit/merge
    when not). Otherwise call other tool agents' deliver() for domain actions, then
    perform inline git create/write/commit/merge.
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
                result.summary = out.summary or result.summary
                result.branch_name = feature_branch_name or ""
                if out.success:
                    result.commit_messages.append(out.summary or "Merged to development")
                logger.info("[%s] Deliver (Git agent): %s", task_id, result.summary)
                return result
            except Exception as exc:
                logger.warning("[%s] Git agent deliver() failed, falling back to inline: %s", task_id, exc)

    if not deliver_files:
        result.summary = "No files to deliver."
        return result

    # Fallback: inline git (no Git agent or Git agent failed)
    slug = re.sub(r"[^a-z0-9-]+", "-", (task_title or task_id).lower()).strip("-")[:40] or "task"
    ok, branch_msg = create_feature_branch(repo_path, DEVELOPMENT_BRANCH, f"{task_id}-{slug}")
    if not ok:
        result.summary = f"Feature branch creation failed: {branch_msg}"
        logger.error("[%s] Deliver: %s", task_id, result.summary)
        checkout_branch(repo_path, DEVELOPMENT_BRANCH)
        return result
    result.branch_name = branch_msg or f"feature/{task_id}-{slug}"

    # 2. Write files and commit
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

    # 3. Merge to development
    merge_ok, merge_msg = merge_branch(repo_path, result.branch_name, DEVELOPMENT_BRANCH)
    if not merge_ok:
        result.summary = f"Merge failed: {merge_msg}"
        logger.error("[%s] Deliver: %s", task_id, result.summary)
        abort_merge(repo_path)
        checkout_branch(repo_path, DEVELOPMENT_BRANCH)
        return result

    result.merged = True

    # 4. Cleanup feature branch
    delete_branch(repo_path, result.branch_name)
    checkout_branch(repo_path, DEVELOPMENT_BRANCH)

    result.summary = f"Merged {result.branch_name} → {DEVELOPMENT_BRANCH}."
    logger.info("[%s] Deliver: %s", task_id, result.summary)
    return result
