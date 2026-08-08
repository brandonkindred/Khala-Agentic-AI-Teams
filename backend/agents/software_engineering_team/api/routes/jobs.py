"""SE team API — run-team job lifecycle routes (create, upload, list, status, retry, cancel, delete, resume, restart, llm-recheck)."""

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from shared.hitl.status import pending_questions_from_raw
from software_engineering_team.api.models import (
    CancelJobResponse,
    DeleteJobResponse,
    FailedTaskDetail,
    JobStatusResponse,
    RetryResponse,
    RunningJobsResponse,
    RunningJobSummary,
    RunTeamRequest,
    RunTeamResponse,
)
from software_engineering_team.api.state import (
    RESTARTABLE_STATUSES,
    RESUMABLE_STATUSES,
    _coerce_current_activity,
    _coerce_progress,
    _parse_task_states,
    _parse_team_progress,
    _preflight_sprint_scope,
    _start_stale_job_monitor_once,
    create_project_workspace,
)
from software_engineering_team.shared.job_store import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PAUSED_LLM_CONNECTIVITY,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    create_job,
    delete_job,
    get_job,
    list_jobs,
    request_cancel,
    reset_job,
    start_job_heartbeat_thread,
    update_job,
)
from software_engineering_team.spec_parser import (
    validate_work_path,
    validate_workspace_path_no_spec,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _resolve_repo_path(repo_path: str | Path, sprint_id: Any) -> Path:
    """Validate a work folder, selecting the spec-gated or spec-free validator by sprint mode.

    Single source of the run/resume/restart path-validation contract. Sprint-mode
    runs synthesize the spec from product_delivery rather than reading it from
    disk, so the on-disk spec gate in ``validate_work_path`` would be a false 400
    on a code-only repo; ``validate_workspace_path_no_spec`` keeps the
    workspace-containment and directory-existence checks without the spec gate.

    Preconditions:
        - ``repo_path`` is a non-empty path-like naming a work folder.
        - ``sprint_id`` is ``None`` (spec-gated) or a sprint identifier (spec-free).
    Postconditions:
        - Returns the resolved, containment-checked ``Path``.
        - Raises ``HTTPException(400)`` if the path is invalid (wrapping the
          validator's ``ValueError``). No other exception type is swallowed.
    """
    try:
        if sprint_id is not None:
            return validate_workspace_path_no_spec(repo_path)
        return validate_work_path(repo_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post(
    "/run-team",
    response_model=RunTeamResponse,
    summary="Start software engineering team",
    description="Validates work folder, creates job, starts Tech Lead orchestrator in background. "
    "Returns job_id immediately. Poll GET /run-team/{job_id} for status.",
)
def run_team(request: RunTeamRequest) -> RunTeamResponse:
    """Start the software engineering team on a work folder."""
    repo_path = _resolve_repo_path(request.repo_path, request.sprint_id)

    # Validate `sprint_id` exists *and has planned scope* before
    # enqueuing the job — otherwise a typo, a deleted sprint, or a
    # never-planned sprint would return 200, kick off a background job,
    # and surface as an async failure on the orchestrator side, wasting
    # capacity and giving the client a misleading success response
    # (Codex review on PR #396). Shared with resume/restart.
    _preflight_sprint_scope(request.sprint_id)

    _start_stale_job_monitor_once()

    job_id = str(uuid.uuid4())
    create_job(job_id, str(repo_path), job_type="run_team")

    try:  # pragma: no cover  # integration-only: spawns Temporal workflow
        # Persist sprint_id inside the launch try so a transient
        # job-service failure on the update doesn't leave a pending
        # job with no workflow running. `None` is written explicitly
        # so non-sprint runs don't carry a stale value from a previous job
        # that reused the same row (defense in depth — create_job mints a fresh uuid).
        update_job(job_id, sprint_id=request.sprint_id)

        from software_engineering_team.temporal.start_workflow import start_run_team_workflow

        start_run_team_workflow(job_id, str(repo_path), sprint_id=request.sprint_id)
    except (
        Exception
    ) as e:  # pragma: no cover  # integration-only: paired with integration-only try block
        logger.exception("Failed to start run-team execution")
        update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)
        raise HTTPException(status_code=503, detail=f"Failed to start workflow: {e}") from e

    start_job_heartbeat_thread(job_id)

    return RunTeamResponse(
        job_id=job_id,
        status="running",
        message="Orchestrator started. Poll GET /run-team/{job_id} for status.",
    )


@router.post(
    "/run-team/upload",
    response_model=RunTeamResponse,
    summary="Start SE team from uploaded spec file",
    description=(
        "Multipart: project_name (text) + spec_file (.md/.txt). "
        "Creates workspace under SE_WORKSPACE_DIR, writes initial_spec.md, starts job. "
        "Returns same RunTeamResponse as POST /run-team."
    ),
)
async def run_team_upload(
    project_name: str = Form(..., min_length=1, max_length=200),
    spec_file: UploadFile = File(...),
) -> RunTeamResponse:
    """Start the SE team from an uploaded spec file, creating the workspace automatically."""
    MAX_BYTES = 5 * 1024 * 1024  # 5 MB
    raw = await spec_file.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="Spec file exceeds 5 MB limit.")
    try:
        workspace = create_project_workspace(project_name, raw)
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"File must be UTF-8: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _start_stale_job_monitor_once()
    job_id = str(uuid.uuid4())
    create_job(job_id, str(workspace), job_type="run_team")

    try:  # pragma: no cover  # integration-only: spawns Temporal workflow
        from software_engineering_team.temporal.start_workflow import start_run_team_workflow

        start_run_team_workflow(job_id, str(workspace))
    except (
        Exception
    ) as e:  # pragma: no cover  # integration-only: paired with integration-only try block
        logger.exception("Failed to start run-team/upload execution")
        update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)
        raise HTTPException(status_code=503, detail=f"Failed to start workflow: {e}") from e

    start_job_heartbeat_thread(job_id)
    return RunTeamResponse(
        job_id=job_id,
        status="running",
        message="Workspace created. Poll GET /run-team/{job_id} for status.",
    )


