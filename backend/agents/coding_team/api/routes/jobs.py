"""coding_team API — core job lifecycle routes: health, run, status, jobs list."""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import List

# Ensure backend/agents is on path for coding_team and job_service_client
from fastapi import APIRouter, HTTPException

from coding_team.agent_status import build_agent_statuses
from coding_team.api import main as _main
from coding_team.api.models import (
    JobListItem,
    PendingQuestion,
    RunRequest,
    RunResponse,
    StatusResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "coding-team"}


def _temporal_dispatch(job_id: str, request: RunRequest) -> bool:
    """Dispatch the job to Temporal when enabled; return True if it was dispatched.

    Preconditions:
        - ``job_id`` names a job row that already exists (the caller ran
          ``create_job``); ``request.plan_input`` is non-null.
    Postconditions:
        - Returns True and starts ``CodingTeamWorkflow`` when ``TEMPORAL_ADDRESS``
          is set and the worker is reachable.
        - Returns False (dispatching nothing) only when the Temporal path is not
          taken at all — Temporal disabled, or its modules unavailable — so the
          caller runs the thread path.
        - Raises (does NOT return False) when Temporal is enabled but the
          dispatch itself fails, e.g. the worker client is not ready
          (``start_workflow_sync`` raises ``RuntimeError``/``TimeoutError``). We
          deliberately do not silently fall back to the thread path here: a
          ``TimeoutError`` can fire after the workflow was already scheduled, so
          a second in-process run would duplicate side effects (branch/commit/PR).
          The caller marks the job failed and surfaces the error instead.
    """
    try:
        from shared_temporal import is_temporal_enabled
    except ImportError:
        return False
    if not is_temporal_enabled():
        return False
    try:
        from coding_team.temporal.start_workflow import start_coding_team_workflow
    except ImportError:
        # Temporal enabled but the team's dispatch module can't be imported: this
        # is a real misconfiguration on a durable deployment, not a reason to
        # silently run non-durably. Surface it rather than hiding it.
        logger.exception("Temporal enabled but coding_team dispatch module failed to import")
        raise
    start_coding_team_workflow(job_id, request.repo_path, request.plan_input)
    logger.info("Coding team job dispatched via Temporal: job_id=%s", job_id)
    return True


@router.post("/run", response_model=RunResponse)
def post_run(request: RunRequest) -> RunResponse:
    """Start a coding_team job. If plan_input is provided, runs orchestrator in background.

    Dispatches through Temporal (durable, restart-survivable) when
    ``TEMPORAL_ADDRESS`` is set; otherwise runs the orchestrator in a daemon
    thread. Both paths return the same ``job_id`` for the client to poll.
    """
    job_id = str(uuid.uuid4())
    _main.create_job(job_id=job_id, repo_path=request.repo_path, plan_input=request.plan_input)
    if request.plan_input:
        try:
            dispatched = _temporal_dispatch(job_id, request)
        except Exception as e:
            # Temporal was enabled but the dispatch failed (worker not ready,
            # start timeout, bad config). Mark the freshly-created row failed so
            # it is not orphaned in 'pending', and surface a retryable error
            # instead of an opaque 500. We do not fall back to the thread path:
            # the workflow may already be scheduled, and a second run would
            # duplicate side effects.
            logger.exception("Coding team Temporal dispatch failed: %s", e)
            _main.update_job(
                job_id,
                status="failed",
                error=f"Temporal dispatch failed: {e}",
                current_activity=None,
            )
            raise HTTPException(
                status_code=503,
                detail="Temporal dispatch failed (worker unavailable); job marked failed. Retry.",
            ) from e
        if dispatched:
            return RunResponse(
                job_id=job_id,
                status="running",
                message="Job started (Temporal). Poll GET /status/{job_id} for progress.",
            )
        plan = _main.plan_from_input(request.plan_input, request.repo_path)

        def run() -> None:
            _main._register_run_thread(job_id)
            try:
                _main.run_orchestrator_wired(job_id, request.repo_path, plan)
            except Exception as e:
                logger.exception("Coding team orchestrator failed: %s", e)
                # current_activity=None: a crash skips the in-flow clears, and a
                # failed job must not keep serving a frozen mid-review sub-bar.
                _main.update_job(job_id, status="failed", error=str(e), current_activity=None)
            finally:
                _main._clear_run_thread(job_id)

        t = threading.Thread(target=run, daemon=True)
        t.start()
    return RunResponse(job_id=job_id, status="pending")


@router.get("/status/{job_id}", response_model=StatusResponse)
def get_status(job_id: str) -> StatusResponse:
    """Get job status and task graph summary."""
    data = _main.get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Job not found")
    return StatusResponse(
        job_id=data.get("job_id", job_id),
        status=data.get("status", "pending"),
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
        pending_questions=[PendingQuestion(**q) for q in data.get("pending_questions", [])],
        waiting_for_answers=bool(data.get("waiting_for_answers", False)),
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


# ---------------------------------------------------------------------------
# GitHub-issue-driven runs
# ---------------------------------------------------------------------------
