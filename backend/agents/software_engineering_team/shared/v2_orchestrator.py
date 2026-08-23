"""
Shared base for the code-v2 Development Agents (backend + frontend).

``BackendDevelopmentAgent`` and ``FrontendDevelopmentAgent`` share their
constructor, their repo-briefing read (including the incremental
:class:`~software_engineering_team.shared.repo_context_cache.RepoContextCache`
fast path), their tooling detection, their tool-runner construction, their
planning + feature-branch setup (``_run_planning_and_branch_setup``), their
per-microtask-review-gated execution phase (``_run_execution_phase``), their
post-execution bookkeeping (``_record_execution_bookkeeping``), their
documentation phase (``_run_documentation_phase``), their deliver + final
status/logging (``_run_deliver_and_finalize``), and the full ``run_workflow``
sequencing that calls all of the above in order
(``_run_development_workflow``) verbatim; only the per-team ``PROFILE``
(repo extension/exclude sets, briefing budget, and tooling-detection
callable — see
:class:`~software_engineering_team.shared.stack_profile.StackProfile`),
tool-agent roster, and a handful of team-specific classes/callables/strings
differ. This base holds the shared members; each team subclasses it, supplies
the divergent parts via class attributes (``PROFILE``, ``_TEAM_LABEL``,
``_DELIVER_IN_PROGRESS_STATUS``), and exposes a thin ``run_workflow`` that
forwards its own module-level names into ``_run_development_workflow`` —
mirroring how ``BaseTeamLead._run_setup_and_delegate`` already does this one
level up for the team-lead classes. The job-update closure each
``run_workflow`` uses comes from the shared ``team_lead_base.make_job_updater``
rather than from this base, since ``BaseTeamLead`` needs the identical closure
for its own Setup phase.
"""

from __future__ import annotations

import logging
import time
from abc import abstractmethod
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Optional, Tuple

from llm_service import LLMClient
from shared.repo_context import read_repo_code_budgeted
from software_engineering_team.shared.repo_context_cache import RepoContextCache
from software_engineering_team.shared.stack_profile import StackProfile
from software_engineering_team.shared.task_utils import merge_extra_requirements
from software_engineering_team.shared.team_lead_base import make_job_updater
from software_engineering_team.shared.tool_agent_runners import build_tool_runners
from software_engineering_team.shared.v2_models import MicrotaskReviewFailedError, Phase
from software_engineering_team.shared.v2_team_config import V2TeamConfig