@router.get(
    "/run-team/jobs",
    response_model=RunningJobsResponse,
    summary="List running jobs",
    description="Returns jobs with status pending or running when running_only=True (default). Set running_only=false to return all jobs (including completed/failed/cancelled).",
)
def get_running_jobs(running_only: bool = True) -> RunningJobsResponse:
    """List jobs. When running_only=True (default), only pending or running; otherwise all jobs."""
    raw = list_jobs(running_only=running_only)
    jobs = [
        RunningJobSummary(
            job_id=item["job_id"],
            status=item["status"],
            repo_path=item.get("repo_path"),
            job_type=item.get("job_type") or "run_team",
            created_at=item.get("created_at"),
        )
        for item in raw
    ]
    # Sort by created_at descending (most recent first)
    jobs.sort(key=lambda j: j.created_at or "", reverse=True)
    return RunningJobsResponse(jobs=jobs)


@router.get(
    "/run-team/{job_id}",
    response_model=JobStatusResponse,
    summary="Get job status",
    description="Poll this endpoint for job progress and results.",
)
def get_job_status(job_id: str) -> JobStatusResponse:
    """Get the status of a run-team job."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    raw_failed = data.get("failed_tasks") or []
    failed_tasks = [
        FailedTaskDetail(
            task_id=str(ft.get("task_id", "")),
            title=str(ft.get("title", "")) if ft.get("title") is not None else "",
            reason=str(ft.get("reason", "")) if ft.get("reason") is not None else "",
        )
        for ft in raw_failed
        if isinstance(ft, dict)
    ]

    execution_order = data.get("execution_order")
    task_ids = list(execution_order) if isinstance(execution_order, list) else []

    task_states_parsed = _parse_task_states(data.get("task_states"))
    team_progress_parsed = _parse_team_progress(data.get("team_progress"))

    # Materialize via the shared helper: model_validate keeps EVERY stored field
    # (including recommendation/allow_multiple, which the previous hand-enumeration
    # silently dropped) and still defensively skips non-dict entries.
    raw_pending_questions = data.get("pending_questions", [])
    pending_questions_parsed = pending_questions_from_raw(raw_pending_questions)

    payload: Dict[str, Any] = {
        "job_id": str(job_id),
        "status": str(data.get("status", JOB_STATUS_PENDING)),
        "repo_path": data.get("repo_path"),
        "requirements_title": data.get("requirements_title"),
        "architecture_overview": data.get("architecture_overview"),
        "current_task": data.get("current_task"),
        "status_text": data.get("status_text"),
        "task_results": data.get("task_results")
        if isinstance(data.get("task_results"), list)
        else [],
        "task_ids": task_ids,
        "progress": _coerce_progress(data.get("progress")),
        "error": data.get("error"),
        "failed_tasks": [ft.model_dump() for ft in failed_tasks],
        "phase": data.get("phase"),
        "task_states": {k: v.model_dump() for k, v in task_states_parsed.items()}
        if task_states_parsed
        else None,
        "team_progress": {k: v.model_dump() for k, v in team_progress_parsed.items()}
        if team_progress_parsed
        else None,
        "pending_questions": [pq.model_dump() for pq in pending_questions_parsed],
        "waiting_for_answers": bool(data.get("waiting_for_answers", False)),
        "planning_subprocess": data.get("planning_subprocess"),
        "planning_completed_phases": data.get("planning_completed_phases") or [],
        "analysis_subprocess": data.get("analysis_subprocess"),
        "analysis_completed_phases": data.get("analysis_completed_phases") or [],
        "planning_hierarchy": data.get("planning_hierarchy"),
        "current_activity": _coerce_current_activity(data.get("current_activity")),
        "last_activity_at": data.get("last_activity_at"),
        "updated_at": data.get("updated_at"),
        "last_heartbeat_at": data.get("last_heartbeat_at"),
        "server_time": datetime.now(timezone.utc).isoformat(),
    }
    return JobStatusResponse.model_validate(payload)


@router.post(
    "/run-team/{job_id}/retry-failed",
    response_model=RetryResponse,
    summary="Retry failed tasks",
    description="Re-run only the tasks that failed in a previous job run. "
    "Use when status is completed, failed, or paused_llm_limit. "
    "When paused_llm_limit (Ollama weekly usage limit exceeded), call after the weekly limit resets to resume.",
)
def retry_failed_tasks(job_id: str) -> RetryResponse:
    """Retry the failed tasks from a previous job run."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    status = data.get("status")
    if status == "running":
        raise HTTPException(status_code=409, detail="Job is still running")

    failed_tasks = data.get("failed_tasks") or []
    if not failed_tasks:
        raise HTTPException(status_code=400, detail="No failed tasks to retry")

    failed_ids = [ft.get("task_id", "") for ft in failed_tasks]

    try:  # pragma: no cover  # integration-only: spawns Temporal workflow
        from software_engineering_team.temporal.start_workflow import start_retry_failed_workflow

        start_retry_failed_workflow(job_id)
    except (
        Exception
    ) as e:  # pragma: no cover  # integration-only: paired with integration-only try block
        logger.exception("Failed to start retry-failed workflow")
        update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)
        raise HTTPException(status_code=503, detail=str(e)) from e

    start_job_heartbeat_thread(job_id)

    return RetryResponse(
        job_id=job_id,
        status="running",
        retrying_tasks=failed_ids,
        message=f"Retrying {len(failed_ids)} failed tasks. Poll GET /run-team/{job_id} for status.",
    )


