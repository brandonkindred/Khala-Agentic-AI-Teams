"""Shared delivery helpers for backend/frontend code-v2 teams."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from shared.git.branch_utils import make_branch_suffix, make_slug
from shared.git.git_utils import DEVELOPMENT_BRANCH
from software_engineering_team.shared.v2_models import DeliverResult

BuildVerifier = Callable[[Path, str, str], Tuple[bool, str]]


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


def run_pre_merge_quality_gate(
    *,
    repo_path: Path,
    task_id: str,
    build_verifier: Optional[BuildVerifier] = None,
    build_verify_label: str = "",
    linting_tool_agent: Any = None,
    lint_agent_type: str = "",
    logger: logging.Logger,
) -> Tuple[bool, str]:
    """Run a whole-repo build/lint check on the final delivered state before merge.

    Compensating gate for the standalone code-v2 endpoints, which invoke
    ``run_workflow`` with ``merge_to_development`` defaulting to ``True`` and no
    subsequent Tech Lead re-review (unlike the swarm-orchestrated path). Mirrors
    the per-microtask build/lint check in ``v2_review.py`` but runs once, here,
    immediately before a merge actually happens.

    Preconditions:
        ``repo_path`` reflects the final delivered file state (already written
        and committed) when this is called.
    Postconditions:
        Returns ``(True, "")`` when both checks pass or are skipped (verifier/
        agent is ``None``). A build failure -- returned or raised -- fails
        closed: returns ``(False, reason)``. A genuine lint failure (the tool
        ran and reported issues) also fails closed. An exception raised by the
        lint tool itself (infrastructure failure, not a lint violation) fails
        open and is logged, matching the per-microtask lint check's philosophy.
        Never raises.
    """
    failures: list[str] = []

    if build_verifier is not None:
        try:
            build_ok, build_msg = build_verifier(repo_path, build_verify_label, task_id)
        except Exception as exc:
            build_ok, build_msg = False, str(exc)
        if not build_ok:
            failures.append(f"Build failed: {build_msg}")

    if linting_tool_agent is not None:
        try:
            from software_engineering_team.linting_tool_agent.models import (
                LintToolInput as _LintInput,
            )
            from software_engineering_team.shared.v2_review import _lint_passed

            lint_result = linting_tool_agent.run(
                _LintInput(
                    repo_path=str(repo_path),
                    agent_type=lint_agent_type,
                    task_id=task_id,
                    task_description="",
                )
            )
            if lint_result and not _lint_passed(lint_result):
                failures.append("Lint failed.")
        except Exception as exc:
            logger.warning(
                "[%s] Pre-merge quality gate: linting tool agent failed: %s", task_id, exc
            )

    if failures:
        return False, "; ".join(failures)
    return True, ""


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
    build_verifier: Optional[BuildVerifier] = None,
    build_verify_label: str = "",
    linting_tool_agent: Any = None,
    lint_agent_type: str = "",
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

    gate_ok, gate_msg = run_pre_merge_quality_gate(
        repo_path=repo_path,
        task_id=task_id,
        build_verifier=build_verifier,
        build_verify_label=build_verify_label,
        linting_tool_agent=linting_tool_agent,
        lint_agent_type=lint_agent_type,
        logger=logger,
    )
    if not gate_ok:
        result.summary = f"Pre-merge quality gate failed: {gate_msg}"
        logger.error("[%s] Deliver: %s", task_id, result.summary)
        ops.checkout_branch(repo_path, DEVELOPMENT_BRANCH)
        return result

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
