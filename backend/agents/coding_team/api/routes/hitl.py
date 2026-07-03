"""coding_team API — human-in-the-loop routes: submit answers and resume.

Handlers register on a module-local ``APIRouter`` that ``main`` mounts with
``app.include_router`` (absolute paths unchanged). Collaborators are dereferenced
through the ``main`` hub so patches applied to ``main`` still take effect.
"""

from __future__ import annotations

import logging
import os

# Ensure backend/agents is on path for coding_team and job_service_client
from fastapi import APIRouter, HTTPException

from coding_team import hitl
from coding_team.api import main as _main
from coding_team.api.models import (
    RunResponse,
    StatusResponse,
    SubmitAnswersRequest,
)
from coding_team.token_crypto import decrypt_token

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/run/{job_id}/answers", response_model=StatusResponse)
def submit_pending_answers(job_id: str, request: SubmitAnswersRequest) -> StatusResponse:
    """Submit answers to a paused coding-team job's pending questions and resume it.

    The orchestrator's blocked wait loop clears on the stored answers (thread alive). If the
    thread died (e.g. a server restart), the orchestrator is restarted automatically; only when
    that is impossible (no usable plan/repo_path) are the answers merely stored with a
    status_text directing the caller to POST /run/{job_id}/resume.

    Authentication/authorization is enforced by the unified API security gateway in front of all
    team mounts; like every other coding-team route, this endpoint assumes that perimeter.
    """
    data = _main.get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Job not found")
    answers = _main._validate_answers(data, request)
    _main.store_submit_answers(job_id, answers)
    if not _main._is_run_thread_alive(job_id):
        # Re-read the record after storing answers: the job may have been cancelled between the
        # initial get_job and now. _try_auto_resume's terminal check must see that current state, or
        # it could spawn a fresh orchestrator for an already-terminal job and overwrite its status.
        current = _main.get_job(job_id) or data
        # Write the optimistic status BEFORE spawning so the endpoint never clobbers a newer
        # status_text the freshly started orchestrator may have already written.
        _main.update_job(job_id, status_text="Answers received; resuming the run.")
        if _main._try_auto_resume(job_id, current):
            logger.info(
                "Orchestrator thread for job %s was not running; restarted it after answers.",
                job_id,
            )
        else:
            logger.info(
                "Orchestrator thread for job %s is not running and could not be auto-resumed; "
                "answers stored. Call POST /run/%s/resume to restart it.",
                job_id,
                job_id,
            )
            _main.update_job(
                job_id,
                status_text="Answers received. Resume the job to continue processing.",
            )
    return _main.get_status(job_id)