@router.post(
    "/run-team/{job_id}/cancel",
    response_model=CancelJobResponse,
    summary="Cancel a running job",
    description="Request cancellation for a running or pending job. Sets a cancellation flag that running agents "
    "check cooperatively and exit gracefully. Returns 200 if cancellation was requested, 404 if job not found, "
    "400 if job is already in a terminal state (completed, failed, or cancelled).",
)
def cancel_job(job_id: str) -> CancelJobResponse:
    """Request cancellation for a job."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    current_status = data.get("status", JOB_STATUS_PENDING)
    terminal_statuses = (JOB_STATUS_COMPLETED, JOB_STATUS_FAILED, JOB_STATUS_CANCELLED)
    if current_status in terminal_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Job is already in terminal state: {current_status}. Cannot cancel.",
        )

    success = request_cancel(job_id)
    if not success:
        raise HTTPException(
            status_code=400,
            detail="Failed to request cancellation. Job may have changed state.",
        )

    # When Temporal is enabled, also cancel the workflow so the worker stops
    try:
        from shared.temporal.client import is_temporal_enabled
        from software_engineering_team.temporal.start_workflow import cancel_run_team_workflow

        if is_temporal_enabled():
            cancel_run_team_workflow(job_id)
    except Exception as e:
        logger.debug("Temporal workflow cancel (non-fatal): %s", e)

    return CancelJobResponse(
        job_id=job_id,
        status="cancelled",
        message="Job cancellation requested. Running agents will stop at the next checkpoint.",
    )


@router.delete(
    "/run-team/{job_id}",
    response_model=DeleteJobResponse,
    summary="Delete a job",
    description="Remove the job from the store. It will no longer appear in the jobs list. "
    "If the job was running, any background work may continue until it next updates the job.",
)
def delete_run_team_job(job_id: str) -> DeleteJobResponse:
    """Delete a job by id. Returns 404 if job not found."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if not delete_job(job_id):
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return DeleteJobResponse(job_id=job_id, message="Job deleted")


