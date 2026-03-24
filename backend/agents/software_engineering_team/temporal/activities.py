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


def _run_planning_v2_impl(
    job_id: str,
    repo_path: str,
    spec_content: str,
    inspiration_content: Optional[str],
) -> None:
    """Same logic as _run_planning_v2_background without starting a thread."""
    from llm_service import get_client
    from software_engineering_team.planning_v2_team import PlanningV2TeamLead
    from software_engineering_team.planning_v2_team.models import Phase

    update_job(job_id, status="running")
    phase_order = [p.value for p in Phase]

    def _job_updater(**kwargs: Any) -> None:
        completed_phases = []
        current = kwargs.get("current_phase", "")
        for p in phase_order:
            if p == current:
                break
            completed_phases.append(p)
        update_job(job_id, completed_phases=completed_phases, **kwargs)

    team_lead = PlanningV2TeamLead(get_client("backend"))
    result = team_lead.run_workflow(
        spec_content=spec_content,
        repo_path=Path(repo_path),
        inspiration_content=inspiration_content or None,
        job_updater=_job_updater,
        job_id=job_id,
    )
    from software_engineering_team.shared.job_store import is_cancel_requested

    if is_cancel_requested(job_id):
        logger.info(
            "Planning-v2: cancellation detected, preserving cancelled state for job %s", job_id
        )
        return
    final_status = "completed" if result.success else "failed"
    phase_results: Dict[str, Any] = {}
    if result.spec_review_result is not None:
        phase_results["spec_review_result"] = result.spec_review_result.model_dump()
    if result.planning_result is not None:
        phase_results["planning_result"] = result.planning_result.model_dump()
    if result.implementation_result is not None:
        phase_results["implementation_result"] = result.implementation_result.model_dump()
    if result.review_result is not None:
        phase_results["review_result"] = result.review_result.model_dump()
    if result.problem_solving_result is not None:
        phase_results["problem_solving_result"] = result.problem_solving_result.model_dump()
    if result.deliver_result is not None:
        phase_results["deliver_result"] = result.deliver_result.model_dump()
    update_job(
        job_id,
        status=final_status,
        progress=100 if result.success else 90,
        summary=result.summary,
        error=result.failure_reason if not result.success else None,
        current_phase=Phase.DELIVER.value,
        phase_results=phase_results if phase_results else None,
    )


@activity.defn(name="run_planning_v2")
def run_planning_v2_activity(
    job_id: str,
    repo_path: str,
    spec_content: str,
    inspiration_content: Optional[str] = None,
) -> None:
    """Execute planning-v2 workflow."""
    try:
        _run_planning_v2_impl(job_id, repo_path, spec_content, inspiration_content)
    except Exception as e:
        logger.exception("Planning-v2 activity failed")
        from software_engineering_team.shared.job_store import is_cancel_requested

        if not is_cancel_requested(job_id):
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
