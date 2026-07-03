"""Background orchestrator/runner thread targets for the SE team API.

Each ``_run_*_background`` is the ``threading.Thread`` target a route spawns to
drive a long-running orchestrator/team off the request thread. They register in
the shared thread registry (``state._active_orchestrator_threads``) and persist
terminal state via the job store.

Preconditions:
    - Called as a thread target with a valid ``job_id`` naming an existing job.
Postconditions:
    - Job state is persisted (completed/failed) before the thread exits; the
      registry entry (when used) is popped in a ``finally``.
"""

import logging
import threading
from typing import Any, Dict, List, Optional

from software_engineering_team.api.state import _active_orchestrator_threads
from software_engineering_team.shared.job_store import (
    JOB_STATUS_FAILED,
    update_job,
)

logger = logging.getLogger(__name__)


def _run_orchestrator_background(
    job_id: str,
    repo_path: str,
    *,
    spec_content_override: Optional[str] = None,
    resolved_questions_override: Optional[List[Dict[str, Any]]] = None,
    planning_only: bool = False,
    sprint_id: Optional[str] = None,
) -> None:
    """Run orchestrator in background thread."""
    _active_orchestrator_threads[job_id] = threading.current_thread()
    try:  # pragma: no cover  # integration-only: delegates into run_orchestrator (LLM + git + subprocess)
        from orchestrator import run_orchestrator

        run_orchestrator(
            job_id,
            repo_path,
            spec_content_override=spec_content_override,
            resolved_questions_override=resolved_questions_override,
            planning_only=planning_only,
            sprint_id=sprint_id,
        )
    except (
        Exception
    ) as e:  # pragma: no cover  # integration-only: paired with integration-only try block
        logger.exception("Orchestrator failed")
        update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)
    finally:  # pragma: no cover  # integration-only: paired with integration-only try block
        _active_orchestrator_threads.pop(job_id, None)


def _run_retry_background(job_id: str) -> None:
    """Run retry in background thread."""
    try:  # pragma: no cover  # integration-only: thin wrapper around run_failed_tasks
        from orchestrator import run_failed_tasks

        run_failed_tasks(job_id)
    except (
        Exception
    ) as e:  # pragma: no cover  # integration-only: paired with integration-only try block
        logger.exception("Retry orchestrator failed")
        update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)


def _run_frontend_code_v2_background(
    job_id: str, repo_path: str, task_dict: dict, architecture_overview: str
) -> None:
    """Run frontend-code-v2 workflow in a background thread."""
    try:  # pragma: no cover  # integration-only: drives FrontendCodeV2TeamLead.run_workflow (LLM + npm/ng)
        import uuid as _uuid
        from pathlib import Path as _Path

        from frontend_code_v2_team import FrontendCodeV2TeamLead

        from llm_service import get_client
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

        def _job_updater(**kwargs):
            completed_phases = []
            current = kwargs.get("current_phase", "")
            for p in phase_order:
                if p == current:
                    break
                completed_phases.append(p)
            update_job(job_id, completed_phases=completed_phases, **kwargs)

        result = team_lead.run_workflow(
            repo_path=_Path(repo_path),
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
    except (
        Exception
    ) as e:  # pragma: no cover  # integration-only: paired with integration-only try block
        logger.exception("Frontend-code-v2 workflow failed")
        update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)


def _run_backend_code_v2_background(
    job_id: str, repo_path: str, task_dict: dict, architecture_overview: str
) -> None:
    """Run backend-code-v2 workflow in a background thread."""
    try:  # pragma: no cover  # integration-only: drives BackendCodeV2TeamLead.run_workflow (LLM + git + lint/build)
        import uuid as _uuid
        from pathlib import Path as _Path

        from backend_code_v2_team import BackendCodeV2TeamLead

        from llm_service import get_client
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

        def _job_updater(**kwargs):
            completed_phases = []
            current = kwargs.get("current_phase", "")
            for p in phase_order:
                if p == current:
                    break
                completed_phases.append(p)
            update_job(job_id, completed_phases=completed_phases, **kwargs)

        result = team_lead.run_workflow(
            repo_path=_Path(repo_path),
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
    except (
        Exception
    ) as e:  # pragma: no cover  # integration-only: paired with integration-only try block
        logger.exception("Backend-code-v2 workflow failed")
        update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)


def _run_product_analysis_background(
    job_id: str,
    repo_path: str,
    spec_content: str,
    initial_spec_path: Optional[str] = None,
) -> None:
    """Run product analysis workflow in a background thread."""
    try:  # pragma: no cover  # integration-only: drives ProductRequirementsAnalysisAgent.run_workflow (multi-phase LLM)
        from pathlib import Path as _Path

        from product_requirements_analysis_agent import (
            AnalysisPhase,
            ProductRequirementsAnalysisAgent,
        )
        from spec_parser import gather_context_files

        from llm_service import get_client

        update_job(job_id, status="running")

        def _job_updater(**kwargs: Any) -> None:
            update_job(job_id, **kwargs)

        # Gather context files for PRA agent
        context_files = gather_context_files(repo_path)
        if context_files:
            logger.info("Product analysis: Gathered %d context files", len(context_files))

        agent = ProductRequirementsAnalysisAgent(get_client("backend"))
        result = agent.run_workflow(
            spec_content=spec_content,
            repo_path=_Path(repo_path),
            job_id=job_id,
            job_updater=_job_updater,
            context_files=context_files,
            initial_spec_path=_Path(initial_spec_path) if initial_spec_path else None,
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
            else result.current_phase.value
            if result.current_phase
            else None,
            iterations=result.iterations,
            validated_spec_path=result.validated_spec_path,
        )
    except (
        Exception
    ) as e:  # pragma: no cover  # integration-only: paired with integration-only try block
        logger.exception("Product analysis workflow failed")
        update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)
