"""SE team API — human-in-the-loop routes: submit answers and auto-answer for run-team pending questions.

Route handlers register on a module-local ``APIRouter`` that ``main`` mounts with
``app.include_router``; absolute paths are unchanged. Monkeypatched collaborators
(background runners, ``SUPERVISOR_LOG_DIR``) are dereferenced through the ``main``
module object at call time so ``monkeypatch.setattr(main, ...)`` still takes effect.
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException

from software_engineering_team.api.models import (
    AutoAnswerRequest,
    AutoAnswerResponse,
    FailedTaskDetail,
    JobStatusResponse,
    PendingQuestion,
    SubmitAnswersRequest,
    TaskStateEntry,
    TeamProgressEntry,
)
from software_engineering_team.api.state import (
    _get_spec_content_for_job,
    _is_orchestrator_alive,
    _real_question_options,
)
from software_engineering_team.shared.job_store import (
    get_job,
    update_job,
)
from software_engineering_team.shared.job_store import submit_answers as store_submit_answers

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
    """Submit answers to pending questions and resume job execution."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if not data.get("waiting_for_answers"):
        raise HTTPException(
            status_code=400,
            detail="Job is not waiting for answers.",
        )

    pending_questions = data.get("pending_questions", [])
    if not pending_questions:
        raise HTTPException(status_code=400, detail="No pending questions to answer.")

    pending_ids = {q["id"] for q in pending_questions}
    required_ids = {q["id"] for q in pending_questions if q.get("required", True)}
    answered_ids = {a.question_id for a in request.answers}

    missing_required = required_ids - answered_ids
    if missing_required:
        raise HTTPException(
            status_code=400,
            detail=f"Missing answers for required questions: {', '.join(sorted(missing_required))}",
        )

    invalid_ids = answered_ids - pending_ids
    if invalid_ids:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown question IDs: {', '.join(sorted(invalid_ids))}",
        )

    for answer in request.answers:
        other_text = (answer.other_text or "").strip()
        q = next((q for q in pending_questions if q["id"] == answer.question_id), None)
        question_options = {o.get("id") for o in (q.get("options") or [])} if q else set()

        if answer.selected_option_id == "other":
            if not other_text:
                raise HTTPException(
                    status_code=400,
                    detail=f"Question {answer.question_id}: 'other' selected but no text provided.",
                )
        elif answer.selected_option_id:
            if answer.selected_option_id not in question_options:
                raise HTTPException(
                    status_code=400,
                    detail=f"Question {answer.question_id}: unknown option '{answer.selected_option_id}'.",
                )
        elif not other_text:
            raise HTTPException(
                status_code=400,
                detail=f"Question {answer.question_id}: no option selected and no text provided.",
            )

    answers_dicts = [
        {
            "question_id": a.question_id,
            "selected_option_id": a.selected_option_id,
            "other_text": a.other_text,
        }
        for a in request.answers
    ]
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

    updated_data = get_job(job_id)
    return JobStatusResponse(
        job_id=job_id,
        status=updated_data.get("status", "running"),
        repo_path=updated_data.get("repo_path"),
        requirements_title=updated_data.get("requirements_title"),
        architecture_overview=updated_data.get("architecture_overview"),
        current_task=updated_data.get("current_task"),
        status_text=updated_data.get("status_text"),
        task_results=updated_data.get("task_results", []),
        task_ids=updated_data.get("execution_order", []),
        progress=updated_data.get("progress"),
        error=updated_data.get("error"),
        failed_tasks=[FailedTaskDetail(**ft) for ft in updated_data.get("failed_tasks", [])],
        phase=updated_data.get("phase"),
        task_states={
            k: TaskStateEntry(**v) for k, v in (updated_data.get("task_states") or {}).items()
        }
        if updated_data.get("task_states")
        else None,
        team_progress={
            k: TeamProgressEntry(**v) for k, v in (updated_data.get("team_progress") or {}).items()
        }
        if updated_data.get("team_progress")
        else None,
        pending_questions=[PendingQuestion(**q) for q in updated_data.get("pending_questions", [])],
        waiting_for_answers=updated_data.get("waiting_for_answers", False),
        planning_subprocess=updated_data.get("planning_subprocess"),
        planning_completed_phases=updated_data.get("planning_completed_phases") or [],
        analysis_subprocess=updated_data.get("analysis_subprocess"),
        analysis_completed_phases=updated_data.get("analysis_completed_phases") or [],
        planning_hierarchy=updated_data.get("planning_hierarchy"),
    )


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
            detail="This endpoint is only for run-team jobs. Use /planning-v2/{job_id}/auto-answer/{question_id} for planning-v2 jobs.",
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
        from product_requirements_analysis_agent import get_auto_answer_for_job

        from llm_service import get_client

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
