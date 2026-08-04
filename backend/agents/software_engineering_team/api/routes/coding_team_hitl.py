"""coding_team API — human-in-the-loop routes: submit answers and resume."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from shared.temporal.runner import signal_workflow_sync
from software_engineering_team import hitl
from software_engineering_team.api import coding_team_main as _main
from software_engineering_team.api.coding_team_models import (
    RunResponse,
    StatusResponse,
    SubmitAnswersRequest,
)
from software_engineering_team.api.orchestration import ResumeSpawnResult
from software_engineering_team.temporal.coding_team_constants import WORKFLOW_ID_PREFIX

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/run/{job_id}/answers", response_model=StatusResponse)
def submit_pending_answers(job_id: str, request: SubmitAnswersRequest) -> StatusResponse:
    """Submit answers to a paused coding-team job's pending questions and resume it.

    Two distinct pause mechanisms, told apart by whether the job record carries a
    ``resume_token`` (set only by a ``pause_strategy="return"`` pause — see
    ``pause_cycle._run_pause_cycle``; never set by a block-mode pause):

    - **Temporal-native pause** (``resume_token`` present): the client must echo the same
      ``resume_token`` it was given in the pause notification/status poll — a mismatch (or a
      missing one, since a legitimate client can only have learned it from the job record)
      raises 409, per the contract doc §3: without this check a client holding a stale token
      would get a 200 while ``CodingTeamWorkflow.submit_answers`` silently drops the mismatched
      signal, giving false confidence the answer landed. Once validated, there is no live
      thread to unblock by clearing the job record's pause flag — ``run_pipeline_activity``
      already returned, and ``CodingTeamWorkflow`` is durably waiting on a signal. Answers are
      appended to ``submitted_answers`` WITHOUT clearing the pause envelope (the orchestrator's
      own re-entry check owns that — see ``pause_cycle._check_pending_pause_reentry``, called
      from ``coding_team_orchestrator.run_coding_team_orchestrator`` — clearing it here would
      race a worker crash into silently dropping the answer), then ``CodingTeamWorkflow`` is signaled
      directly so it re-invokes the pipeline activity with ``acknowledged_resume_token`` set to
      this same token.
    - **Thread-mode / GitHub-hook pause** (``resume_token`` absent): unchanged from before this
      branch existed. The orchestrator's blocked wait loop clears on the stored answers (thread
      alive). If the thread died (e.g. a server restart), the orchestrator is restarted
      automatically; only when that is impossible (no usable plan/repo_path) are the answers
      merely stored with a status_text directing the caller to POST /run/{job_id}/resume.

    Authentication/authorization is enforced by the unified API security gateway in front of all
    team mounts; like every other coding-team route, this endpoint assumes that perimeter.
    """
    data = _main.get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Job not found")
    answers = _main._validate_answers(data, request)
    resume_token = data.get("resume_token")
    if resume_token:
        if request.resume_token != resume_token:
            raise HTTPException(
                status_code=409,
                detail="resume_token does not match this job's current pause; it is stale or "
                "answers a pause that already resolved.",
            )
        _main.store_append_submitted_answers(job_id, answers)
        signal_workflow_sync(
            f"{WORKFLOW_ID_PREFIX}{job_id}",
            "submit_answers",
            {"resume_token": resume_token, "answers": answers},
        )
        return _main.get_status(job_id)
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
    recovered = _main._recover_resume_plan(job_id, plan_raw, repo_path)
    if recovered is None:
        raise HTTPException(
            status_code=400, detail="Job has an invalid plan_input and cannot be resumed."
        )
    repo_path, plan = recovered

    resolved = _main._resolve_github_job_token(job_id, data)
    if resolved is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "GitHub-issue job cannot resume: no GitHub token is available (none persisted on "
                "the job record and GITHUB_TOKEN unset), and the publish flow (PR, issue comments) "
                "would be lost without one."
            ),
        )
    is_github_job, ctx, token = resolved

    result, post_claim, err = _main._claim_and_spawn_resume(
        job_id, ctx, repo_path, plan, token, is_github_job
    )
    if result is ResumeSpawnResult.CLAIM_STORE_ERROR:
        # A store transport error here must surface as a controlled 500: a bare propagation 500s
        # opaquely, and swallowing it to False would falsely report "already running" when no
        # claim was actually taken.
        logger.error("Resume for job %s: resume-claim store error.", job_id, exc_info=err)
        raise HTTPException(
            status_code=500, detail="Failed to acquire the resume claim due to a job-store error."
        ) from err
    if result in (ResumeSpawnResult.CLAIM_LOST, ResumeSpawnResult.THREAD_CLAIM_LOST):
        return RunResponse(
            job_id=job_id, status=data.get("status", "running"), message="Job already running."
        )
    if result is ResumeSpawnResult.POST_CLAIM_READ_ERROR:
        raise HTTPException(
            status_code=500, detail="Failed to verify job state after acquiring resume claim."
        ) from err
    if result is ResumeSpawnResult.NOT_WAITING:
        return RunResponse(
            job_id=job_id,
            status=(post_claim or data).get("status", "running"),
            message="Job already running.",
        )
    if result is ResumeSpawnResult.SPAWN_FAILED:
        # A failed spawn must release the shared claim so a later /resume can win — already done
        # inside _claim_and_spawn_resume; re-raise the original spawn failure unchanged.
        raise err
    if result is not ResumeSpawnResult.SPAWNED:
        # Exhaustiveness guard: a future ResumeSpawnResult member falling through here would
        # silently report a successful resume for what is actually a new, unhandled outcome.
        raise RuntimeError(f"Unhandled ResumeSpawnResult: {result!r}")
    return RunResponse(job_id=job_id, status="running", message="Job resumed.")
