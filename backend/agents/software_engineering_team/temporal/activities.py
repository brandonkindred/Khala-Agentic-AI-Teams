"""
Temporal activities for the software engineering team.

Each activity wraps the existing orchestrator or standalone runner logic;
they run in the worker process and update the job store. No threads are started.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from temporalio import activity

from shared.concurrency import BackgroundHeartbeat
from shared.observability import bind_trace_id, current_trace_id, new_trace_id
from shared.temporal.activity_utils import is_last_attempt
from software_engineering_team.shared.job_store import (
    JOB_STATUS_FAILED,
    JOB_STATUS_RUNNING,
    update_job,
)

logger = logging.getLogger(__name__)

RETRY_FAILED_SCHEDULE_TO_CLOSE_SECONDS = 24 * 3600
STANDALONE_SCHEDULE_TO_CLOSE_SECONDS = 12 * 3600


@activity.defn(name="retry_failed")
def retry_failed_activity(job_id: str, trace_id: str = "") -> None:
    """Re-run failed tasks for a job (run_failed_tasks).

    Postconditions:
        On failure the job is marked FAILED and the exception is re-raised so
        Temporal can retry (per the workflow retry policy) and fail the workflow.
        ``trace_id`` (workflow-supplied, or freshly generated when blank) is
        forwarded to ``run_failed_tasks``, which binds it for the retry.
    """
    resolved_trace_id = trace_id or new_trace_id()
    try:
        from software_engineering_team.orchestrator import run_failed_tasks

        run_failed_tasks(job_id, trace_id=resolved_trace_id)
    except Exception as e:
        logger.exception("Retry failed activity failed", extra={"trace_id": resolved_trace_id})
        update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)
        raise


def _run_code_v2_impl(
    job_id: str,
    repo_path: str,
    task_dict: Dict[str, Any],
    architecture_overview: str,
    *,
    task_type: Any,
    assignee: str,
    id_prefix: str,
    team_lead_factory: Callable[[], Any],
) -> None:
    """Shared body for the frontend/backend code-v2 activities.

    Preconditions:
        team_lead_factory returns an object exposing a ``run_workflow(**kwargs)``
        method with the same contract as FrontendCodeV2TeamLead/BackendCodeV2TeamLead.
    """
    import uuid as _uuid

    from shared.dev_models.models import (
        SystemArchitecture,
        Task,
        TaskStatus,
    )

    update_job(job_id, status=JOB_STATUS_RUNNING)
    tid = task_dict.get("id") or f"{id_prefix}-{_uuid.uuid4().hex[:8]}"
    task = Task(
        id=tid,
        title=task_dict.get("title", ""),
        description=task_dict.get("description", ""),
        requirements=task_dict.get("requirements", ""),
        acceptance_criteria=task_dict.get("acceptance_criteria", []),
        type=task_type,
        assignee=assignee,
        status=TaskStatus.PENDING,
    )
    arch = SystemArchitecture(overview=architecture_overview) if architecture_overview else None
    team_lead = team_lead_factory()
    phase_order = [
        "setup",
        "planning",
        "execution",
        "review",
        "problem_solving",
        "documentation",
        "deliver",
    ]

    def _job_updater(**kwargs: Any) -> None:
        completed_phases = []
        current = kwargs.get("current_phase", "")
        for p in phase_order:
            if p == current:
                break
            completed_phases.append(p)
        update_job(job_id, completed_phases=completed_phases, **kwargs)

    from software_engineering_team.shared.production_review_agents import (
        build_production_review_kwargs_in_process,
    )

    result = team_lead.run_workflow(
        repo_path=Path(repo_path),
        task=task,
        architecture=arch,
        job_updater=_job_updater,
        **build_production_review_kwargs_in_process(),
    )
    final_status = "completed" if result.success else "failed"
    update_job(
        job_id,
        status=final_status,
        progress=100 if result.success else (result.iterations_used * 20),
        summary=result.summary,
        error=result.failure_reason if not result.success else None,
        current_phase=result.current_phase.value if result.current_phase else "deliver",
    )


def _run_frontend_code_v2_impl(
    job_id: str,
    repo_path: str,
    task_dict: Dict[str, Any],
    architecture_overview: str,
) -> None:
    """Same logic as _run_frontend_code_v2_background without starting a thread."""
    from llm_service import get_client
    from shared.dev_models.models import TaskType
    from software_engineering_team.frontend_code_v2_team import FrontendCodeV2TeamLead

    _run_code_v2_impl(
        job_id,
        repo_path,
        task_dict,
        architecture_overview,
        task_type=TaskType.FRONTEND,
        assignee="frontend-code-v2",
        id_prefix="fv2",
        team_lead_factory=lambda: FrontendCodeV2TeamLead(get_client("frontend")),
    )


@activity.defn(name="run_frontend_code_v2")
def run_frontend_code_v2_activity(
    job_id: str,
    repo_path: str,
    task_dict: Dict[str, Any],
    architecture_overview: str = "",
) -> None:
    """Execute frontend-code-v2 workflow.

    Postconditions:
        On the final Temporal attempt, the job is marked FAILED; on a non-final
        attempt the FAILED write is skipped so a retry that later succeeds never
        leaves a transient FAILED status behind. The exception is always
        re-raised so Temporal can retry (per the workflow retry policy) and fail
        the workflow once attempts are exhausted.
    """
    try:
        _run_frontend_code_v2_impl(job_id, repo_path, task_dict, architecture_overview)
    except Exception as e:
        logger.exception("Frontend-code-v2 activity failed", extra={"trace_id": current_trace_id()})
        if is_last_attempt():
            update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)
        raise


def _run_backend_code_v2_impl(
    job_id: str,
    repo_path: str,
    task_dict: Dict[str, Any],
    architecture_overview: str,
) -> None:
    """Same logic as _run_backend_code_v2_background without starting a thread."""
    from llm_service import get_client
    from shared.dev_models.models import TaskType
    from software_engineering_team.backend_code_v2_team import BackendCodeV2TeamLead

    _run_code_v2_impl(
        job_id,
        repo_path,
        task_dict,
        architecture_overview,
        task_type=TaskType.BACKEND,
        assignee="backend-code-v2",
        id_prefix="bv2",
        team_lead_factory=lambda: BackendCodeV2TeamLead(get_client("backend")),
    )


@activity.defn(name="run_backend_code_v2")
def run_backend_code_v2_activity(
    job_id: str,
    repo_path: str,
    task_dict: Dict[str, Any],
    architecture_overview: str = "",
) -> None:
    """Execute backend-code-v2 workflow.

    Postconditions:
        On the final Temporal attempt, the job is marked FAILED; on a non-final
        attempt the FAILED write is skipped so a retry that later succeeds never
        leaves a transient FAILED status behind. The exception is always
        re-raised so Temporal can retry (per the workflow retry policy) and fail
        the workflow once attempts are exhausted.
    """
    try:
        _run_backend_code_v2_impl(job_id, repo_path, task_dict, architecture_overview)
    except Exception as e:
        logger.exception("Backend-code-v2 activity failed", extra={"trace_id": current_trace_id()})
        if is_last_attempt():
            update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)
        raise


def _run_product_analysis_impl(
    job_id: str,
    repo_path: str,
    spec_content: str,
    initial_spec_path: Optional[str] = None,
) -> None:
    """Same logic as _run_product_analysis_background without starting a thread."""
    from llm_service import get_client
    from software_engineering_team.product_requirements_analysis_agent import (
        AnalysisPhase,
        ProductRequirementsAnalysisAgent,
    )
    from software_engineering_team.spec_parser import gather_context_files

    update_job(job_id, status=JOB_STATUS_RUNNING)

    def _job_updater(**kwargs: Any) -> None:
        update_job(job_id, **kwargs)

    context_files = gather_context_files(repo_path)
    if context_files:
        logger.info(
            "Product analysis: Gathered %d context files",
            len(context_files),
            extra={"trace_id": current_trace_id()},
        )

    agent = ProductRequirementsAnalysisAgent(get_client("backend"))
    result = agent.run_workflow(
        spec_content=spec_content,
        repo_path=Path(repo_path),
        job_id=job_id,
        job_updater=_job_updater,
        context_files=context_files,
        initial_spec_path=Path(initial_spec_path) if initial_spec_path else None,
    )
    final_status = "completed" if result.success else "failed"
    update_job(
        job_id,
        status=final_status,
        progress=100 if result.success else 90,
        summary=result.summary,
        error=result.failure_reason if not result.success else None,
        current_phase=AnalysisPhase.SPEC_CLEANUP.value
        if result.success
        else (result.current_phase.value if result.current_phase else None),
        iterations=result.iterations,
        validated_spec_path=result.validated_spec_path,
    )


@activity.defn(name="run_product_analysis")
def run_product_analysis_activity(
    job_id: str,
    repo_path: str,
    spec_content: str,
    initial_spec_path: Optional[str] = None,
) -> None:
    """Execute product-analysis workflow.

    Postconditions:
        On the final Temporal attempt, the job is marked FAILED; on a non-final
        attempt the FAILED write is skipped so a retry that later succeeds never
        leaves a transient FAILED status behind. The exception is always
        re-raised so Temporal can retry (per the workflow retry policy) and fail
        the workflow once attempts are exhausted.
    """
    try:
        _run_product_analysis_impl(job_id, repo_path, spec_content, initial_spec_path)
    except Exception as e:
        logger.exception("Product analysis activity failed", extra={"trace_id": current_trace_id()})
        if is_last_attempt():
            update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)
        raise


# ---------------------------------------------------------------------------
# V2 workflow activities — each is one phase of the pipeline
# ---------------------------------------------------------------------------


@activity.defn(name="parse_spec_and_analyze")
def parse_spec_activity(
    job_id: str,
    repo_path: str,
    spec_content_override: Optional[str] = None,
    trace_id: str = "",
    sprint_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Phase 1: Parse spec + run Product Requirements Analysis.

    Returns SpecParseResult as a dict. ``trace_id`` (workflow-supplied, or freshly
    generated when blank) is bound for the duration of this activity — this activity
    runs in its own process/thread, so unlike the thread-mode orchestrator the id
    must be passed explicitly rather than inherited via contextvars.

    Postconditions:
        When ``sprint_id`` is set, spec content is synthesized from the
        ``product_delivery`` sprint's planned stories (via
        ``shared.sprint_scope.load_requirements_from_sprint``) instead of read from
        disk, and both the LLM spec-parse and the PRA agent are skipped — mirroring
        the thread-mode orchestrator's sprint path (``discovery.py``).
    """
    with bind_trace_id(trace_id or new_trace_id()):
        return _parse_spec_activity_body(job_id, repo_path, spec_content_override, sprint_id)


