"""
Temporal activities for the software engineering team.

Each activity wraps the existing orchestrator or standalone runner logic;
they run in the worker process and update the job store. No threads are started.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from temporalio import activity

from software_engineering_team.shared.job_store import (
    JOB_STATUS_FAILED,
    update_job,
)

logger = logging.getLogger(__name__)

# Default long timeout for run_orchestrator (e.g. 48 hours)
RUN_ORCHESTRATOR_SCHEDULE_TO_CLOSE_SECONDS = 48 * 3600
RETRY_FAILED_SCHEDULE_TO_CLOSE_SECONDS = 24 * 3600
STANDALONE_SCHEDULE_TO_CLOSE_SECONDS = 12 * 3600


@activity.defn(name="run_orchestrator")
def run_orchestrator_activity(
    job_id: str,
    repo_path: str,
    spec_content_override: Optional[str] = None,
    resolved_questions_override: Optional[List[Dict[str, Any]]] = None,
    planning_only: bool = False,
) -> None:
    """Execute the main Tech Lead orchestrator (run_orchestrator)."""
    try:
        from software_engineering_team.orchestrator import run_orchestrator

        run_orchestrator(
            job_id,
            Path(repo_path),
            spec_content_override=spec_content_override,
            resolved_questions_override=resolved_questions_override,
            planning_only=planning_only,
        )
    except Exception as e:
        logger.exception("Orchestrator activity failed")
        update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)


@activity.defn(name="retry_failed")
def retry_failed_activity(job_id: str) -> None:
    """Re-run failed tasks for a job (run_failed_tasks)."""
    try:
        from software_engineering_team.orchestrator import run_failed_tasks

        run_failed_tasks(job_id)
    except Exception as e:
        logger.exception("Retry failed activity failed")
        update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)


def _run_frontend_code_v2_impl(
    job_id: str,
    repo_path: str,
    task_dict: Dict[str, Any],
    architecture_overview: str,
) -> None:
    """Same logic as _run_frontend_code_v2_background without starting a thread."""
    import uuid as _uuid

    from llm_service import get_client
    from software_engineering_team.frontend_code_v2_team import FrontendCodeV2TeamLead
    from software_engineering_team.shared.models import (
        SystemArchitecture,
        Task,
        TaskStatus,
        TaskType,
    )

    update_job(job_id, status="running")
    tid = task_dict.get("id") or f"fv2-{_uuid.uuid4().hex[:8]}"
    task = Task(
        id=tid,
        title=task_dict.get("title", ""),
        description=task_dict.get("description", ""),
        requirements=task_dict.get("requirements", ""),
        acceptance_criteria=task_dict.get("acceptance_criteria", []),
        type=TaskType.FRONTEND,
        assignee="frontend-code-v2",
        status=TaskStatus.PENDING,
    )
    arch = SystemArchitecture(overview=architecture_overview) if architecture_overview else None
    team_lead = FrontendCodeV2TeamLead(get_client("frontend"))
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

    result = team_lead.run_workflow(
        repo_path=Path(repo_path),
        task=task,
        architecture=arch,
        job_updater=_job_updater,
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


@activity.defn(name="run_frontend_code_v2")
def run_frontend_code_v2_activity(
    job_id: str,
    repo_path: str,
    task_dict: Dict[str, Any],
    architecture_overview: str = "",
) -> None:
    """Execute frontend-code-v2 workflow."""
    try:
        _run_frontend_code_v2_impl(job_id, repo_path, task_dict, architecture_overview)
    except Exception as e:
        logger.exception("Frontend-code-v2 activity failed")
        update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)


def _run_backend_code_v2_impl(
    job_id: str,
    repo_path: str,
    task_dict: Dict[str, Any],
    architecture_overview: str,
) -> None:
    """Same logic as _run_backend_code_v2_background without starting a thread."""
    import uuid as _uuid

    from llm_service import get_client
    from software_engineering_team.backend_code_v2_team import BackendCodeV2TeamLead
    from software_engineering_team.shared.models import (
        SystemArchitecture,
        Task,
        TaskStatus,
        TaskType,
    )

    update_job(job_id, status="running")
    tid = task_dict.get("id") or f"bv2-{_uuid.uuid4().hex[:8]}"
    task = Task(
        id=tid,
        title=task_dict.get("title", ""),
        description=task_dict.get("description", ""),
        requirements=task_dict.get("requirements", ""),
        acceptance_criteria=task_dict.get("acceptance_criteria", []),
        type=TaskType.BACKEND,
        assignee="backend-code-v2",
        status=TaskStatus.PENDING,
    )
    arch = SystemArchitecture(overview=architecture_overview) if architecture_overview else None
    team_lead = BackendCodeV2TeamLead(get_client("backend"))
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

    result = team_lead.run_workflow(
        repo_path=Path(repo_path),
        task=task,
        architecture=arch,
        job_updater=_job_updater,
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


@activity.defn(name="run_backend_code_v2")
def run_backend_code_v2_activity(
    job_id: str,
    repo_path: str,
    task_dict: Dict[str, Any],
    architecture_overview: str = "",
) -> None:
    """Execute backend-code-v2 workflow."""
    try:
        _run_backend_code_v2_impl(job_id, repo_path, task_dict, architecture_overview)
    except Exception as e:
        logger.exception("Backend-code-v2 activity failed")
        update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)



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

    update_job(job_id, status="running")

    def _job_updater(**kwargs: Any) -> None:
        update_job(job_id, **kwargs)

    context_files = gather_context_files(repo_path)
    if context_files:
        logger.info("Product analysis: Gathered %d context files", len(context_files))

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
    """Execute product-analysis workflow."""
    try:
        _run_product_analysis_impl(job_id, repo_path, spec_content, initial_spec_path)
    except Exception as e:
        logger.exception("Product analysis activity failed")
        update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)


# ---------------------------------------------------------------------------
# V2 workflow activities — each is one phase of the pipeline
# ---------------------------------------------------------------------------


@activity.defn(name="parse_spec_and_analyze")
def parse_spec_activity(
    job_id: str,
    repo_path: str,
    spec_content_override: Optional[str] = None,
) -> Dict[str, Any]:
    """Phase 1: Parse spec + run Product Requirements Analysis.

    Returns SpecParseResult as a dict.
    """
    from software_engineering_team.temporal.phase_models import SpecParseResult

    try:
        from software_engineering_team.orchestrator import (
            _check_cancellation,
            ensure_plan_dir,
        )
        from software_engineering_team.shared.job_store import JOB_STATUS_RUNNING

        path = Path(repo_path).resolve()
        update_job(job_id, status=JOB_STATUS_RUNNING, phase="product_analysis", status_text="Starting pipeline")

        from spec_parser import (
            gather_context_files,
            get_newest_spec_content,
            get_newest_spec_path,
            parse_spec_with_llm,
        )

        from llm_service import get_client

        initial_spec_path = None
        if spec_content_override is not None:
            spec_content = spec_content_override
        else:
            initial_spec_path = get_newest_spec_path(path)
            spec_content = get_newest_spec_content(path)

        context_files = gather_context_files(path)
        requirements = parse_spec_with_llm(spec_content, get_client("spec_intake"))
        update_job(job_id, requirements_title=requirements.title, status_text="Specification parsed")

        _check_cancellation(job_id)
        plan_dir = ensure_plan_dir(path)

        # Run PRA
        from product_requirements_analysis_agent import ProductRequirementsAnalysisAgent

        def _pra_updater(**kwargs: Any) -> None:
            analysis_phase = kwargs.pop("current_phase", None)
            if analysis_phase:
                kwargs["analysis_subprocess"] = analysis_phase
            update_job(job_id, phase="product_analysis", **kwargs)

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
        _check_cancellation(job_id)

        return SpecParseResult(
            spec_content=spec_content,
            validated_spec=validated_spec,
            requirements_title=requirements.title,
            plan_dir=str(plan_dir),
            context_files_count=len(context_files),
            pra_iterations=pra_result.iterations,
        ).model_dump()

    except Exception as e:
        logger.exception("parse_spec_activity failed for job %s", job_id)
        update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)
        raise


@activity.defn(name="plan_project")
def plan_project_activity(
    job_id: str,
    repo_path: str,
    spec_parse_result: Dict[str, Any],
) -> Dict[str, Any]:
    """Phase 2: Run Planning V3 workflow.

    Returns PlanResult as a dict.
    """
    from software_engineering_team.temporal.phase_models import PlanResult, SpecParseResult

    try:
        from software_engineering_team.orchestrator import _check_cancellation, _get_agents

        spec_data = SpecParseResult.model_validate(spec_parse_result)
        path = Path(repo_path).resolve()
        validated_spec = spec_data.validated_spec or spec_data.spec_content

        update_job(job_id, phase="planning", status_text="Starting planning workflow")

        from planning_v3_adapter import adapt_planning_v3_result
        from spec_parser import parse_spec_with_llm

        from llm_service import get_client
        from planning_v3_team.orchestrator import run_workflow as run_planning_v3_workflow

        # Re-parse requirements for the adapter (lightweight)
        requirements = parse_spec_with_llm(spec_data.spec_content, get_client("spec_intake"))

        agents = _get_agents()

        def _planning_updater(**kwargs: Any) -> None:
            planning_phase = kwargs.pop("current_phase", None)
            if planning_phase:
                kwargs["planning_subprocess"] = planning_phase
            update_job(job_id, **kwargs)

        def _run_architecture(spec_content, prd_content, rp, client_context):
            from architecture_expert.models import ArchitectureInput

            from software_engineering_team.shared.models import ProductRequirements

            req_desc = (spec_content or "").strip()
            if prd_content:
                req_desc = (req_desc + "\n\n" + prd_content.strip()).strip()
            reqs = ProductRequirements(
                title="Project", description=req_desc or "See planning artifacts.",
                acceptance_criteria=["Deliver according to spec."], constraints=[], priority="medium", metadata={},
            )
            features_doc = prd_content or ""
            arch_input = ArchitectureInput(
                requirements=reqs,
                technology_preferences=["Python", "FastAPI", "PostgreSQL", "Docker"],
                project_overview={"features_and_functionality_doc": features_doc, "goals": ""},
                features_and_functionality_doc=features_doc or None,
            )
            try:
                arch_output = agents["architecture"].run(arch_input)
                return (arch_output.architecture.overview or "") if arch_output and arch_output.architecture else None
            except Exception:
                return None

        p3_result = run_planning_v3_workflow(
            repo_path=str(path),
            spec_content=validated_spec,
            use_product_analysis=False,
            use_planning_v2=False,
            llm=get_client("project_planning"),
            job_updater=_planning_updater,
            run_architecture_fn=_run_architecture,
        )
        if not p3_result.get("success"):
            err = p3_result.get("failure_reason") or "Planning V3 failed"
            update_job(job_id, status=JOB_STATUS_FAILED, error=err, phase="completed")
            return PlanResult().model_dump()

        adapter_result = adapt_planning_v3_result(p3_result, spec_title=requirements.title, repo_path=str(path))
        adapter_result.shared_planning_doc_path = str(path / "plan" / "planning_team" / "planning_document.md")
        spec_content_for_planning = adapter_result.final_spec_content or spec_data.spec_content
        update_job(job_id, requirements_title=adapter_result.requirements.title)

        _check_cancellation(job_id)

        return PlanResult(
            adapter_result_dict=adapter_result.model_dump() if hasattr(adapter_result, "model_dump") else {},
            spec_content_for_planning=spec_content_for_planning,
            requirements_title=adapter_result.requirements.title,
        ).model_dump()

    except Exception as e:
        logger.exception("plan_project_activity failed for job %s", job_id)
        update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)
        raise


@activity.defn(name="execute_coding_team")
def execute_coding_team_activity(
    job_id: str,
    repo_path: str,
    plan_result: Dict[str, Any],
    resolved_questions_override: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Phase 3: Build CodingTeamPlanInput and run coding team.

    Returns ExecutionResult as a dict.
    """
    from software_engineering_team.temporal.phase_models import ExecutionResult
    from software_engineering_team.temporal.phase_models import PlanResult as PlanResultModel

    try:
        from software_engineering_team.orchestrator import (
            _build_coding_team_plan_input,
            _read_repo_code,
            _truncate_for_context,
        )

        plan_data = PlanResultModel.model_validate(plan_result)
        path = Path(repo_path).resolve()

        # Reconstruct adapter_result from dict
        from planning_v3_adapter import PlanningV2AdapterResult

        adapter_result = PlanningV2AdapterResult.model_validate(plan_data.adapter_result_dict)

        existing_code = _truncate_for_context(_read_repo_code(path), 8000)
        if existing_code == "# No code files found":
            existing_code = None

        plan_input = _build_coding_team_plan_input(
            adapter_result, str(path), existing_code, resolved_questions_override
        )

        from coding_team.orchestrator import run_coding_team_orchestrator
        from llm_service import get_client
        from software_engineering_team.shared.job_store import JOB_STATUS_COMPLETED, get_job

        run_coding_team_orchestrator(
            job_id,
            str(path),
            plan_input,
            update_job_fn=lambda **kw: update_job(job_id, **kw),
            get_job_fn=lambda jid: get_job(jid),
            get_llm=get_client,
        )
        update_job(job_id, status=JOB_STATUS_COMPLETED, phase="completed")

        return ExecutionResult(merged_count=0).model_dump()

    except Exception as e:
        logger.exception("execute_coding_team_activity failed for job %s", job_id)
        update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)
        raise
