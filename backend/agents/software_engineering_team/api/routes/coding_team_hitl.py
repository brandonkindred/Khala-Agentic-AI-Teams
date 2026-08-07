"""coding_team API — human-in-the-loop routes: submit answers and resume."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from shared.temporal.runner import signal_workflow_sync
from software_engineering_team import hitl
from software_engineering_team.api import coding_team_main as _main
from software_engineering_team.api.coding_team_models import (
    RunResponse,
    StatusResponse,
    SubmitAnswersRequest,
)
from software_engineering_team.temporal.coding_team_constants import WORKFLOW_ID_PREFIX

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
    - **Thread-mode / GitHub-hook pause** (``resume_token`` absent): answers are stored
      only; the caller must resume via a Temporal-native pause or other orchestration path.
      No auto-resume or thread restart.

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
    return _main.get_status(job_id)


@router.post("/run/{job_id}/resume", response_model=RunResponse)
def resume_job(job_id: str) -> RunResponse:
    """Resume a Temporal-native paused coding-team job by signaling CodingTeamWorkflow.

    Only jobs with a ``resume_token`` (pause_strategy=\"return\") can be resumed here.
    Thread-mode claim/spawn is removed.

    Authentication/authorization is enforced by the unified API security gateway
    in front of all team mounts; like every other coding-team route, this
    endpoint assumes that perimeter.

    Preconditions:
        - Job exists, is not terminal, has ``resume_token``, and ``status`` is
          ``waiting_for_user``.
    Postconditions:
        - Raises 404 / 400 as documented; on success delivers ``submit_answers`` to
          ``coding_team-{job_id}`` and returns ``\"Job resumed.\"``.
    """
    data = _main.get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Job not found")
    if hitl.is_terminal(data):
        raise HTTPException(
            status_code=400,
            detail=f"Job is {data.get('status', 'terminal')} and cannot be resumed.",
        )
    resume_token = data.get("resume_token")
    if not resume_token:
        raise HTTPException(
            status_code=400,
            detail=(
                "Job has no resume_token; only a Temporal-native paused job "
                "(waiting_for_user with resume_token) can be resumed."
            ),
        )
    if data.get("status") != hitl.WAITING_STATUS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Job is {data.get('status', 'in an unknown state')}, not paused waiting for "
                "answers; only a paused (waiting_for_user) job can be resumed."
            ),
        )
    signal_workflow_sync(
        f"{WORKFLOW_ID_PREFIX}{job_id}",
        "submit_answers",
        {
            "resume_token": resume_token,
            "answers": data.get("submitted_answers") or [],
        },
    )
    return RunResponse(job_id=job_id, status="running", message="Job resumed.")