class BaseV2DevelopmentAgent:
    """Shared base for the code-v2 Development Agents.

    Subclasses set the class-level ``PROFILE`` (a
    :class:`~software_engineering_team.shared.stack_profile.StackProfile`
    instance carrying the repo extension/exclude sets, briefing budget, and
    tooling-detection callable that drive this base's shared
    ``_read_repo_code``/``_detect_tooling``) plus their tool-agent roster and
    class attributes ``_TEAM_LABEL`` / ``_DELIVER_IN_PROGRESS_STATUS``; their
    public ``run_workflow`` is a thin wrapper that forwards its own
    module-level names (tool-agent builder, planning/execution/deliver
    functions, review classes, git-branch tool-agent kind) into
    ``_run_development_workflow``.

    Invariants: this base's own instance state is limited to ``llm`` and
    ``_repo_context_cache``, so ``BaseV2DevelopmentAgent`` itself (and a
    subclass that adds no instance state of its own, e.g.
    ``BackendDevelopmentAgent``/``FrontendDevelopmentAgent``) built via
    ``__new__`` and given those two attributes behaves identically to a
    constructed one. A subclass that extends instance state (e.g.
    :class:`ConfigDrivenV2DevelopmentAgent`'s ``self.config``) documents its
    own, wider construction contract instead — this invariant does not carry
    over to it automatically.
    """

    PROFILE: StackProfile

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client
        # Optional incremental repo-context cache threaded in by the team lead so
        # the per-task briefing re-reads only changed files instead of re-walking
        # the whole repo. None (the direct-construction/test path) falls back to
        # the fresh-walk ``_read_repo_code``.
        self._repo_context_cache: Optional[RepoContextCache] = None

    def _build_tool_runners(self, tool_agents: Dict[Any, Any]) -> Dict[Any, Callable[..., Any]]:
        """Build run callables from tool agent instances (for the Execution phase)."""
        return build_tool_runners(tool_agents)

    @staticmethod
    def _assemble_tool_agents(*entries: Tuple[Any, Any]) -> Dict[Any, Any]:
        """Assemble a tool-agent roster from (kind, instance) pairs.

        Preconditions: each entry is a ``(kind, agent)`` pair; kinds are hashable;
          agent instances are already constructed (deferred imports happen in the
          caller). Duplicate kinds are last-wins (same as ``dict(entries)``).
        Postconditions: returns a ``Dict`` mapping each kind to its instance;
          does not import or construct agents itself.
        """
        return dict(entries)

    @staticmethod
    def _build_progress_callback(
        update_job: Callable[..., None], *, review_label: str = "Reviewing code"
    ) -> Callable[..., None]:
        """Build the per-microtask progress callback handed to the execution loop.

        Extracted from ``run_workflow`` so the phase-label mapping + progress
        math are unit-isolated from the workflow body and the closure no longer
        buries ~30 lines inside ``run_workflow``.

        Preconditions: ``update_job`` is the run_workflow job-update callable
          (forwards kwargs to the job updater; the run_workflow closure swallows
          its failures).
        Postconditions: returns a callback
          ``(current_index, done, total, title, microtask_phase, phase_detail)
          -> None`` that maps the microtask phase to a human label and reports
          progress (15..75%% of the job) via ``update_job``; never raises into
          the execution loop. The ``"review"`` phase label is ``review_label``
          (backend and frontend differ here — ``"Reviewing code"`` vs.
          ``"Reviewing"`` — so callers parameterize it rather than losing it).
        """
        phase_labels = {
            "coding": "Writing code",
            "code_review": "Code review",
            "qa_testing": "QA testing",
            "security_testing": "Security testing",
            "qa_security_testing": "QA + Security testing",
            "documentation": "Documentation",
            "review": review_label,
            "problem_solving": "Fixing issues",
            "completed": "Completed",
        }

        def _progress_cb(
            current_index: int,
            done: int,
            total: int,
            title: str,
            microtask_phase: str = "coding",
            phase_detail: str = "",
        ) -> None:
            phase_label = phase_labels.get(
                microtask_phase, microtask_phase.replace("_", " ").title()
            )
            status = f"{phase_label}: {title} ({current_index}/{total})"
            if phase_detail:
                status = f"{status} — {phase_detail}"
            update_job(
                current_phase="execution",
                current_microtask=title,
                current_microtask_phase=microtask_phase,
                phase_detail=phase_detail,
                current_microtask_index=current_index,
                microtasks_completed=done,
                microtasks_total=total,
                progress=min(15 + int(done / max(total, 1) * 60), 75),
                status_text=status,
            )

        return _progress_cb

    @staticmethod
    def _run_preflight(
        *,
        task_id: str,
        repo_path: Path,
        feature_branch_name: Optional[str],
        detect_tooling: Callable[[Path], Tuple[bool, bool]],
        checkout_branch: Callable[[Path, str], Tuple[bool, str]],
        configure_quality_tooling: Callable[[Path], Any],
        update_job: Callable[..., None],
        logger: logging.Logger,
        emit_branch_ready_progress: bool = False,
    ) -> Optional[str]:
        """Check out the feature branch (if any) and verify lint/test tooling.

        Extracted from ``run_workflow`` so the branch-checkout + tooling-dispatch +
        missing-tooling failure-result block is defined once; ``detect_tooling``
        stays genuinely team-specific and is passed in rather than shared.

        Preconditions: ``repo_path`` is an existing directory. ``checkout_branch``
          and ``configure_quality_tooling`` are the caller module's own
          (monkeypatch-patchable) names, not imported fresh here, so tests that
          patch a team's orchestrator module keep working unchanged. Both are
          assumed not to raise; ``_run_preflight`` does not wrap them in
          ``try``/``except``, so a raising callable is the caller's to handle.
        Postconditions: returns ``None`` when checkout (if any) succeeded and
          both lint and test tooling are detected; otherwise returns the
          failure-reason string the caller should set on its result and return
          early with (already logged via ``logger.error``). Never raises on its
          own; propagates any exception raised by the injected
          ``checkout_branch`` or ``configure_quality_tooling`` callables.
          ``emit_branch_ready_progress`` controls whether a "Branch ... ready"
          progress update fires after a successful checkout — backend emits it,
          frontend does not, and this preserves that one real behavioral
          difference between the two teams explicitly rather than losing it.
        """
        if feature_branch_name:
            ok, checkout_msg = checkout_branch(repo_path, feature_branch_name)
            if not ok:
                failure_reason = f"Feature branch checkout failed: {checkout_msg}"
                logger.error("[%s] %s", task_id, failure_reason)
                return failure_reason
            logger.info("[%s] Reusing existing feature branch: %s", task_id, feature_branch_name)
            configure_quality_tooling(repo_path)
            if emit_branch_ready_progress:
                update_job(
                    current_phase="planning",
                    progress=4,
                    status_text=f"Branch {feature_branch_name} ready",
                )

        # ── Pre-flight: verify linting & testing are configured ───────
        # Runs after the feature-branch checkout so it validates the branch that
        # will actually be edited, not whatever branch setup last left checked out.
        has_lint, has_test = detect_tooling(repo_path)
        if not has_lint or not has_test:
            missing = []
            if not has_lint:
                missing.append("linting")
            if not has_test:
                missing.append("testing")
            logger.error(
                "[%s] Pre-flight check failed: %s not configured at %s",
                task_id,
                " and ".join(missing),
                repo_path,
            )
            return (
                f"Pre-flight check failed: {' and '.join(missing)} not configured. "
                "The build process requires linting and testing to be set up before coding tasks begin."
            )
        logger.info("[%s] Pre-flight check passed: linting and testing configured", task_id)
        return None

    @staticmethod
    def _run_planning_and_branch_setup(
        *,
        task_id: str,
        task: Any,
        repo_path: Path,
        architecture: Any,
        existing_code: str,
        tool_agents: Dict[Any, Any],
        git_agent: Any,
        feature_branch_name: Optional[str],
        llm: LLMClient,
        run_planning: Callable[..., Any],
        update_job: Callable[..., None],
        logger: logging.Logger,
    ) -> Tuple[Optional[Any], Optional[str], Optional[str]]:
        """Run planning, then create a feature branch if one isn't already set.

        Extracted from ``run_workflow`` so the planning-invocation +
        job-status-update + feature-branch-creation sequence is defined once;
        ``run_planning`` is injected (the caller's own module-level name) so
        tests that monkeypatch a team's orchestrator module keep working
        unchanged, matching ``_run_preflight``'s pattern. ``git_agent`` is
        pre-resolved by the caller (``tool_agents.get(ToolAgentKind.GIT_BRANCH_MANAGEMENT)``)
        since ``ToolAgentKind`` is a distinct enum per team and the shared
        base cannot reference it.

        Preconditions: ``repo_path`` is an existing directory containing the
          branch to plan/edit. ``run_planning`` follows the team
          ``run_planning`` signature (``llm``, ``task``, ``repo_path``,
          ``architecture``, ``existing_code``, ``tool_agents`` keywords) and
          is assumed not to raise by contract, but any exception it does
          raise is caught and reported via the returned failure reason.
        Postconditions: returns ``(planning_result, feature_branch_name,
          failure_reason)``. On planning failure, ``planning_result`` is
          ``None`` and ``failure_reason`` is set (already logged via
          ``logger.error``); the caller sets ``result.failure_reason`` and
          returns early exactly as ``run_workflow`` does today. On success,
          ``failure_reason`` is ``None`` and ``feature_branch_name`` is the
          incoming name unchanged if already set, else the newly created
          branch name if branch creation succeeded, else the incoming
          (falsy) name. Never raises on its own; branch-creation failures are
          swallowed and logged, never surfaced as a failure_reason, since
          ``deliver`` can still create the branch later.
        """
        logger.info("[%s] Next step -> Starting Phase: Planning", task_id)
        update_job(
            current_phase="planning",
            progress=5,
            status_text="Analyzing task and creating implementation plan...",
        )

        try:
            planning_result = run_planning(
                llm=llm,
                task=task,
                repo_path=repo_path,
                architecture=architecture,
                existing_code=existing_code,
                tool_agents=tool_agents,
            )
        except Exception as exc:
            failure_reason = f"Planning failed: {exc}"
            logger.error("[%s] %s", task_id, failure_reason)
            return None, feature_branch_name, failure_reason

        if planning_result is None:
            failure_reason = "Planning returned no result"
            logger.error("[%s] %s", task_id, failure_reason)
            return None, feature_branch_name, failure_reason

        total_microtasks = len(planning_result.microtasks)
        update_job(
            current_phase="planning",
            progress=10,
            microtasks_total=total_microtasks,
            microtasks_completed=0,
            status_text=f"Plan created with {total_microtasks} microtask(s)",
        )

        # ── Create feature branch (Git agent) before first execution ───
        create_feature_branch_fn = (
            getattr(git_agent, "create_feature_branch", None) if git_agent is not None else None
        )
        if not feature_branch_name and callable(create_feature_branch_fn):
            update_job(
                current_phase="planning", progress=12, status_text="Creating feature branch..."
            )
            try:
                ok, branch_name = create_feature_branch_fn(repo_path, task_id, task.title or "")
                if ok and branch_name:
                    feature_branch_name = branch_name
                    logger.info("[%s] Created feature branch: %s", task_id, feature_branch_name)
                    update_job(
                        current_phase="planning",
                        progress=14,
                        status_text=f"Branch {feature_branch_name} ready",
                    )
                else:
                    logger.warning(
                        "[%s] Git agent create_feature_branch failed, deliver will create branch",
                        task_id,
                    )
            except Exception as exc:
                logger.warning("[%s] Git agent create_feature_branch raised: %s", task_id, exc)

        return planning_result, feature_branch_name, None

    @staticmethod
    def _run_execution_phase(
        *,
        task_id: str,
        task: Any,
        planning_result: Any,
        repo_path: Path,
        architecture: Any,
        spec_content: str,
        existing_code: str,
        tool_runners: Dict[Any, Callable[..., Any]],
        progress_callback: Callable[..., None],
        review_deps: Any,
        review_config: Any,
        llm: LLMClient,
        result: Any,
        run_execution_with_review_gates: Callable[..., Any],
        update_job: Callable[..., None],
        logger: logging.Logger,
        status_text: str,
    ) -> Optional[Dict[str, str]]:
        """Run the review-gated execution loop and return the produced files.

        Extracted from ``run_workflow`` so the execution-phase status update +
        review-gated execution invocation + exception handling + empty-files
        check is defined once. ``run_execution_with_review_gates`` is injected
        (the caller's own late-imported module-level name) so tests that
        monkeypatch a team's orchestrator module (``orch.run_execution_with_review_gates``)
        keep working unchanged, matching ``_run_deliver_and_finalize``'s pattern.
        ``progress_callback`` is built by the caller via
        ``self._build_progress_callback(update_job, review_label=...)`` before
        this is called, since that factory is already its own extracted helper
        and the one real per-team difference (``review_label``) is resolved
        there rather than duplicated here. ``review_deps`` (a
        ``ReviewDependencies``, itself defined once in ``shared.phases.execution``
        and re-exported verbatim by both teams' ``phases/_profile.py``) and
        ``review_config`` (the team's already-resolved ``MicrotaskReviewConfig``,
        i.e. ``review_config or MicrotaskReviewConfig()`` -- left to the caller
        since that class differs per team) are passed in fully constructed.
        ``MicrotaskReviewFailedError`` is imported directly from
        ``shared.v2_models`` rather than injected: each team's ``models.py``
        re-exports the identical class object, so catching the shared import
        here still catches whatever ``run_execution_with_review_gates`` raises.
        ``status_text`` is parameterized because backend and frontend differ
        (``"Starting code implementation"`` vs. ``"Starting code
        implementation..."``); the "Next step -> Starting Phase: Execution"
        log line is unified across both teams (backend previously logged
        "...Phase 2: Execution") since no test asserts on the exact log text.

        Preconditions: ``result`` exposes writable ``current_phase``,
          ``execution_result``, and ``failure_reason`` attributes.
          ``run_execution_with_review_gates`` follows the team
          ``run_execution_with_review_gates`` keyword signature.
        Postconditions: ``result.current_phase`` is ``Phase.EXECUTION``. On
          success, ``result.execution_result`` is set and this returns the
          produced (non-empty) ``files`` dict. On a
          ``MicrotaskReviewFailedError``, a generic exception, or an empty
          ``files`` result, sets ``result.failure_reason`` (logged via
          ``logger.error`` for the first two; the empty-files case matches
          prior behavior and is not logged) and returns ``None``; the
          caller's contract is to ``return result`` immediately in that case,
          exactly as ``run_workflow`` does today. Never raises on its own.
        """
        logger.info("[%s] Next step -> Starting Phase: Execution", task_id)
        result.current_phase = Phase.EXECUTION
        update_job(
            current_phase="execution",
            current_microtask="",
            progress=15,
            status_text=status_text,
        )

        try:  # pragma: no cover  # integration-only: runs review-gated execution loop against live LLM + build/lint/test tooling
            exec_result = run_execution_with_review_gates(
                llm=llm,
                task=task,
                planning_result=planning_result,
                repo_path=repo_path,
                architecture=architecture,
                spec_content=spec_content,
                existing_code=existing_code,
                tool_runners=tool_runners,
                progress_callback=progress_callback,
                review_config=review_config,
                review_deps=review_deps,
            )
            result.execution_result = exec_result
        except MicrotaskReviewFailedError as err:
            result.failure_reason = (
                f"Microtask {err.microtask.id} failed review: {err.review_result.summary}"
            )
            logger.error("[%s] %s", task_id, result.failure_reason)
            return None
        except Exception as exc:
            result.failure_reason = f"Execution failed: {exc}"
            logger.error("[%s] %s", task_id, result.failure_reason)
            return None

        current_files = exec_result.files
        if not current_files:
            result.failure_reason = "Execution produced no files."
            return None

        return current_files

    @staticmethod
    def _record_execution_bookkeeping(
        *,
        task_id: str,
        result: Any,
        exec_result: Any,
        repo_path: Path,
        feature_branch_name: Optional[str],
        git_agent: Any,
        logger: logging.Logger,
    ) -> Tuple[int, int]:
        """Count execution outcomes, set ``iterations_used``, and commit mid-workflow.

        Extracted from ``run_workflow`` so completed/failed counting,
        ``result.iterations_used``, and the optional
        ``commit_current_changes`` call are defined once. Status values are
        compared as strings (``"completed"`` / ``"review_failed"``) so this
        base never imports a team-specific ``MicrotaskStatus`` enum; both
        teams' enums use those values today. ``git_agent`` is pre-resolved by
        the caller since ``ToolAgentKind`` differs per team.

        Preconditions: ``exec_result.microtasks`` is iterable; each item has a
          ``status`` comparable to ``"completed"`` / ``"review_failed"``.
          ``result`` has a writable ``iterations_used`` attribute.
        Postconditions: ``result.iterations_used`` equals the completed count.
          Returns ``(completed_count, failed_count)``. When
          ``feature_branch_name`` is truthy and ``git_agent`` exposes
          ``commit_current_changes``, that method is invoked once with
          ``repo_path`` and a ``feat: {N} microtasks completed`` message;
          exceptions from the commit are logged as warnings and never raised.
          Never raises on its own.
        """
        completed_count = sum(1 for mt in exec_result.microtasks if mt.status == "completed")
        failed_count = sum(1 for mt in exec_result.microtasks if mt.status == "review_failed")
        result.iterations_used = completed_count

        if (
            feature_branch_name
            and git_agent is not None
            and hasattr(git_agent, "commit_current_changes")
        ):
            try:
                git_agent.commit_current_changes(
                    repo_path, f"feat: {completed_count} microtasks completed"
                )
            except Exception as exc:
                logger.warning("[%s] Git agent commit_current_changes raised: %s", task_id, exc)

        return completed_count, failed_count

    @staticmethod
    def _run_documentation_phase(
        *,
        task_id: str,
        task: Any,
        repo_path: Path,
        llm: LLMClient,
        exec_result: Any,
        planning_result: Any,
        tool_agents: Dict[Any, Any],
        result: Any,
        current_files: Dict[str, str],
        run_documentation_phase: Callable[..., Any],
        update_job: Callable[..., None],
        logger: logging.Logger,
        status_text: str,
    ) -> Dict[str, str]:
        """Run the documentation phase and merge any new files into ``current_files``.

        Extracted from ``run_workflow`` so the documentation status update +
        phase invocation + file-merge + exception-swallow block is defined once;
        ``run_documentation_phase`` is injected (the caller's late-imported
        module-level name) so tests that monkeypatch
        ``phases.documentation.run_documentation_phase`` keep working.
        ``status_text`` is parameterized because backend and frontend differ
        (``"Generating documentation and API specs"`` vs.
        ``"Generating documentation and API docs..."``).

        Preconditions: ``status_text`` is the team-specific job status string.
          ``result`` exposes ``current_phase``, ``documentation_result``, and
          ``final_files`` attributes. ``current_files`` is the mutable
          post-execution file map.
        Postconditions: ``result.current_phase`` is ``Phase.DOCUMENTATION``.
          On success, ``result.documentation_result`` is set and any
          ``doc_result.files`` are merged into ``current_files`` /
          ``result.final_files``. On failure, logs a warning and leaves
          ``documentation_result`` unset. Never raises; returns the (possibly
          updated) ``current_files`` dict.
        """
        logger.info("[%s] Next step -> Starting Phase: Documentation", task_id)
        result.current_phase = Phase.DOCUMENTATION
        update_job(
            current_phase="documentation",
            progress=80,
            status_text=status_text,
        )

        try:
            doc_result = run_documentation_phase(
                llm=llm,
                task=task,
                repo_path=repo_path,
                execution_result=exec_result,
                planning_result=planning_result,
                tool_agents=tool_agents,
            )
            result.documentation_result = doc_result
            if doc_result.files:
                current_files.update(doc_result.files)
                result.final_files = current_files
            logger.info("[%s] Documentation phase complete: %s", task_id, doc_result.summary)
        except Exception as exc:
            logger.warning(
                "[%s] Documentation phase failed: %s. Next step -> Continuing to Deliver phase",
                task_id,
                exc,
            )

        return current_files

    @staticmethod
    def _run_deliver_and_finalize(
        *,
        task_id: str,
        repo_path: Path,
        current_files: Any,
        exec_summary: str,
        task_title: str,
        task_description: str,
        tool_agents: Dict[Any, Any],
        feature_branch_name: Optional[str],
        merge_to_development: bool,
        failed_count: int,
        completed_count: int,
        start_time: float,
        result: Any,
        run_deliver: Callable[..., Any],
        update_job: Callable[..., None],
        logger: logging.Logger,
        team_label: str,
        deliver_in_progress_status: str,
        build_verifier: Optional[Callable[..., Tuple[bool, str]]] = None,
        build_verify_label: str = "",
        linting_tool_agent: Any = None,
        lint_agent_type: str = "",
    ) -> Optional[str]:
        """Run deliver, then emit final job status and workflow timing log.

        Extracted from ``run_workflow`` so the deliver-invocation + result
        mutation + final status/logging sequence is defined once. ``run_deliver``
        is injected (the caller's own module-level name) so tests that
        monkeypatch a team's orchestrator module keep working unchanged.
        ``team_label`` and ``deliver_in_progress_status`` are passed explicitly
        from each subclass's class attributes so the one real per-team string
        differences are preserved without hardcoding.

        Preconditions: ``repo_path`` is an existing directory.
          ``run_deliver`` follows the team ``run_deliver`` keyword signature.
          ``result`` is a mutable workflow-result object with ``current_phase``,
          ``deliver_result``, ``success``, ``summary``, ``needs_followup``, and
          ``failure_reason`` attributes. ``start_time`` was captured via
          ``time.monotonic()`` before this helper runs. ``build_verifier``/
          ``linting_tool_agent`` (with their ``build_verify_label``/
          ``lint_agent_type`` labels), when provided, are forwarded into
          ``run_deliver`` as a compensating pre-merge build/lint gate for the
          ``merge_to_development=True`` case; ``None`` (the default) skips the
          gate, matching the swarm-orchestrated path's existing behavior.
        Postconditions: on a non-raising ``run_deliver`` result (including soft
          failure where ``merged``/``branch_ready`` is false), mutates ``result``
          (sets ``deliver_result``, ``success``, ``summary``, and optionally
          ``needs_followup``), emits the final job update, logs workflow timing,
          and returns ``None``. Soft deliver failure leaves ``result.success``
          false and finalizes with the ``"{team_label} task completed with
          issues"`` status. On deliver exception, sets ``result.failure_reason``,
          logs the error, and returns the failure-reason string; callers may
          ignore that return value and always ``return result``, since the
          failure is already on ``result.failure_reason``. In-progress status
          text is ``deliver_in_progress_status``; final status is
          ``"{team_label} task complete"`` or
          ``"{team_label} task completed with issues"``. Never raises on its own.
        """
        logger.info("[%s] Next step -> Starting Phase: Deliver", task_id)
        result.current_phase = Phase.DELIVER
        update_job(
            current_phase="deliver",
            progress=90,
            status_text=deliver_in_progress_status,
        )

        try:
            deliver_result = run_deliver(
                task_id=task_id,
                repo_path=repo_path,
                files=current_files,
                summary=exec_summary,
                task_title=task_title,
                tool_agents=tool_agents,
                task_description=task_description,
                feature_branch_name=feature_branch_name,
                merge_to_development=merge_to_development,
                build_verifier=build_verifier,
                build_verify_label=build_verify_label,
                linting_tool_agent=linting_tool_agent,
                lint_agent_type=lint_agent_type,
            )
            result.deliver_result = deliver_result
            delivered = (
                deliver_result.merged if merge_to_development else deliver_result.branch_ready
            )
            result.success = delivered and failed_count == 0
            result.summary = f"{exec_summary} {deliver_result.summary}"
            if failed_count > 0:
                result.needs_followup = True
                result.summary += f" ({failed_count} microtask(s) failed review)"
        except Exception as exc:
            failure_reason = f"Deliver failed: {exc}"
            result.failure_reason = failure_reason
            logger.error("[%s] %s", task_id, failure_reason)
            return failure_reason

        elapsed = time.monotonic() - start_time
        final_status = (
            f"{team_label} task complete"
            if result.success
            else f"{team_label} task completed with issues"
        )
        update_job(
            current_phase="deliver",
            progress=100 if result.success else 95,
            status_text=final_status,
        )
        logger.info(
            "[%s] WORKFLOW %s in %.1fs (%d microtasks completed, %d failed review)",
            task_id,
            "SUCCEEDED" if result.success else "PARTIAL",
            elapsed,
            completed_count,
            failed_count,
        )
        return None

    def _run_development_workflow(
        self,
        *,
        repo_path: Path,
        task: Any,
        architecture: Any = None,
        spec_content: str = "",
        qa_agent: Any = None,
        security_agent: Any = None,
        code_review_agent: Any = None,
        build_verifier: Optional[Callable[..., Tuple[bool, str]]] = None,
        linting_tool_agent: Any = None,
        job_updater: Optional[Callable[..., None]] = None,
        review_config: Any = None,
        merge_to_development: bool = True,
        repo_context_cache: Optional[RepoContextCache] = None,
        result_cls: Callable[..., Any],
        team_label: str,
        deliver_in_progress_status: str,
        logger: logging.Logger,
        checkout_branch: Callable[[Path, str], Tuple[bool, str]],
        configure_quality_tooling: Callable[[Path], Any],
        detect_tooling: Callable[[Path], Tuple[bool, bool]],
        emit_branch_ready_progress: bool,
        build_tool_agents: Callable[[LLMClient], Dict[Any, Any]],
        git_branch_management_kind: Any,
        run_planning: Callable[..., Any],
        review_label: str,
        execution_status_text: str,
        review_deps_cls: Callable[..., Any],
        review_config_cls: Callable[..., Any],
        run_execution_with_review_gates: Callable[..., Any],
        documentation_status_text: str,
        run_documentation_phase: Callable[..., Any],
        run_deliver: Callable[..., Any],
    ) -> Any:
        """Run the full 5-phase lifecycle (Pre-flight -> Planning -> Execution ->
        Documentation -> Deliver) shared verbatim by every code-v2 Development Agent.

        Extracted from each team's ``run_workflow`` so the phase-sequencing glue
        that calls ``_run_preflight`` / ``_run_planning_and_branch_setup`` /
        ``_build_progress_callback`` / ``_run_execution_phase`` /
        ``_record_execution_bookkeeping`` / ``_run_documentation_phase`` /
        ``_run_deliver_and_finalize`` in order is defined once. The
        ``review_deps`` / ``review_config`` construction stays here (rather
        than inside ``_run_execution_phase``) since ``ReviewDependencies`` and
        ``MicrotaskReviewConfig`` are still genuinely per-team classes; the
        execution try/except itself — including the
        ``MicrotaskReviewFailedError`` catch, now a single shared exception
        imported directly in ``_run_execution_phase`` rather than injected —
        is not duplicated here. Every team-specific piece — which module-level
        function/class to call, the one real behavioral divergence
        (``emit_branch_ready_progress``), and a couple of status strings — is
        taken as a parameter rather than imported here, so callers pass their
        *own* module-level names (not copies) and existing tests that
        monkeypatch a team's orchestrator module (e.g. ``orch.checkout_branch``,
        ``orch._build_tool_agents``, ``orch.run_planning``,
        ``orch.run_execution_with_review_gates``, ``orch.run_deliver``) keep
        working unchanged, matching the pattern ``_run_preflight`` and
        ``_run_planning_and_branch_setup`` already use. ``logger`` is likewise
        injected so log records keep each team's own module name rather than
        this shared module's. The job-update closure itself is built via
        ``team_lead_base.make_job_updater`` — the same factory
        ``BaseTeamLead._run_setup_and_delegate`` uses — so this is now the
        second, not third, place that closure is defined.

        Preconditions: ``repo_path`` is an existing directory. ``result_cls``
          constructs a workflow-result object accepting a ``task_id`` keyword
          and exposing the attributes every helper above already requires
          (``current_phase``, ``failure_reason``, ``planning_result``,
          ``execution_result``, ``iterations_used``, ``final_files``,
          ``documentation_result``, ``deliver_result``, ``success``,
          ``summary``, ``needs_followup``). ``review_deps_cls`` accepts the
          same keywords as ``ReviewDependencies`` in either team's
          ``phases.execution`` module. ``review_config_cls`` is callable with
          no arguments when ``review_config`` is falsy.
        Postconditions: returns a ``result_cls`` instance. On a pre-flight or
          planning failure, returns early with ``result.failure_reason`` set
          locally and later phases not run. On an execution failure
          (including an empty ``exec_result.files``), ``_run_execution_phase``
          has already set ``result.failure_reason`` before returning
          ``None``; this method returns ``result`` immediately in that case
          with later phases not run — identical short-circuiting to the
          former per-team ``run_workflow`` bodies. On success, threads through
          bookkeeping, documentation, and deliver exactly as
          ``_run_deliver_and_finalize`` documents, and returns ``result``.
          Propagates exceptions from ``self._stack_profile()`` and from the
          injected callables when they raise outside their documented
          failure paths; otherwise does not raise on its own.
        """
        self._repo_context_cache = repo_context_cache
        task_id = task.id
        start_time = time.monotonic()
        result = result_cls(task_id=task_id)

        _update_job = make_job_updater(job_updater, task_id, logger)

        logger.info(
            "[%s] WORKFLOW START: %s Development Agent (per-microtask review gates)",
            task_id,
            team_label,
        )

        # ── Check out the review feature branch, then ensure tooling ───
        # Setup commits lint/test scaffolding to ``development``, but a handoff
        # feature branch created before setup does not inherit it. Configure the
        # tooling on the branch we will actually edit so the pre-flight check and
        # later quality gates see the config (idempotent when already present).
        feature_branch_name = (task.feature_branch_name or "").strip() or None
        preflight_failure = self._run_preflight(
            task_id=task_id,
            repo_path=repo_path,
            feature_branch_name=feature_branch_name,
            detect_tooling=detect_tooling,
            checkout_branch=checkout_branch,
            configure_quality_tooling=configure_quality_tooling,
            update_job=_update_job,
            logger=logger,
            emit_branch_ready_progress=emit_branch_ready_progress,
        )
        if preflight_failure is not None:
            result.failure_reason = preflight_failure
            return result

        existing_code = self._read_existing_code(repo_path)
        tool_agents = build_tool_agents(self.llm)
        tool_runners = self._build_tool_runners(tool_agents)
        git_agent = tool_agents.get(git_branch_management_kind)

        result.current_phase = Phase.PLANNING
        planning_result, feature_branch_name, failure_reason = self._run_planning_and_branch_setup(
            task_id=task_id,
            task=task,
            repo_path=repo_path,
            architecture=architecture,
            existing_code=existing_code,
            tool_agents=tool_agents,
            git_agent=git_agent,
            feature_branch_name=feature_branch_name,
            llm=self.llm,
            run_planning=run_planning,
            update_job=_update_job,
            logger=logger,
        )
        if failure_reason is not None:
            result.failure_reason = failure_reason
            return result
        result.planning_result = planning_result

        # ── Execution with per-microtask review gates ──────────────────
        progress_callback = self._build_progress_callback(_update_job, review_label=review_label)

        review_deps = review_deps_cls(
            build_verifier=build_verifier,
            qa_agent=qa_agent,
            security_agent=security_agent,
            code_review_agent=code_review_agent,
            linting_tool_agent=linting_tool_agent,
            tool_agents=tool_agents,
        )

        config = review_config or review_config_cls()

        current_files = self._run_execution_phase(
            task_id=task_id,
            task=task,
            planning_result=planning_result,
            repo_path=repo_path,
            architecture=architecture,
            spec_content=spec_content,
            existing_code=existing_code,
            tool_runners=tool_runners,
            progress_callback=progress_callback,
            review_deps=review_deps,
            review_config=config,
            llm=self.llm,
            result=result,
            run_execution_with_review_gates=run_execution_with_review_gates,
            update_job=_update_job,
            logger=logger,
            status_text=execution_status_text,
        )
        if current_files is None:
            return result
        exec_result = result.execution_result

        completed_count, failed_count = self._record_execution_bookkeeping(
            task_id=task_id,
            result=result,
            exec_result=exec_result,
            repo_path=repo_path,
            feature_branch_name=feature_branch_name,
            git_agent=git_agent,
            logger=logger,
        )

        result.final_files = current_files

        # ── Documentation ────────────────────────────────────────────
        current_files = self._run_documentation_phase(
            task_id=task_id,
            task=task,
            repo_path=repo_path,
            llm=self.llm,
            exec_result=exec_result,
            planning_result=planning_result,
            tool_agents=tool_agents,
            result=result,
            current_files=current_files,
            run_documentation_phase=run_documentation_phase,
            update_job=_update_job,
            logger=logger,
            status_text=documentation_status_text,
        )

        # ── Deliver ─────────────────────────────────────────────────
        # ``_stack_profile()`` resolves the concrete class-level ``PROFILE``
        # (Backend/FrontendDevelopmentAgent) or, for
        # ``ConfigDrivenV2DevelopmentAgent``, ``self.config.stack_profile``;
        # ``None`` only on a bare ``BaseV2DevelopmentAgent`` (e.g. unit
        # tests), where the labels below default to "" and go unused since
        # such callers also don't pass build_verifier/linting_tool_agent.
        stack_profile = self._stack_profile()
        self._run_deliver_and_finalize(
            task_id=task_id,
            repo_path=repo_path,
            current_files=current_files,
            exec_summary=exec_result.summary,
            task_title=task.title or "",
            task_description=task.description or "",
            tool_agents=tool_agents,
            feature_branch_name=feature_branch_name,
            merge_to_development=merge_to_development,
            failed_count=failed_count,
            completed_count=completed_count,
            start_time=start_time,
            result=result,
            run_deliver=run_deliver,
            update_job=_update_job,
            logger=logger,
            team_label=team_label,
            deliver_in_progress_status=deliver_in_progress_status,
            build_verifier=build_verifier,
            build_verify_label=stack_profile.build_verify_label if stack_profile else "",
            linting_tool_agent=linting_tool_agent,
            lint_agent_type=stack_profile.name if stack_profile else "",
        )
        return result

    @classmethod
    def _read_repo_code(cls, repo_path: Path, max_chars: Optional[int] = None) -> str:
        """Read the repo briefing using this stack's ``PROFILE``.

        Delegates to the shared budgeted scanner so every per-domain reader shares
        one implementation; ``PROFILE.repo_extensions``/``repo_exclude_dirs`` are
        the contract, and ``PROFILE.repo_max_chars`` is the default budget when
        ``max_chars`` is not supplied.

        A ``classmethod`` (not instance method) so it stays callable unbound —
        ``BackendDevelopmentAgent._read_repo_code(repo_path)`` — exactly as
        subclasses and tests have always called it, while still resolving the
        calling subclass's ``PROFILE``.
        """
        return read_repo_code_budgeted(
            repo_path,
            extensions=cls.PROFILE.repo_extensions,
            exclude_dirs=cls.PROFILE.repo_exclude_dirs,
            max_chars=max_chars if max_chars is not None else cls.PROFILE.repo_max_chars,
        )

    @classmethod
    def _detect_tooling(cls, repo_path: Path) -> Tuple[bool, bool]:
        """Return ``(has_lint, has_test)`` via this stack's ``PROFILE.detect_tooling``.

        A ``classmethod`` so ``FrontendDevelopmentAgent._detect_tooling(repo_path)``
        keeps working unbound, resolving the calling subclass's ``PROFILE``.
        """
        return cls.PROFILE.detect_tooling(repo_path)

    def _stack_profile(self) -> Optional[StackProfile]:
        """Return this instance's ``StackProfile``, if it has one.

        Preconditions: none.
        Postconditions: returns the concrete subclass's class-level
          ``PROFILE`` (e.g. ``BackendDevelopmentAgent``/
          ``FrontendDevelopmentAgent``), or ``None`` on a bare
          ``BaseV2DevelopmentAgent`` (e.g. unit tests) that sets no
          ``PROFILE``. Subclasses whose stack profile isn't a class
          attribute (e.g. :class:`ConfigDrivenV2DevelopmentAgent`, which
          resolves it from ``self.config`` instead) override this.
        """
        return getattr(self, "PROFILE", None)

    def _read_existing_code(self, repo_path: Path) -> str:
        """Return the repo briefing, consulting the incremental cache when one is threaded in.

        Preconditions: ``repo_path`` is an existing directory.
        Postconditions: returns a briefing byte-identical to
          ``_read_repo_code(repo_path)`` for the current on-disk state; when a
          cache is present it re-reads only changed eligible files. Raises
          ``AssertionError`` if the precondition is violated (caller bug).
        Invariants: with no cache the fresh walk runs each call; with a cache the
          output never differs from the fresh walk, only the amount of file I/O.

        The no-cache branch calls ``_read_repo_code(repo_path)`` with no kwargs
        deliberately: callers (and tests) monkeypatch it with a no-kwargs
        signature, and the cache carries its own char budget, so forwarding one
        here would both break that patch surface and be ignored.
        """
        assert repo_path.is_dir(), "repo_path must be an existing directory"
        if self._repo_context_cache is not None:
            return self._repo_context_cache.read(repo_path)
        return self._read_repo_code(repo_path)


