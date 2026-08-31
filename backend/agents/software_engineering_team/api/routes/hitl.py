"""SE team API — human-in-the-loop routes: submit answers and auto-answer for run-team pending questions."""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException

from planning_team.temporal.answer_signal import SUBMIT_PLANNING_ANSWERS_SIGNAL
from shared.hitl.validation import validate_answers
from shared.temporal.runner import signal_workflow_sync
from software_engineering_team.api.models import (
    AutoAnswerRequest,
    AutoAnswerResponse,
    JobStatusResponse,
    SubmitAnswersRequest,
)
from software_engineering_team.api.state import (
    _get_spec_content_for_job,
    _is_orchestrator_alive,
    _real_question_options,
    build_job_status_response,
)
from software_engineering_team.shared.job_store import (
    append_submitted_answers as store_append_submitted_answers,
)
from software_engineering_team.shared.job_store import (
    get_job,
    update_job,
)
from software_engineering_team.shared.job_store import submit_answers as store_submit_answers
from software_engineering_team.temporal.constants import WORKFLOW_ID_PREFIX_RUN_TEAM

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/run-team/{job_id}/answers",
    response_model=JobStatusResponse,
    summary="Submit answers to pending questions",
    description="Submit user answers to pending questions. The job will resume once all required questions are answered. "
    "Each answer can select a predefined option or provide custom 'other' text.",
)
def submit_pending_answers(job_id: str, request: SubmitAnswersRequest) -> JobStatusResponse:
    """Submit answers to a run-team job's pending questions and resume job execution.

    Two distinct pause mechanisms, told apart by whether the job record carries a
    ``resume_token`` (set only when ``plan_project_activity`` catches a
    ``PlanningAnswerPauseSignal`` — see ``temporal/activities.py``; never set by a
    thread-mode pause):

    - **Temporal-native pause** (``resume_token`` present): the client must echo the same
      ``resume_token`` it was given in the pause notification/status poll — a mismatch (or a
      missing one) raises 409, mirroring the coding team's identical contract
      (``coding_team_hitl.py``): without this check a client holding a stale token would get
      a 200 while ``RunTeamWorkflowV2``'s ``submit_planning_answers`` handler silently drops
      the mismatched signal, giving false confidence the answer landed. Once validated,
      answers are appended to ``submitted_answers`` (for audit/status-poll visibility only —
      resumption itself is driven entirely by the signal, not by re-reading the job record),
      then ``RunTeamWorkflowV2`` is signaled directly so its Planning-phase pause loop
      resumes with the resolved answers.
    - **Thread-mode pause** (``resume_token`` absent): unchanged existing behavior — answers
      are stored via ``store_submit_answers``, which clears ``waiting_for_answers`` so a
      still-alive blocked wait loop can proceed.

    Authentication/authorization is enforced by the unified API security gateway in front of
    all team mounts; like every other run-team route, this endpoint assumes that perimeter.
    """
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    # Reconciled validation lives in shared.hitl (the strict union of both teams' rules):
    # it adds the corrupted-record (500) and duplicate-answer (400) rejections SE's old
    # inline check lacked, and returns answer dicts carrying each question's text.
    answers_dicts = validate_answers(data, request)

    resume_token = data.get("resume_token")
    if resume_token:
        if request.resume_token != resume_token:
            raise HTTPException(
                status_code=409,
                detail="resume_token does not match this job's current pause; it is stale or "
                "answers a pause that already resolved.",
            )
        # Signal first: it is the durable, resuming action (the workflow's own signal
        # handler owns applying the answers). If this succeeds but the audit-trail append
        # below fails, the workflow has still resumed -- the more important half. The
        # reverse order would risk the opposite failure: answers already persisted (and
        # rejected as a duplicate by validate_answers on any retry) while the workflow,
        # never signaled, stays paused forever.
        signal_workflow_sync(
            f"{WORKFLOW_ID_PREFIX_RUN_TEAM}{job_id}",
            SUBMIT_PLANNING_ANSWERS_SIGNAL,
            {"resume_token": resume_token, "answers": answers_dicts},
        )
        store_append_submitted_answers(job_id, answers_dicts)
        return build_job_status_response(job_id, get_job(job_id))

    store_submit_answers(job_id, answers_dicts)

    # If the orchestrator thread is alive, its _wait_for_user_answers polling loop
    # will pick up the answers automatically (waiting_for_answers is now False).
    # If the thread is dead (server restarted), the job stays in running state
    # with answers stored — the user or UI should call POST /run-team/{job_id}/resume.
    if not _is_orchestrator_alive(job_id):
        logger.info(
            "Orchestrator thread for job %s is not running; answers stored. "
            "Call POST /run-team/%s/resume to restart the orchestrator.",
            job_id,
            job_id,
        )
        update_job(
            job_id,
            status_text="Answers received. Resume the job to continue processing.",
        )

    return build_job_status_response(job_id, get_job(job_id))


@router.post(
    "/run-team/{job_id}/auto-answer/{question_id}",
    response_model=AutoAnswerResponse,
    summary="Auto-answer a pending question for run-team job",
    description="Use LLM to automatically answer a pending question based on industry best practices. "
    "The answer is NOT automatically applied - review the response and submit via /answers endpoint.",
)
def auto_answer_run_team_question(
    job_id: str,
    question_id: str,
    request: Optional[AutoAnswerRequest] = None,
) -> AutoAnswerResponse:
    """Auto-answer a pending question using LLM analysis."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if data.get("job_type") not in (None, "run_team"):
        raise HTTPException(
            status_code=400,
            detail="This endpoint is only for run-team jobs.",
        )

    pending_questions = data.get("pending_questions", [])
    question_data = next((q for q in pending_questions if q.get("id") == question_id), None)
    if not question_data:
        raise HTTPException(
            status_code=404,
            detail=f"Question {question_id} not found in pending questions.",
        )

    real_options = _real_question_options(question_data)
    if not real_options:
        raise HTTPException(
            status_code=422,
            detail="This question has no selectable options. Provide a free-text answer via the /answers endpoint using the other_text field.",
        )

    spec_content = _get_spec_content_for_job(data)
    additional_context = request.spec_context if request else None

    try:  # pragma: no cover  # integration-only: runs PRA's LLM auto-answer pipeline
        from llm_service import get_client
        from software_engineering_team.product_requirements_analysis_agent import (
            get_auto_answer_for_job,
        )

        llm = get_client("backend")
        result = get_auto_answer_for_job(
            llm=llm,
            job_id=job_id,
            question_id=question_id,
            spec_content=spec_content,
            additional_context=additional_context,
        )

        if not result:
            raise HTTPException(
                status_code=500,
                detail="Auto-answer failed to produce a result.",
            )

        return AutoAnswerResponse(
            question_id=result.question_id,
            selected_option_id=result.selected_option_id,
            selected_answer=result.selected_answer,
            rationale=result.rationale,
            confidence=result.confidence,
            risks=result.risks,
            applied=False,
        )
    except (
        ImportError
    ) as e:  # pragma: no cover  # integration-only: paired with integration-only try block
        raise HTTPException(
            status_code=500,
            detail=f"Auto-answer module not available: {e}",
        )
    except (
        Exception
    ) as e:  # pragma: no cover  # integration-only: paired with integration-only try block
        logger.exception("Auto-answer failed")
        raise HTTPException(
            status_code=500,
            detail=f"Auto-answer failed: {e}",
        )