def _parse_spec_activity_body(
    job_id: str,
    repo_path: str,
    spec_content_override: Optional[str],
    sprint_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Body of :func:`parse_spec_activity`, run inside its ``bind_trace_id`` block.

    Preconditions: a trace id is already bound (callers must go through
        :func:`parse_spec_activity`).
    Postconditions: returns a ``SpecParseResult`` dict; on the final Temporal
        attempt the job is marked FAILED, while a non-final attempt skips the
        FAILED write so a retry that later succeeds never leaves a transient
        FAILED status behind. Either way, the exception propagates to the
        activity wrapper.
    """
    from software_engineering_team.temporal.phase_models import SpecParseResult

    try:
        from software_engineering_team.orchestrator import (
            _check_cancellation,
            ensure_plan_dir,
        )
        from software_engineering_team.shared.job_store import JOB_STATUS_RUNNING

        path = Path(repo_path).resolve()
        update_job(
            job_id,
            status=JOB_STATUS_RUNNING,
            phase="product_analysis",
            status_text="Starting pipeline",
        )

        from llm_service import get_client
        from software_engineering_team.spec_parser import (
            gather_context_files,
            get_newest_spec_content,
            get_newest_spec_path,
            parse_spec_with_llm,
        )

        initial_spec_path = None
        requirements = None
        # Sprint path: spec is synthesized from the product_delivery sprint's planned
        # stories. Both the LLM spec-parse and the PRA agent are skipped below — the
        # spec is already structured (per-story user_story + ACs) and validated by
        # the upstream Sprint Planner. Mirrors discovery.py::resolve_spec_source.
        if sprint_id is not None:
            if spec_content_override is not None:
                # Raise (rather than marking the job FAILED and returning a normal
                # result) so the activity itself fails: RunTeamWorkflowV2 doesn't
                # inspect SpecParseResult for a failure sentinel, so a normal return
                # here would let the workflow barrel into Phase 2/3 on an empty spec
                # even though the job was already marked FAILED.
                raise ValueError(
                    "parse_spec_activity received both sprint_id and "
                    "spec_content_override; they are mutually exclusive."
                )

            from software_engineering_team.shared.sprint_scope import (
                load_requirements_from_sprint,
            )

            requirements, spec_content = load_requirements_from_sprint(sprint_id)
        elif spec_content_override is not None:
            spec_content = spec_content_override
        else:
            initial_spec_path = get_newest_spec_path(path)
            spec_content = get_newest_spec_content(path)

        context_files = gather_context_files(path)
        if sprint_id is None:
            requirements = parse_spec_with_llm(spec_content, get_client("spec_intake"))
        update_job(
            job_id, requirements_title=requirements.title, status_text="Specification parsed"
        )

        _check_cancellation(job_id)
        plan_dir = ensure_plan_dir(path)

        if sprint_id is not None:
            # Sprint path: PRA's review/communicate/update/cleanup loop has nothing
            # to do (the spec is already structured and validated), so the
            # synthesized spec is used directly. Mirrors
            # discovery.py::run_product_requirements_analysis's sprint path.
            validated_spec = spec_content
            pra_iterations = 0
        else:
            # Run PRA
            from software_engineering_team.orchestrator import _make_pra_job_updater
            from software_engineering_team.product_requirements_analysis_agent import (
                ProductRequirementsAnalysisAgent,
            )

            # Shared with the thread path: rewrites current_phase into the analysis_*
            # fields AND rescales the agent's own 0-100 progress onto the
            # product-analysis band — without it the Temporal bar sprints to 100
            # during PRA and collapses at the next phase handoff.
            _pra_updater = _make_pra_job_updater(job_id)

            pra_agent = ProductRequirementsAnalysisAgent(get_client("product_analysis"))
            pra_result = pra_agent.run_workflow(
                spec_content=spec_content,
                repo_path=path,
                job_id=job_id,
                job_updater=_pra_updater,
                context_files=context_files,
                initial_spec_path=Path(initial_spec_path) if initial_spec_path else None,
            )
            if not pra_result.success:
                err = pra_result.failure_reason or "PRA did not complete"
                update_job(job_id, status=JOB_STATUS_FAILED, error=err, phase="completed")
                return SpecParseResult(spec_content=spec_content).model_dump()

            validated_spec = pra_result.final_spec_content or spec_content
            pra_iterations = pra_result.iterations

        _check_cancellation(job_id)

        return SpecParseResult(
            spec_content=spec_content,
            validated_spec=validated_spec,
            requirements_title=requirements.title,
            plan_dir=str(plan_dir),
            context_files_count=len(context_files),
            pra_iterations=pra_iterations,
        ).model_dump()

    except Exception as e:
        logger.exception(
            "parse_spec_activity failed for job %s",
            job_id,
            extra={"trace_id": current_trace_id()},
        )
        if is_last_attempt():
            update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)
        raise


@activity.defn(name="plan_project")
def plan_project_activity(
    job_id: str,
    repo_path: str,
    spec_parse_result: Dict[str, Any],
    trace_id: str = "",
) -> Dict[str, Any]:
    """Phase 2: Run Planning workflow.

    Returns PlanResult as a dict. ``trace_id`` (workflow-supplied, or freshly
    generated when blank) is bound for the duration of this activity — see
    ``parse_spec_activity`` for why it must be passed explicitly here rather than
    inherited via contextvars.
    """
    with bind_trace_id(trace_id or new_trace_id()):
        return _plan_project_activity_body(job_id, repo_path, spec_parse_result)


def _plan_project_activity_body(
    job_id: str,
    repo_path: str,
    spec_parse_result: Dict[str, Any],
) -> Dict[str, Any]:
    """Body of :func:`plan_project_activity`, run inside its ``bind_trace_id`` block.

    Preconditions: a trace id is already bound (callers must go through
        :func:`plan_project_activity`); ``spec_parse_result`` validates as a ``SpecParseResult``.
    Postconditions: returns a ``PlanResult`` dict; on the final Temporal attempt the job is
        marked FAILED, while a non-final attempt skips the FAILED write so a retry that
        later succeeds never leaves a transient FAILED status behind. Either way, the
        exception propagates to the activity wrapper. Uses ``spec_data.requirements_title``
        (set by Phase 1) as the adapter's spec title rather than re-parsing
        ``spec_data.spec_content`` via the LLM — avoids a second, nondeterministic parse and
        an unnecessary spec-intake LLM dependency; required for the sprint path, where
        ``spec_data.spec_content`` is synthesized Markdown, not LLM-parseable prose.
    """
    from software_engineering_team.temporal.phase_models import PlanResult, SpecParseResult

    try:
        from software_engineering_team.orchestrator import _check_cancellation, _get_agents

        spec_data = SpecParseResult.model_validate(spec_parse_result)
        path = Path(repo_path).resolve()
        validated_spec = spec_data.validated_spec or spec_data.spec_content

        update_job(job_id, phase="planning", status_text="Starting planning workflow")

        from llm_service import get_client
        from planning_team.orchestrator import run_workflow as run_planning_workflow
        from software_engineering_team.planning_adapter import adapt_planning_result
        from software_engineering_team.shared import planning_audit

        agents = _get_agents()

        from software_engineering_team.orchestrator import (
            _make_planning_architecture_fn,
            _make_planning_job_updater,
        )

        # Shared with the thread path: rescales Planning's own 0-100 progress onto
        # the planning band so the Temporal bar stays monotone into the coding phase.
        _planning_updater = _make_planning_job_updater(job_id)

        # Identical wiring to the thread path: the shared factory owns architecture-input
        # construction (including technology_preferences derivation) and resolves the agent
        # lazily/defensively, so a construction failure degrades to no overview rather than
        # aborting planning.
        _run_architecture = _make_planning_architecture_fn(lambda: agents["architecture"])

        planning_result = run_planning_workflow(
            repo_path=str(path),
            spec_content=validated_spec,
            use_product_analysis=False,
            llm=get_client("project_planning"),
            job_updater=_planning_updater,
            run_architecture_fn=_run_architecture,
        )
        if not planning_result.get("success"):
            err = planning_result.get("failure_reason") or "Planning failed"
            update_job(job_id, status=JOB_STATUS_FAILED, error=err, phase="completed")
            return PlanResult().model_dump()

        planning_audit.record_se_planning_run(job_id, planning_result)

        adapter_result = adapt_planning_result(
            planning_result, spec_title=spec_data.requirements_title, repo_path=str(path)
        )
        adapter_result.shared_planning_doc_path = str(
            path / "plan" / "planning_team" / "planning_document.md"
        )
        spec_content_for_planning = adapter_result.final_spec_content or spec_data.spec_content
        update_job(job_id, requirements_title=adapter_result.requirements.title)

        _check_cancellation(job_id)

        # to_dict, not model_dump: the adapter result is a dataclass, and the old
        # hasattr(model_dump) probe silently serialized {} — the coding activity
        # could then never reconstruct it and every Temporal run died at handoff.
        return PlanResult(
            adapter_result_dict=adapter_result.to_dict(),
            spec_content_for_planning=spec_content_for_planning,
            requirements_title=adapter_result.requirements.title,
        ).model_dump()

    except Exception as e:
        logger.exception(
            "plan_project_activity failed for job %s",
            job_id,
            extra={"trace_id": current_trace_id()},
        )
        if is_last_attempt():
            update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)
        raise


def _coding_heartbeat_interval_s() -> float:
    """Interval (seconds) between background heartbeats for the coding-team activity.

    Must stay comfortably below the activity's `heartbeat_timeout` (10 min). Override via
    `CODING_TEAM_HEARTBEAT_INTERVAL_S`; blank/garbage/non-positive falls back to 30s.
    """
    raw = os.getenv("CODING_TEAM_HEARTBEAT_INTERVAL_S", "")
    try:
        val = float(raw)
        return val if val > 0 else 30.0
    except (TypeError, ValueError):
        return 30.0


def _coding_update_callback(job_id: str) -> Callable[..., None]:
    """Forward orchestrator progress writes to `update_job(job_id, **kw)`.

    Liveness is owned by the background `BackgroundHeartbeat` in
    `execute_coding_team_activity`, not here — this callback does not heartbeat.

    Preconditions:
        - `job_id` identifies an existing job.
    Postconditions:
        - The returned callable forwards all kwargs to `update_job(job_id, **kwargs)`.
    """

    def _update(**kw: Any) -> None:
        update_job(job_id, **kw)

    return _update


@activity.defn(name="execute_coding_team")
def execute_coding_team_activity(
    job_id: str,
    repo_path: str,
    plan_result: Dict[str, Any],
    resolved_questions_override: Optional[List[Dict[str, Any]]] = None,
    trace_id: str = "",
) -> Dict[str, Any]:
    """Phase 3: Build CodingTeamPlanInput and run coding team.

    Returns ExecutionResult as a dict. ``trace_id`` (workflow-supplied, or freshly
    generated when blank) is bound for the duration of this activity, including the
    ``parallel_map`` fan-out inside ``run_coding_team_orchestrator``. Note the V2
    workflow defines no Phase-4 activity, so the integration finalize step
    (``_emit_coding_team_metrics`` / ``_finalize_from_coding_snapshot``) runs on the
    thread-mode path only and is not covered by this activity's bound id.
    """
    with bind_trace_id(trace_id or new_trace_id()):
        return _execute_coding_team_activity_body(
            job_id, repo_path, plan_result, resolved_questions_override
        )


def _execute_coding_team_activity_body(
    job_id: str,
    repo_path: str,
    plan_result: Dict[str, Any],
    resolved_questions_override: Optional[List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Body of :func:`execute_coding_team_activity`, run inside its ``bind_trace_id`` block.

    Preconditions: a trace id is already bound (callers must go through
        :func:`execute_coding_team_activity`); ``plan_result`` validates as a ``PlanResult``
        whose ``adapter_result_dict`` reconstructs a ``PlanningAdapterResult``.
    Postconditions: returns an ``ExecutionResult`` dict; the coding-team orchestrator owns
        the job's terminal status on every exit path, and the bound trace id is visible to
        its ``parallel_map`` workers via ``contextvars.copy_context()``.
    """
    from software_engineering_team.temporal.phase_models import ExecutionResult
    from software_engineering_team.temporal.phase_models import PlanResult as PlanResultModel

    try:
        from shared.repo_context.repo_utils import read_repo_code, truncate_for_context
        from software_engineering_team.orchestrator import _build_coding_team_plan_input

        plan_data = PlanResultModel.model_validate(plan_result)
        path = Path(repo_path).resolve()

        # Reconstruct adapter_result from dict
        from software_engineering_team.planning_adapter import PlanningAdapterResult

        adapter_result = PlanningAdapterResult.from_dict(plan_data.adapter_result_dict)

        existing_code = truncate_for_context(read_repo_code(path), 8000)
        if existing_code == "# No code files found":
            existing_code = None

        plan_input = _build_coding_team_plan_input(
            adapter_result, str(path), existing_code, resolved_questions_override
        )

        from software_engineering_team.coding_engine_provider import SECodeEngineProvider
        from software_engineering_team.coding_team_orchestrator import run_coding_team_orchestrator
        from software_engineering_team.orchestrator import PROGRESS_BAND_CODING
        from software_engineering_team.shared.job_store import get_job

        # Single liveness mechanism: a background beater emits `activity.heartbeat()` on a fixed
        # interval for the whole run, keeping the activity alive across long blocking steps (e.g.
        # multi-minute code-gen LLM calls) that emit no update callback. `copy_context=True` carries
        # the Temporal activity handle into the beater thread; beat errors (outside an activity
        # context, e.g. unit tests) are swallowed so the loop survives.
        with BackgroundHeartbeat(
            activity.heartbeat,
            _coding_heartbeat_interval_s(),
            name="coding-team-heartbeat",
            copy_context=True,
            join_timeout=5.0,
        ):
            base, span = PROGRESS_BAND_CODING
            # Mirrors the thread path (software_engineering_team/orchestrator.py):
            # get_llm deliberately NOT passed — the coding team's default getter wraps
            # the LLM clients in strands models with reasoning-stream capture, whose
            # periodic flush is the only thing refreshing job activity DURING a
            # multi-minute LLM call. Passing the raw get_client both made every long
            # call look stalled AND handed TechLeadAgent a non-strands object that
            # Agent(model=...) cannot construct from. The band keeps the SE job's
            # progress bar monotone across the planning → coding handoff.
            run_coding_team_orchestrator(
                job_id,
                str(path),
                plan_input,
                update_job_fn=_coding_update_callback(job_id),
                get_job_fn=lambda jid: get_job(jid),
                progress_base=base,
                progress_span=span,
                engine_provider=SECodeEngineProvider(),
            )
        # run_coding_team_orchestrator owns its terminal status on every exit path (the heartbeat
        # callback forwards its status writes to update_job), so do not re-write COMPLETED here — it
        # would clobber a failure / partial-success the orchestrator already set.

        return ExecutionResult(merged_count=0).model_dump()

    except Exception as e:
        logger.exception(
            "execute_coding_team_activity failed for job %s",
            job_id,
            extra={"trace_id": current_trace_id()},
        )
        update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)
        raise