class ConfigDrivenV2DevelopmentAgent(BaseV2DevelopmentAgent):
    """Generic ``BaseV2DevelopmentAgent`` driven entirely by a :class:`V2TeamConfig`.

    ``BaseV2DevelopmentAgent`` already takes nearly everything team-specific
    (tool-agent builder, planning/execution/deliver functions, review classes)
    as parameters injected by each subclass's ``run_workflow``; what still
    comes from a hand-written per-team constant is specifically the four axes
    :class:`V2TeamConfig` captures: the stack's default language and
    conventions map (today a class-level ``PROFILE`` each team sets by hand),
    its tool-agent registry (today only implicit in each team's
    ``_build_tool_agents()`` body), and its optional extra review clause
    (today a bare module constant, e.g. frontend's
    ``_ACCESSIBILITY_VERIFY_NOTE``). This class resolves all four from a
    ``V2TeamConfig`` instance instead, so a concrete team subclasses it and
    supplies only that config plus a ``_build_tool_agents`` hook.
    ``backend_code_v2_team`` and ``frontend_code_v2_team`` both subclass this
    base and supply their team-specific ``V2TeamConfig`` instance.

    Invariants: ``self.config`` is set once at construction and never
    reassigned; every property/method below is a pure read through it (or
    through the ``StackProfile`` it composes), so two instances built from the
    same config always agree. This deliberately widens
    ``BaseV2DevelopmentAgent``'s two-attribute ``__new__``-construction
    contract: a ``__new__``-constructed instance of this subclass
    specifically (bypassing ``__init__``) must also set ``self.config`` —
    the base class's ``llm``/``_repo_context_cache`` pair is necessary but
    not sufficient here, since every property/method above reads
    ``self.config``.
    """

    def __init__(self, llm_client: LLMClient, config: V2TeamConfig) -> None:
        """Construct the agent from an LLM client and a team's config.

        Preconditions: ``llm_client`` is not ``None`` (enforced by
          ``BaseV2DevelopmentAgent.__init__``); ``config`` is a ``V2TeamConfig``
          instance (not ``None``).
        Postconditions: ``self.config`` is ``config`` (the same object, not a
          copy); all ``BaseV2DevelopmentAgent.__init__`` postconditions
          (``self.llm``, ``self._repo_context_cache``) also hold.
        """
        super().__init__(llm_client)
        assert config is not None, "config is required"
        self.config = config

    @property
    def default_language(self) -> str:
        """This team's fallback language, read from ``config.stack_profile``.

        Preconditions: none beyond construction.
        Postconditions: returns ``self.config.stack_profile.default_language``
          unchanged; pure, no side effects.
        """
        return self.config.stack_profile.default_language

    def conventions_for(self, language: str) -> str:
        """Return this team's conventions text for ``language``.

        Delegates to ``StackProfile.conventions_for`` rather than duplicating
        its ``"_default"``-fallback logic, so the two can never disagree.

        Preconditions: ``language`` is a string.
        Postconditions: returns the conventions entry for ``language`` if
          present in ``config.stack_profile.conventions_by_language``, else
          the ``"_default"`` entry. Pure; no side effects.
        """
        return self.config.stack_profile.conventions_for(language)

    @property
    def tool_agent_kinds(self) -> FrozenSet[str]:
        """This team's declared ``ToolAgentKind`` registry, as plain strings.

        Preconditions: none beyond construction.
        Postconditions: returns ``self.config.tool_agent_kinds`` unchanged;
          pure, no side effects.
        """
        return self.config.tool_agent_kinds

    @property
    def extra_review_clause(self) -> str:
        """This team's optional extra code-review guidance (``""`` if none).

        Preconditions: none beyond construction.
        Postconditions: returns ``self.config.extra_review_clause`` unchanged;
          pure, no side effects.
        """
        return self.config.extra_review_clause

    def _validate_tool_agents(self, tool_agents: Dict[Any, Any]) -> None:
        """Assert a built tool-agent roster matches the config's declared registry.

        Makes ``tool_agent_kinds`` genuinely config-driven rather than inert
        stored data: a caller's ``build_tool_agents(llm)`` output (keyed by
        the team's own ``ToolAgentKind`` enum members, which are ``(str,
        Enum)`` subclasses) is checked against ``self.tool_agent_kinds``
        instead of being trusted silently.

        Preconditions: ``tool_agents`` is a mapping whose keys are ``str`` or
          ``(str, Enum)``-subclass values (so ``str(kind.value if
          hasattr(kind, "value") else kind)`` yields the plain-string form
          ``V2TeamConfig.tool_agent_kinds`` stores).
        Postconditions: returns ``None`` when the set of built kinds equals
          ``self.tool_agent_kinds`` exactly. Raises ``ValueError`` naming the
          missing and/or extra kinds otherwise. Never mutates ``tool_agents``
          or ``self.config``.
        """
        built_kinds = frozenset(
            str(kind.value if hasattr(kind, "value") else kind) for kind in tool_agents
        )
        expected_kinds = self.tool_agent_kinds
        if built_kinds == expected_kinds:
            return
        missing = expected_kinds - built_kinds
        extra = built_kinds - expected_kinds
        raise ValueError(
            "Tool-agent roster does not match the declared registry "
            f"(missing={sorted(missing)}, extra={sorted(extra)})"
        )

    def build_task_requirements(self, base_requirements: str) -> str:
        """Merge this team's extra review clause into a base requirements string.

        Delegates to the shared ``merge_extra_requirements`` helper — the
        same one ``shared.v2_review.run_coordinator_llm_review`` uses for its
        ``extra_task_requirements`` handling — rather than duplicating the
        blank-line-separator-or-verbatim merge rule, so this is a drop-in
        source for that parameter once a concrete team wires this config
        through instead of a hard-coded module constant.

        Preconditions: ``base_requirements`` is a string (may be empty).
        Postconditions: returns ``base_requirements`` unchanged when
          ``self.extra_review_clause`` is ``""``; otherwise returns the clause
          appended after a blank line when ``base_requirements`` is
          non-empty, or the clause verbatim when it is empty. Pure; no side
          effects.
        """
        return merge_extra_requirements(base_requirements, self.extra_review_clause)

    def _stack_profile(self) -> StackProfile:
        """Return this instance's ``StackProfile``, read from ``self.config``.

        Overrides the parent's ``getattr(self, "PROFILE", None)`` lookup:
        this class deliberately has no class-level ``PROFILE`` attribute, so
        that lookup would always return ``None`` here (silently emptying the
        deliver phase's ``build_verify_label``/``lint_agent_type``). This
        override is what ``_run_development_workflow``'s deliver-phase call
        to ``self._stack_profile()`` resolves to for this subclass.

        Preconditions: none beyond construction.
        Postconditions: returns ``self.config.stack_profile``; never
          ``None`` (config is always required at construction, unlike the
          base class's class-attribute fallback).
        """
        return self.config.stack_profile

    def _read_repo_code(self, repo_path: Path, max_chars: Optional[int] = None) -> str:
        """Read the repo briefing using ``self.config.stack_profile``.

        Overrides the parent's ``classmethod`` (which reads a class-level
        ``PROFILE``) as an instance method, since here the profile is
        per-instance data threaded in via ``config`` at construction, not a
        class attribute. ``self._read_existing_code``'s no-cache branch calls
        ``self._read_repo_code(repo_path)`` via normal instance dispatch, so
        it resolves to this override unchanged.

        Preconditions: same as the parent classmethod.
        Postconditions: same as the parent classmethod, but sourced from
          ``self.config.stack_profile`` instead of ``cls.PROFILE``.
        """
        profile = self._stack_profile()
        return read_repo_code_budgeted(
            repo_path,
            extensions=profile.repo_extensions,
            exclude_dirs=profile.repo_exclude_dirs,
            max_chars=max_chars if max_chars is not None else profile.repo_max_chars,
        )

    def _detect_tooling(self, repo_path: Path) -> Tuple[bool, bool]:
        """Return ``(has_lint, has_test)`` via ``self.config.stack_profile.detect_tooling``.

        Overrides the parent's ``classmethod`` as an instance method for the
        same reason as ``_read_repo_code`` above; ``self._detect_tooling`` is
        already how ``_run_development_workflow`` callers pass it through
        (e.g. ``detect_tooling=self._detect_tooling``), so this resolves
        unchanged via instance dispatch.

        Preconditions: same as the parent classmethod.
        Postconditions: same as the parent classmethod, but sourced from
          ``self.config.stack_profile`` instead of ``cls.PROFILE``.
        """
        return self._stack_profile().detect_tooling(repo_path)

    @abstractmethod
    def _build_tool_agents(self, llm: LLMClient) -> Dict[Any, Any]:
        """Build the team's tool-agent roster.

        Subclasses override this to construct their team-specific tool agents.
        The tool-agent builder is kept as a subclass hook (rather than a field
        on ``V2TeamConfig``) because each team's builder uses deferred imports
        of heavy strands/llm_service machinery that cannot be eagerly loaded
        as a frozen-dataclass field.

        Preconditions: ``llm`` is a configured ``LLMClient`` (not ``None``).
        Postconditions: returns a ``Dict`` mapping the team's ``ToolAgentKind``
          enum members to constructed agent instances.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must override _build_tool_agents"
        )

    def _build_and_validate_tool_agents(self, llm: LLMClient) -> Dict[Any, Any]:
        """Build tool agents and validate the roster matches the config's declared registry.

        Calls :meth:`_build_tool_agents` (the subclass-supplied hook) then
        :meth:`_validate_tool_agents` to enforce exact equality with the
        config's ``tool_agent_kinds``.

        Preconditions:
            ``llm`` is a configured ``LLMClient`` (not ``None``).
        Postconditions:
            Returns a ``Dict[Any, Any]`` mapping every kind declared in
            ``self.config.tool_agent_kinds`` (as strings or enum members) to a
            constructed agent instance. Raises ``ValueError`` (from
            ``_validate_tool_agents``) if the built roster does not exactly
            match the config's declared kinds.
        """
        agents = self._build_tool_agents(llm)
        self._validate_tool_agents(agents)
        return agents