# Include JOB_STATUS_FAILED so users can resume after server down or stale heartbeat


@router.post(
    "/run-team/{job_id}/resume",
    response_model=RunTeamResponse,
    summary="Resume an interrupted job",
    description="Re-start the orchestrator for a run_team job that was interrupted (e.g. server halt or runtime error). "
    "Allowed when status is pending, running, agent_crash, or failed. Use after server restart to re-initiate the job; "
    "poll GET /run-team/{job_id} for status.",
)
def resume_run_team_job(job_id: str) -> RunTeamResponse:
    """Resume a run_team job by re-starting the orchestrator. Use after server restart or when the job appears stuck."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Job not found")

    job_type = data.get("job_type")
    if job_type is not None and job_type != "run_team":
        raise HTTPException(
            status_code=400,
            detail=f"Only run_team jobs can be resumed via this endpoint (job_type={job_type}).",
        )

    status = data.get("status", JOB_STATUS_PENDING)
    if status not in RESUMABLE_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Job cannot be resumed (status={status}). Resume is only allowed for pending, running, agent_crash, or failed.",
        )

    repo_path = data.get("repo_path")
    if not repo_path:
        raise HTTPException(status_code=400, detail="Job has no repo_path; cannot resume.")

    sprint_id = data.get("sprint_id")
    # Validate for its raise side-effect only; keep the raw stored string
    # unchanged. The resolved Path is deliberately discarded here (unlike
    # run_team, which assigns it) so a resume never rewrites the path handed
    # to the orchestrator/workflow from the value persisted at creation.
    _resolve_repo_path(repo_path, sprint_id)

    # Re-validate the sprint scope on resume — the sprint may have been
    # deleted or unplanned since the job was created. Surfaces synchronously
    # before flipping the job to `running` (Codex review on PR #396).
    _preflight_sprint_scope(sprint_id)

    # current_activity is wiped because the dead attempt's finally clears never
    # ran — without this the UI renders its frozen mid-review sub-bar through the
    # resumed run. last_activity_at is re-stamped centrally by this very write,
    # so the stall warning cannot false-fire off the dead attempt's timestamp.
    update_job(
        job_id,
        status=JOB_STATUS_RUNNING,
        error=None,
        agent_crash_details=None,
        current_activity=None,
    )

    try:  # pragma: no cover  # integration-only: spawns Temporal workflow for resume
        from software_engineering_team.temporal.start_workflow import start_run_team_workflow

        # Pass previously submitted answers so the orchestrator doesn't re-ask questions.
        submitted_answers = data.get("submitted_answers") or None
        start_run_team_workflow(
            job_id,
            str(repo_path),
            resolved_questions_override=submitted_answers,
            sprint_id=sprint_id,
        )
    except (
        Exception
    ) as e:  # pragma: no cover  # integration-only: paired with integration-only try block
        logger.exception("Failed to start resume workflow")
        update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)
        raise HTTPException(status_code=503, detail=str(e)) from e

    start_job_heartbeat_thread(job_id)

    return RunTeamResponse(
        job_id=job_id,
        status="running",
        message="Job resumed. Poll GET /run-team/{job_id} for status.",
    )


@router.post(
    "/run-team/{job_id}/restart",
    response_model=RunTeamResponse,
    summary="Restart a completed/failed/cancelled run-team job",
    description="Resets the same job (same job_id) to initial state and starts the workflow again. "
    "Only allowed when the existing job is in a terminal state (completed, failed, cancelled, or agent_crash). "
    "Returns the same job_id.",
)
def restart_run_team_job(job_id: str) -> RunTeamResponse:
    """Restart a run_team job by resetting the existing job to initial state and re-running the orchestrator."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Job not found")

    job_type = data.get("job_type")
    if job_type is not None and job_type != "run_team":
        raise HTTPException(
            status_code=400,
            detail=f"Only run_team jobs can be restarted via this endpoint (job_type={job_type}).",
        )

    status = data.get("status", JOB_STATUS_PENDING)
    if status not in RESTARTABLE_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Job cannot be restarted (status={status}). "
                "Restart is only allowed for completed, failed, cancelled, or agent_crash jobs."
            ),
        )

    repo_path = data.get("repo_path")
    if not repo_path:
        raise HTTPException(status_code=400, detail="Job has no repo_path; cannot restart.")

    # Capture sprint_id before validation so we know which check to run.
    sprint_id = data.get("sprint_id")
    # Validate for its raise side-effect only; keep the raw stored string.
    # The resolved Path is discarded here (unlike run_team) so reset_job below
    # re-persists the original repo_path rather than a canonicalized rewrite.
    _resolve_repo_path(repo_path, sprint_id)

    # Re-persist sprint_id after reset_job clears the payload so a
    # sprint-scoped restart goes back through the synthesized-spec
    # path instead of silently falling back to repo spec parsing.

    # Re-validate the sprint scope BEFORE `reset_job` — otherwise a
    # restart with a deleted/unplanned sprint would discard the prior
    # job state, then fail asynchronously. Codex review on PR #396.
    _preflight_sprint_scope(sprint_id)

    reset_job(job_id, str(repo_path), job_type="run_team")
    update_job(job_id, status=JOB_STATUS_RUNNING, error=None, sprint_id=sprint_id)

    try:  # pragma: no cover  # integration-only: spawns Temporal workflow for restart
        from software_engineering_team.temporal.start_workflow import start_run_team_workflow

        start_run_team_workflow(job_id, str(repo_path), sprint_id=sprint_id)
    except (
        Exception
    ) as e:  # pragma: no cover  # integration-only: paired with integration-only try block
        logger.exception("Failed to start restart workflow")
        update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)
        raise HTTPException(status_code=503, detail=str(e)) from e

    start_job_heartbeat_thread(job_id)

    return RunTeamResponse(
        job_id=job_id,
        status="running",
        message="Job restarted. Poll GET /run-team/{job_id} for status.",
    )


