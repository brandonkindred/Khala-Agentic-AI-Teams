"""
Shared Deliver-phase implementation for the code-v2 teams.

The backend and frontend deliver phases differed only in docstrings. The real
git work already lives in ``shared/deliver_utils.py`` (via ``DeliverGitOps``);
this collapses the remaining orchestration wrapper into one place.

``make_run_deliver`` builds a ``DeliverGitOps`` bundle from ``git_ns``/``output_ns``
fresh on every call, so tests can monkeypatch ``shared.git_utils``/``shared.repo_writer``
directly (even after ``make_run_deliver`` has been called) without needing a
per-team wrapper module as the patch surface.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from shared.git import git_utils
from software_engineering_team.shared import repo_writer
from software_engineering_team.shared.deliver_utils import (
    DeliverGitOps,
    deliver_inline_merge,
    prepare_handoff_branch,
)
from software_engineering_team.shared.stack_profile import PhaseModels


def run_deliver_impl(
    *,
    task_id: str,
    repo_path: Path,
    files: Dict[str, str],
    summary: str,
    task_title: str,
    tool_agents: Optional[Dict[Any, Any]],
    task_description: str,
    feature_branch_name: Optional[str],
    merge_to_development: bool,
    ops: DeliverGitOps,
    commit_msg_template: str,
    models: PhaseModels,
    logger: logging.Logger,
    build_verifier: Optional[Callable[[Path, str, str], Any]] = None,
    build_verify_label: str = "",
    linting_tool_agent: Any = None,
    lint_agent_type: str = "",
) -> Any:
    """Create feature branch, write files, commit, merge to development.

    If the Git branch management agent is present, delegate all git operations to it
    (merge to development when feature_branch_name is set, or create/write/commit/merge
    when not). When merge_to_development is False, prepare and commit the feature branch
    but leave it unmerged for an external Tech Lead review.

    Preconditions:
        ``ops`` bundles the (possibly test-patched) git callables; ``models``
        exposes ``DeliverResult``, ``Phase``, ``ToolAgentKind``, and
        ``ToolAgentPhaseInput``; ``commit_msg_template`` has ``{scope}`` and
        ``{summary}`` slots.
    Postconditions:
        Returns a ``DeliverResult``. When there are no files to deliver, returns
        early with ``summary="No files to deliver."`` and no git side effects.
    """
    deliver_result_cls = models.DeliverResult
    phase_enum = models.Phase
    tool_agent_kind_enum = models.ToolAgentKind
    phase_input_cls = models.ToolAgentPhaseInput

    result = deliver_result_cls()
    deliver_files = dict(files)

    if tool_agents:  # pragma: no cover  # integration-only: dispatches tool agents that run real git/build/deploy
        phase_inp = phase_input_cls(
            phase=phase_enum.DELIVER,
            repo_path=str(repo_path),
            current_files=deliver_files,
            task_title=task_title,
            task_description=task_description,
            task_id=task_id,
        )
        for kind, agent in tool_agents.items():
            if kind == tool_agent_kind_enum.GIT_BRANCH_MANAGEMENT:
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
    result.delivered_files = sorted(deliver_files)

    if not merge_to_development:
        return prepare_handoff_branch(
            task_id=task_id,
            repo_path=repo_path,
            deliver_files=deliver_files,
            summary=summary,
            task_title=task_title,
            feature_branch_name=feature_branch_name,
            commit_msg_template=commit_msg_template,
            ops=ops,
            logger=logger,
        )

    if tool_agents:  # pragma: no cover  # integration-only: dispatches tool agents that run real git/build/deploy
        git_agent = tool_agents.get(tool_agent_kind_enum.GIT_BRANCH_MANAGEMENT)
        if git_agent is not None and hasattr(git_agent, "deliver"):
            phase_inp = phase_input_cls(
                phase=phase_enum.DELIVER,
                repo_path=str(repo_path),
                current_files=deliver_files,
                task_title=task_title,
                task_description=task_description,
                task_id=task_id,
                feature_branch_name=feature_branch_name,
                build_verifier=build_verifier,
                build_verify_label=build_verify_label,
                linting_tool_agent=linting_tool_agent,
                lint_agent_type=lint_agent_type,
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

    return deliver_inline_merge(
        task_id=task_id,
        repo_path=repo_path,
        deliver_files=deliver_files,
        summary=summary,
        task_title=task_title,
        commit_msg_template=commit_msg_template,
        ops=ops,
        logger=logger,
        build_verifier=build_verifier,
        build_verify_label=build_verify_label,
        linting_tool_agent=linting_tool_agent,
        lint_agent_type=lint_agent_type,
    )


def make_run_deliver(
    *,
    git_ns: Any = git_utils,
    output_ns: Any = repo_writer,
    models: PhaseModels,
    commit_msg_template: str,
    logger: logging.Logger,
) -> Callable[..., Any]:
    """Bind a team-facing ``run_deliver`` over the shared deliver implementation.

    Preconditions:
        ``git_ns`` exposes ``abort_merge``, ``checkout_branch``,
        ``commit_working_tree``, ``create_feature_branch``, ``delete_branch``,
        and ``merge_branch``; ``output_ns`` exposes ``write_agent_output``;
        both default to the real ``shared.git_utils``/``shared.repo_writer``
        modules. ``models`` satisfies ``PhaseModels``; ``commit_msg_template``
        has ``{scope}`` and ``{summary}`` slots; ``logger`` is a
        ``logging.Logger``.
    Postconditions:
        Returns a keyword-only ``run_deliver`` matching the code-v2 team public
        signature. Each call builds a fresh ``DeliverGitOps`` from the *current*
        attributes on ``git_ns``/``output_ns`` (so monkeypatches on those
        modules, applied after bind, still apply) and delegates entirely to
        ``run_deliver_impl``.
    """

    def run_deliver(
        *,
        task_id: str,
        repo_path: Path,
        files: Dict[str, str],
        summary: str,
        task_title: str = "",
        tool_agents: Optional[Dict[Any, Any]] = None,
        task_description: str = "",
        feature_branch_name: Optional[str] = None,
        merge_to_development: bool = True,
        build_verifier: Optional[Callable[[Path, str, str], Any]] = None,
        build_verify_label: str = "",
        linting_tool_agent: Any = None,
        lint_agent_type: str = "",
    ) -> Any:
        """Create feature branch, write files, commit, merge to development.

        If the Git branch management agent is present, delegate all git operations to it
        (merge to development when feature_branch_name is set, or create/write/commit/merge
        when not). When merge_to_development is False, commit the feature branch and leave
        it unmerged, ready for an external Tech Lead review instead of merging/deleting it.

        Preconditions:
            ``repo_path`` is a git repo; ``files`` maps relative paths to content.
        Postconditions:
            Returns a ``DeliverResult``. Each call builds a fresh ``DeliverGitOps`` from
            the current attributes on ``git_ns``/``output_ns`` and delegates entirely to
            ``run_deliver_impl``; git side effects run through that ``ops`` bundle.
        """
        ops = DeliverGitOps(
            abort_merge=git_ns.abort_merge,
            checkout_branch=git_ns.checkout_branch,
            commit_working_tree=git_ns.commit_working_tree,
            create_feature_branch=git_ns.create_feature_branch,
            delete_branch=git_ns.delete_branch,
            merge_branch=git_ns.merge_branch,
            write_agent_output=output_ns.write_agent_output,
        )
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
            ops=ops,
            commit_msg_template=commit_msg_template,
            models=models,
            logger=logger,
            build_verifier=build_verifier,
            build_verify_label=build_verify_label,
            linting_tool_agent=linting_tool_agent,
            lint_agent_type=lint_agent_type,
        )

    return run_deliver
