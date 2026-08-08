"""Shared delivery helpers for backend/frontend code-v2 teams."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from shared.git.branch_utils import make_branch_suffix, make_slug
from shared.git.git_utils import DEVELOPMENT_BRANCH
from software_engineering_team.shared.v2_models import DeliverResult


class FilesPayload:
    """Minimal duck-type wrapper so ``write_agent_output`` can consume files."""

    def __init__(self, files: Dict[str, str], summary: str, commit_msg: str) -> None:
        self.files = files
        self.summary = summary
        self.suggested_commit_message = commit_msg
        self.gitignore_entries: list[str] = []


@dataclass(frozen=True)
class DeliverGitOps:
    """Git and writer operations used by shared delivery logic.

    Built fresh by ``make_run_deliver`` on every call from its ``git_ns``/
    ``output_ns`` namespaces, which default to the real ``shared.git_utils``/
    ``shared.repo_writer`` modules — tests monkeypatch those modules directly.
    """

    abort_merge: Callable[..., Any]
    checkout_branch: Callable[..., Any]
    commit_working_tree: Callable[..., Any]
    create_feature_branch: Callable[..., Any]
    delete_branch: Callable[..., Any]
    merge_branch: Callable[..., Any]
    write_agent_output: Callable[..., Any]


def _cleanup_handoff_failure(
    repo_path: Path,
    branch_name: str,
    *,
    created_branch: bool,
    ops: DeliverGitOps,
) -> None:
    """Return to development and remove a newly-created failed handoff branch."""
    ops.checkout_branch(repo_path, DEVELOPMENT_BRANCH)
    if created_branch and branch_name:
        ops.delete_branch(repo_path, branch_name)


def prepare_handoff_branch(
    *,
    task_id: str,
    repo_path: Path,
    deliver_files: Dict[str, str],
    summary: str,
    task_title: str,
    feature_branch_name: Optional[str],
    commit_msg_template: str,
    ops: DeliverGitOps,
    logger: logging.Logger,
) -> DeliverResult:
    """Commit a feature branch and leave it ready for external Tech Lead review."""
    result = DeliverResult()
    if not deliver_files:
        result.summary = "No files to deliver."
        return result
    result.delivered_files = sorted(deliver_files)
    slug = make_slug(task_id, task_title)
    branch_suffix = make_branch_suffix(task_id, task_title)
    branch_name = feature_branch_name
    created_branch = False
    if branch_name:
        ok, checkout_msg = ops.checkout_branch(repo_path, branch_name)
        if not ok:
            result.summary = f"Feature branch checkout failed: {checkout_msg}"
            logger.error("[%s] Deliver: %s", task_id, result.summary)
            restore_ok, restore_msg = ops.checkout_branch(repo_path, DEVELOPMENT_BRANCH)
            if not restore_ok:
                logger.error(
                    "[%s] Deliver: failed to restore %s after checkout failure: %s",
                    task_id,
                    DEVELOPMENT_BRANCH,
                    restore_msg,
                )
            return result
    else:
        ok, branch_msg = ops.create_feature_branch(repo_path, DEVELOPMENT_BRANCH, branch_suffix)
        if not ok:
            result.summary = f"Feature branch creation failed: {branch_msg}"
            logger.error("[%s] Deliver: %s", task_id, result.summary)
            ops.checkout_branch(repo_path, DEVELOPMENT_BRANCH)
            return result
        branch_name = branch_msg or f"feature/{branch_suffix}"
        created_branch = True

    result.branch_name = branch_name or ""
    commit_msg = commit_msg_template.format(scope=slug[:20], summary=summary[:72])
    payload = FilesPayload(deliver_files, summary, commit_msg)
    write_ok, write_msg = ops.write_agent_output(repo_path, payload, subdir="")
    if not write_ok:
        result.summary = f"Write failed: {write_msg}"
        logger.error("[%s] Deliver: %s", task_id, result.summary)
        _cleanup_handoff_failure(
            repo_path, result.branch_name, created_branch=created_branch, ops=ops
        )
        return result

    commit_ok, commit_msg_out = ops.commit_working_tree(repo_path, commit_msg)
    if not commit_ok:
        result.summary = f"Commit failed: {commit_msg_out}"
        logger.error("[%s] Deliver: %s", task_id, result.summary)
        _cleanup_handoff_failure(
            repo_path, result.branch_name, created_branch=created_branch, ops=ops
        )
        return result

    result.commit_messages.append(commit_msg)
    result.branch_ready = True
    result.summary = f"Prepared {result.branch_name} for Tech Lead review."
    logger.info("[%s] Deliver: %s", task_id, result.summary)
    return result


def deliver_inline_merge(
    *,
    task_id: str,
    repo_path: Path,
    deliver_files: Dict[str, str],
    summary: str,
    task_title: str,
    commit_msg_template: str,
    ops: DeliverGitOps,
    logger: logging.Logger,
) -> DeliverResult:
    """Create a feature branch, write files, merge it, and restore development."""
    result = DeliverResult()
    if not deliver_files:
        result.summary = "No files to deliver."
        return result
    result.delivered_files = sorted(deliver_files)

    slug = make_slug(task_id, task_title)
    branch_suffix = make_branch_suffix(task_id, task_title)
    ok, branch_msg = ops.create_feature_branch(repo_path, DEVELOPMENT_BRANCH, branch_suffix)
    if not ok:
        result.summary = f"Feature branch creation failed: {branch_msg}"
        logger.error("[%s] Deliver: %s", task_id, result.summary)
        ops.checkout_branch(repo_path, DEVELOPMENT_BRANCH)
        return result
    result.branch_name = branch_msg or f"feature/{branch_suffix}"

    commit_msg = commit_msg_template.format(scope=slug[:20], summary=summary[:72])
    payload = FilesPayload(deliver_files, summary, commit_msg)
    write_ok, write_msg = ops.write_agent_output(repo_path, payload, subdir="")
    if not write_ok:
        result.summary = f"Write failed: {write_msg}"
        logger.error("[%s] Deliver: %s", task_id, result.summary)
        ops.checkout_branch(repo_path, DEVELOPMENT_BRANCH)
        return result
    result.commit_messages.append(commit_msg)

    merge_ok, merge_msg = ops.merge_branch(repo_path, result.branch_name, DEVELOPMENT_BRANCH)
    if not merge_ok:
        result.summary = f"Merge failed: {merge_msg}"
        logger.error("[%s] Deliver: %s", task_id, result.summary)
        ops.abort_merge(repo_path)
        ops.checkout_branch(repo_path, DEVELOPMENT_BRANCH)
        return result

    result.merged = True
    result.branch_ready = True
    ops.delete_branch(repo_path, result.branch_name)
    ops.checkout_branch(repo_path, DEVELOPMENT_BRANCH)
    result.summary = f"Merged {result.branch_name} \u2192 {DEVELOPMENT_BRANCH}."
    logger.info("[%s] Deliver: %s", task_id, result.summary)
    return result
