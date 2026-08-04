"""coding_team API — core job lifecycle routes: run, status, jobs list.

No ``/health`` route here: SE's own ``status.py`` already serves ``/health``
on the app these routers are mounted onto (``software_engineering_team.api.main``).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, HTTPException

from shared.hitl.status import pending_questions_from_raw
from software_engineering_team.agent_status import build_agent_statuses
from software_engineering_team.api import coding_team_main as _main
from software_engineering_team.api.coding_team_models import (
    JobListItem,
    RunRequest,
    RunResponse,
    StatusResponse,
)
from software_engineering_team.models import JobStatus

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/run", response_model=RunResponse)
def post_run(request: RunRequest) -> RunResponse:
    """Start a coding_team job. If plan_input is provided, dispatches to Temporal.

    Dispatch is unconditionally through Temporal (durable, restart-survivable);
    a plan-less request just creates a pending row for the caller to fill in later.
    """
    job_id = str(uuid.uuid4())
    _main.create_job(job_id=job_id, repo_path=request.repo_path, plan_input=request.plan_input)
    if request.plan_input:
        try:
            from software_engineering_team.temporal.coding_team_start_workflow import (
                start_coding_team_workflow,
            )

            start_coding_team_workflow(job_id, request.repo_path, request.plan_input)
            logger.info("Coding team job dispatched via Temporal: job_id=%s", job_id)
        except Exception as e:
            # Dispatch failed (worker not ready, start timeout, bad config). Mark
            # the freshly-created row failed so it is not orphaned in 'pending',
            # and surface a retryable error instead of an opaque 500.
            logger.exception("Coding team Temporal dispatch failed: %s", e)
            _main.update_job(
                job_id,
                status=JobStatus.FAILED.value,
                error=f"Temporal dispatch failed: {e}",
                current_activity=None,
            )
            raise HTTPException(
                status_code=503,
                detail="Temporal dispatch failed (worker unavailable); job marked failed. Retry.",
            ) from e
        return RunResponse(
            job_id=job_id,
            status=JobStatus.RUNNING.value,
            message="Job started (Temporal). Poll GET /status/{job_id} for progress.",
        )
    return RunResponse(job_id=job_id, status="pending")


@router.get("/status/{job_id}", response_model=StatusResponse)
def get_status(job_id: str) -> StatusResponse:
    """Get job status and task graph summary."""
    data = _main.get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Job not found")
    return StatusResponse(
        job_id=data.get("job_id", job_id),
        status=data.get("status", JobStatus.PENDING.value),
        phase=data.get("phase"),
        status_text=data.get("status_text"),
        thinking=data.get("thinking"),
        repo_path=data.get("repo_path"),
        task_graph_snapshot=data.get("task_graph_snapshot", []),
        agent_task_map=data.get("agent_task_map", {}),
        agents=build_agent_statuses(
            data.get("stack_specs", []),
            data.get("agent_task_map", {}),
            data.get("task_graph_snapshot", []),
            data.get("current_activity"),
            data.get("phase"),
        ),
        error=data.get("error"),
        github_context=data.get("github_context"),
        github_pr_url=data.get("github_pr_url"),
        review_summary=data.get("review_summary"),
        pending_questions=pending_questions_from_raw(data.get("pending_questions", [])),
        waiting_for_answers=bool(data.get("waiting_for_answers", False)),
        resume_token=data.get("resume_token"),
        current_activity=data.get("current_activity")
        if isinstance(data.get("current_activity"), dict)
        else None,
        last_activity_at=data.get("last_activity_at"),
        updated_at=data.get("updated_at"),
        last_heartbeat_at=data.get("last_heartbeat_at"),
        progress=_main._coerce_progress(data.get("progress")),
        server_time=datetime.now(timezone.utc).isoformat(),
    )


@router.get("/jobs", response_model=List[JobListItem])
def get_jobs(active: bool = False) -> List[JobListItem]:
    """List coding_team jobs.

    Postconditions:
        - With ``active=true``, only non-terminal jobs (pending/running/waiting_for_user) are
          returned, filtered at the job service so terminal jobs' full records (task graphs,
          thinking text) never cross the wire just to be discarded.
        - Every item carries the job's ``github_context`` (when present) and its
          ``waiting_for_answers`` flag, so list consumers can identify paused
          GitHub-issue runs without a per-job status call.
        - Missing fields fall back to ``None``/``False``; ``status`` defaults to
          ``"pending"`` for records that predate the field.
    """
    jobs = _main.list_jobs(active_only=active)
    return [
        JobListItem(
            job_id=j.get("job_id", ""),
            status=j.get("status", "pending"),
            repo_path=j.get("repo_path"),
            phase=j.get("phase"),
            status_text=j.get("status_text"),
            updated_at=j.get("updated_at"),
            waiting_for_answers=bool(j.get("waiting_for_answers", False)),
            github_context=j.get("github_context"),
        )
        for j in jobs
    ]