@router.post("/run/{job_id}/resume", response_model=RunResponse)
def resume_job(job_id: str) -> RunResponse:
    """Restart a paused coding-team job's orchestrator after answers were stored but its thread died.

    No-op-safe: if a thread is still running (or a wait loop heartbeats from another worker), it
    will resume on its own and this just reports status. GitHub-issue jobs are restarted through
    the full hook path so publication (PR, issue comments) is preserved; that path needs a GitHub
    token, sourced by decrypting the one persisted (as opaque ciphertext) on the job record at
    creation (falling back to the ``GITHUB_TOKEN`` env).

    Authentication/authorization is enforced by the unified API security gateway in front of all
    team mounts; like every other coding-team route, this endpoint assumes that perimeter.

    Preconditions:
        - The job exists, is not terminal, and (once liveness can't be proven) is paused in the
          ``waiting_for_user`` state — the only state a resume is both needed and provably safe.
    Postconditions:
        - Raises 404 (unknown job), 400 (terminal job, a non-paused job that can't be proven
          alive, missing repo_path/plan, or a GitHub-issue job with no usable token); returns
          "already running" without spawning when a live thread, fresh heartbeat, or concurrent
          claim exists; otherwise spawns the orchestrator (hook path for GitHub-issue jobs) and
          reports "Job resumed."
    """
    data = _main.get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Job not found")
    if hitl.is_terminal(data):
        raise HTTPException(
            status_code=400,
            detail=f"Job is {data.get('status', 'terminal')} and cannot be resumed.",
        )
    if _main._is_run_thread_alive(job_id) or _main._answer_wait_heartbeat_fresh(data):
        # The thread registry is process-local; a fresh answer-wait heartbeat means the job's
        # wait loop is alive in another worker — resuming here would double-drive the job.
        return RunResponse(
            job_id=job_id, status=data.get("status", "running"), message="Job already running."
        )
    # Past the liveness no-op, we could not PROVE the job is alive — but proof is only possible for
    # a paused job (its wait loop heartbeats). A job in any other non-terminal state (most
    # dangerously ``running``, actively doing code work with no heartbeat) might still be alive in
    # another worker, and a heartbeat goes stale 30s after a pause ends. Only a paused
    # (waiting_for_user) job is safely resumable; restarting anything else risks a second
    # orchestrator mutating the same checkout concurrently.
    if data.get("status") != hitl.WAITING_STATUS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Job is {data.get('status', 'in an unknown state')}, not paused waiting for "
                "answers; only a paused (waiting_for_user) job can be resumed."
            ),
        )
    plan_raw = data.get("plan_input") or {}
    if not isinstance(plan_raw, dict):
        raise HTTPException(
            status_code=400, detail="Job has a corrupted plan_input and cannot be resumed."
        )
    repo_path = data.get("repo_path") or plan_raw.get("repo_path")
    if not repo_path:
        raise HTTPException(status_code=400, detail="Job has no plan_input/repo_path to resume.")
    plan = _main.plan_from_input(plan_raw, repo_path)

    ctx = data.get("github_context") or {}
    is_github_job = bool(
        ctx.get("owner") and ctx.get("repo") and ctx.get("issue_number") is not None
    )
    # Prefer the token persisted (encrypted) at job creation; fall back to GITHUB_TOKEN env.
    token = (
        (decrypt_token(data.get("github_token_encrypted")) or os.environ.get("GITHUB_TOKEN"))
        if is_github_job
        else None
    )
    if is_github_job and not token:
        raise HTTPException(
            status_code=400,
            detail=(
                "GitHub-issue job cannot resume: no GitHub token is available (none persisted on "
                "the job record and GITHUB_TOKEN unset), and the publish flow (PR, issue comments) "
                "would be lost without one."
            ),
        )

    # Cross-worker claim FIRST (shared store), then the process-local claim: together they stop two
    # concurrent resume requests — in the same OR different worker processes — from both spawning an
    # orchestrator for this job. A store transport error here must surface as a controlled 500: a
    # bare propagation 500s opaquely, and swallowing it to False would falsely report "already
    # running" when no claim was actually taken.
    try:
        claimed = _main.claim_resume(job_id)
    except Exception as e:
        logger.exception("Resume for job %s: resume-claim store error.", job_id)
        raise HTTPException(
            status_code=500, detail="Failed to acquire the resume claim due to a job-store error."
        ) from e
    if not claimed:
        return RunResponse(
            job_id=job_id, status=data.get("status", "running"), message="Job already running."
        )
    # Post-claim re-read: a wait loop in another worker may have consumed answers and advanced the
    # job out of waiting_for_user between the initial GET and the claim. claim_resume checks only
    # the stamp, not the status, so verify freshness here before spawning.
    try:
        post_claim = _main.get_job(job_id)
    except Exception as exc:
        _main.release_resume_claim(job_id)
        raise HTTPException(
            status_code=500, detail="Failed to verify job state after acquiring resume claim."
        ) from exc
    if not post_claim or post_claim.get("status") != hitl.WAITING_STATUS:
        _main.release_resume_claim(job_id)
        return RunResponse(
            job_id=job_id,
            status=(post_claim or data).get("status", "running"),
            message="Job already running.",
        )
    if not _main._claim_run_thread(job_id):
        _main.release_resume_claim(job_id)
        return RunResponse(
            job_id=job_id, status=data.get("status", "running"), message="Job already running."
        )

    try:
        if is_github_job:
            _main._start_github_resume_thread(job_id, ctx, repo_path, plan, token or "")
        else:
            _main._start_orchestrator_thread(job_id, repo_path, plan)
    except Exception:
        # A failed spawn must release the shared claim so a later /resume can win.
        _main.release_resume_claim(job_id)
        raise
    return RunResponse(job_id=job_id, status="running", message="Job resumed.")
