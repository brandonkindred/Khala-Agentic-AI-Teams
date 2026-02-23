"""
Git branch management tool agent: create feature branch, commit along the way, merge to development.

Uses only shared.git_utils and shared.repo_writer. No backend_agent code.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from shared.git_utils import (
    DEVELOPMENT_BRANCH,
    abort_merge,
    checkout_branch,
    commit_working_tree,
    create_feature_branch as git_create_feature_branch,
    delete_branch,
    merge_branch,
)

from ...models import (
    Phase,
    ToolAgentInput,
    ToolAgentOutput,
    ToolAgentPhaseInput,
    ToolAgentPhaseOutput,
)
from ...prompts import DELIVER_COMMIT_MSG_TEMPLATE

logger = logging.getLogger(__name__)


class _FilesPayload:
    """Minimal payload for write_agent_output."""

    def __init__(self, files: Dict[str, str], summary: str, commit_msg: str) -> None:
        self.files = files
        self.summary = summary
        self.suggested_commit_message = commit_msg
        self.gitignore_entries: list[str] = []


class GitBranchManagementToolAgent:
    """
    Manages feature branch creation, incremental commits during execution,
    and merge back to development in Deliver.
    """

    def run(self, inp: ToolAgentInput) -> ToolAgentOutput:
        return self.execute(inp)

    def execute(self, inp: ToolAgentInput) -> ToolAgentOutput:
        """No-op for normal microtasks; git ops are invoked by orchestrator/deliver."""
        return ToolAgentOutput(summary="Git branch management: no execution for microtasks.")

    def plan(self, phase_inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Recommend branch naming and commit strategy."""
        return ToolAgentPhaseOutput(
            recommendations=[
                "Use feature/<task_id>-<slug> branch name off development.",
                "Commit after each execution iteration to preserve incremental progress.",
            ],
            summary="Git branch management planning input provided.",
        )

    def review(self, phase_inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Recommend commit message format and squashing before merge."""
        return ToolAgentPhaseOutput(
            recommendations=[
                "Use conventional commit messages (feat(scope): summary).",
                "Consider squashing WIP commits before final merge if desired.",
            ],
            summary="Git branch management review completed.",
        )

    def problem_solve(self, phase_inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Recommend revert or branch fixes for issues."""
        return ToolAgentPhaseOutput(
            recommendations=[
                "If needed, revert last commit or fix on feature branch before re-review.",
            ],
            summary="Git branch management problem-solving input provided.",
        )

    def create_feature_branch(
        self,
        repo_path: str | Path,
        task_id: str,
        task_title: str,
    ) -> Tuple[bool, Optional[str]]:
        """
        Create feature branch from development and checkout it.
        Returns (success, branch_name or None).
        """
        path = Path(repo_path).resolve()
        if not (path / ".git").exists():
            logger.warning("Git branch management: not a git repository at %s", path)
            return False, None
        slug = re.sub(r"[^a-z0-9-]+", "-", (task_title or task_id).lower()).strip("-")[:40] or "task"
        ok, branch_msg = git_create_feature_branch(path, DEVELOPMENT_BRANCH, f"{task_id}-{slug}")
        if not ok:
            return False, None
        branch_name = branch_msg or f"feature/{task_id}-{slug}"
        return True, branch_name

    def commit_current_changes(self, repo_path: str | Path, message: str) -> Tuple[bool, str]:
        """Commit current working tree (add -A, commit -m). Returns (success, message)."""
        return commit_working_tree(repo_path, message)

    def deliver(self, phase_inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """
        If feature_branch_name is set: commit any remaining changes, merge to development, delete branch.
        If not set: create feature branch, write current_files, commit, merge, delete branch (fallback).
        """
        repo_path = Path(phase_inp.repo_path).resolve() if phase_inp.repo_path else None
        if not repo_path or not (repo_path / ".git").exists():
            return ToolAgentPhaseOutput(success=False, summary="Not a git repository.")

        task_id = phase_inp.task_id or "task"
        task_title = phase_inp.task_title or ""
        slug = re.sub(r"[^a-z0-9-]+", "-", task_title.lower()).strip("-")[:40] if task_title else "task"
        branch_name: Optional[str] = phase_inp.feature_branch_name

        if branch_name:
            # We have been committing along the way; commit any remaining, then merge and cleanup.
            commit_working_tree(repo_path, f"chore: finalize before merge")
            merge_ok, merge_msg = merge_branch(repo_path, branch_name, DEVELOPMENT_BRANCH)
            if not merge_ok:
                abort_merge(repo_path)
                checkout_branch(repo_path, branch_name)
                return ToolAgentPhaseOutput(success=False, summary=f"Merge failed: {merge_msg}")
            delete_branch(repo_path, branch_name)
            checkout_branch(repo_path, DEVELOPMENT_BRANCH)
            return ToolAgentPhaseOutput(
                success=True,
                summary=f"Merged {branch_name} → {DEVELOPMENT_BRANCH}.",
            )
        else:
            # Fallback: create branch, write files, commit, merge, delete.
            ok, created_branch = self.create_feature_branch(repo_path, task_id, task_title)
            if not ok or not created_branch:
                return ToolAgentPhaseOutput(success=False, summary="Feature branch creation failed.")
            from shared.repo_writer import write_agent_output

            scope = slug[:20]
            summary = (phase_inp.task_description or "")[:72]
            commit_msg = DELIVER_COMMIT_MSG_TEMPLATE.format(scope=scope, summary=summary or "deliver")
            payload = _FilesPayload(phase_inp.current_files, summary, commit_msg)
            write_ok, write_msg = write_agent_output(repo_path, payload, subdir="")
            if not write_ok:
                checkout_branch(repo_path, DEVELOPMENT_BRANCH)
                return ToolAgentPhaseOutput(success=False, summary=f"Write failed: {write_msg}")
            merge_ok, merge_msg = merge_branch(repo_path, created_branch, DEVELOPMENT_BRANCH)
            if not merge_ok:
                abort_merge(repo_path)
                checkout_branch(repo_path, DEVELOPMENT_BRANCH)
                return ToolAgentPhaseOutput(success=False, summary=f"Merge failed: {merge_msg}")
            delete_branch(repo_path, created_branch)
            checkout_branch(repo_path, DEVELOPMENT_BRANCH)
            return ToolAgentPhaseOutput(
                success=True,
                summary=f"Merged {created_branch} → {DEVELOPMENT_BRANCH}.",
            )