@router.post(
    "/run-team/{job_id}/resume-after-llm-check",
    response_model=RetryResponse,
    summary="Resume after LLM connectivity check",
    description="Use when the job status is paused_llm_connectivity (frontend could not reach the LLM after retries). "
    "After the user has verified LLM connectivity, call this endpoint to set status to running and retry the failed task(s). "
    "Same retry flow as retry-failed; poll GET /run-team/{job_id} for status.",
)
def resume_after_llm_check(job_id: str) -> RetryResponse:
    """Resume a job paused due to LLM connectivity by retrying the failed tasks."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    status = data.get("status")
    if status != JOB_STATUS_PAUSED_LLM_CONNECTIVITY:
        raise HTTPException(
            status_code=400,
            detail=f"Job is not paused for LLM connectivity (status={status}). Use this endpoint only when status is {JOB_STATUS_PAUSED_LLM_CONNECTIVITY}.",
        )

    failed_tasks = data.get("failed_tasks") or []
    failed_ids = [ft.get("task_id", "") for ft in failed_tasks]

    update_job(job_id, status="running", error=None)

    try:  # pragma: no cover  # integration-only: spawns Temporal workflow
        from software_engineering_team.temporal.start_workflow import start_retry_failed_workflow

        start_retry_failed_workflow(job_id)
    except (
        Exception
    ) as e:  # pragma: no cover  # integration-only: paired with integration-only try block
        logger.exception("Failed to start resume-after-llm-check workflow")
        update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)
        raise HTTPException(status_code=503, detail=str(e)) from e

    start_job_heartbeat_thread(job_id)

    return RetryResponse(
        job_id=job_id,
        status="running",
        retrying_tasks=failed_ids,
        message="Resumed after LLM connectivity check. Poll GET /run-team/{job_id} for status.",
    )
