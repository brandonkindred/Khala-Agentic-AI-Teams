"""Blogging API — human-in-the-loop collaboration: title selection/rating, story
elicitation, Q&A answers, and draft feedback."""

from __future__ import annotations

import logging
from typing import Any, Dict

from agents.blogging.api.dependencies import get_job
from agents.blogging.api.models import (
    BlogAnswersRequest,
    BlogJobStatusResponse,
    DraftFeedbackRequest,
    RateTitlesRequest,
    SelectTitleRequest,
    StoryResponseRequest,
    _blog_job_dict_to_status_response,
)
from fastapi import APIRouter, Depends, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/job/{job_id}/select-title",
    response_model=BlogJobStatusResponse,
    summary="Submit title selection",
    description=(
        "Resume the pipeline after title selection. "
        "Sets waiting_for_title_selection=False and records the chosen title."
    ),
)
def select_title(
    job_id: str,
    request: SelectTitleRequest,
    _job: Dict[str, Any] = Depends(
        get_job(
            "submit_title_selection",
            waiting_for=(
                "waiting_for_title_selection",
                "Job is not currently waiting for title selection",
            ),
        )
    ),
) -> BlogJobStatusResponse:
    """Author submits their chosen title, resuming the pipeline."""
    from agents.blogging.api import main as _main

    if not request.title.strip():
        raise HTTPException(status_code=422, detail="title must not be empty")
    _main.submit_title_selection(job_id, request.title.strip())
    updated = _main.get_blog_job(job_id)
    if (
        updated is None
    ):  # pragma: no cover - race-only guard against a job vanishing between submit and re-read; not reachable in unit tests.
        raise HTTPException(status_code=500, detail="Job not found after title selection")
    return _blog_job_dict_to_status_response(updated, job_id)


@router.post(
    "/job/{job_id}/rate-titles",
    response_model=BlogJobStatusResponse,
    summary="Rate title candidates",
    description=(
        "Rate each title as dislike, like, or love. "
        "If any title is loved, it becomes the selected title. "
        "Otherwise the pipeline generates new candidates based on the feedback."
    ),
)
def rate_titles(
    job_id: str,
    request: RateTitlesRequest,
    _job: Dict[str, Any] = Depends(
        get_job(
            "submit_title_ratings",
            waiting_for=(
                "waiting_for_title_selection",
                "Job is not currently waiting for title selection",
            ),
        )
    ),
) -> BlogJobStatusResponse:
    """Submit title ratings. Love = select that title. Like/Dislike = generate more."""
    from agents.blogging.api import main as _main

    if not request.ratings:
        raise HTTPException(status_code=422, detail="At least one rating is required")
    for r in request.ratings:
        if r.rating not in ("dislike", "like", "love"):
            raise HTTPException(status_code=422, detail=f"Invalid rating: {r.rating}")

    ratings_dicts = [{"title": r.title, "rating": r.rating} for r in request.ratings]
    _main.submit_title_ratings(job_id, ratings_dicts)

    updated = _main.get_blog_job(job_id)
    if (
        updated is None
    ):  # pragma: no cover - race-only guard against a job vanishing between submit and re-read; not reachable in unit tests.
        raise HTTPException(status_code=500, detail="Job not found after rating submission")
    return _blog_job_dict_to_status_response(updated, job_id)


@router.post(
    "/job/{job_id}/story-response",
    response_model=BlogJobStatusResponse,
    summary="Submit story elicitation response",
    description=(
        "Send a message in the ghost writer story elicitation conversation. "
        "Clears waiting_for_story_input and appends the message to story_chat_history."
    ),
)
def story_response(
    job_id: str,
    request: StoryResponseRequest,
    _job: Dict[str, Any] = Depends(
        get_job(
            "submit_story_user_message",
            waiting_for=(
                "waiting_for_story_input",
                "Job is not currently waiting for a story response",
            ),
        )
    ),
) -> BlogJobStatusResponse:
    """Author submits a message in the story elicitation chat."""
    from agents.blogging.api import main as _main

    if not request.message.strip():
        raise HTTPException(status_code=422, detail="message must not be empty")
    _main.submit_story_user_message(job_id, request.message.strip())
    # Notify the ghost writer's event subscription so it wakes immediately
    try:
        from agents.blogging.shared.job_event_bus import publish

        publish(job_id, {"story_response_received": True}, event_type="story_update")
    except Exception:  # pragma: no cover - defensive guard around event bus; failures here must not break the API response.
        pass  # event bus is optional — polling fallback still works
    updated = _main.get_blog_job(job_id)
    if (
        updated is None
    ):  # pragma: no cover - race-only guard against a job vanishing between submit and re-read; not reachable in unit tests.
        raise HTTPException(status_code=500, detail="Job not found after story response")
    return _blog_job_dict_to_status_response(updated, job_id)


@router.post(
    "/job/{job_id}/skip-story-gap",
    response_model=BlogJobStatusResponse,
    summary="Skip the current story gap",
    description=(
        "Skip the current story elicitation gap and advance to the next one. "
        "Increments current_story_gap_index and clears waiting_for_story_input."
    ),
)
def skip_story_gap(
    job_id: str,
    _job: Dict[str, Any] = Depends(
        get_job(
            "skip_current_story_gap",
            waiting_for=(
                "waiting_for_story_input",
                "Job is not currently waiting for a story response",
            ),
        )
    ),
) -> BlogJobStatusResponse:
    """Author skips the current story gap."""
    from agents.blogging.api import main as _main

    logger.info("Skipping current story gap for job %s", job_id)
    _main.skip_current_story_gap(job_id)
    # Notify the ghost writer's event subscription so it wakes immediately
    try:
        from agents.blogging.shared.job_event_bus import publish

        publish(job_id, {"story_gap_skipped": True}, event_type="story_update")
    except Exception:  # pragma: no cover - defensive guard around event bus; failures here must not break the API response.
        pass  # event bus is optional — polling fallback still works
    updated = _main.get_blog_job(job_id)
    if (
        updated is None
    ):  # pragma: no cover - race-only guard against a job vanishing between submit and re-read; not reachable in unit tests.
        raise HTTPException(status_code=500, detail="Job not found after skip")
    return _blog_job_dict_to_status_response(updated, job_id)


@router.post(
    "/job/{job_id}/answers",
    response_model=BlogJobStatusResponse,
    summary="Submit answers to pending questions",
    description=(
        "Resume the pipeline after Q&A. Stores answers, clears pending_questions, "
        "and sets waiting_for_answers=False."
    ),
)
def submit_answers(
    job_id: str,
    request: BlogAnswersRequest,
    _job: Dict[str, Any] = Depends(
        get_job(
            "submit_blog_answers",
            waiting_for=(
                "waiting_for_answers",
                "Job is not currently waiting for answers",
            ),
        )
    ),
) -> BlogJobStatusResponse:
    """Author submits answers to pipeline Q&A questions."""
    from agents.blogging.api import main as _main

    _main.submit_blog_answers(job_id, request.answers)
    updated = _main.get_blog_job(job_id)
    if (
        updated is None
    ):  # pragma: no cover - race-only guard against a job vanishing between submit and re-read; not reachable in unit tests.
        raise HTTPException(status_code=500, detail="Job not found after answer submission")
    return _blog_job_dict_to_status_response(updated, job_id)


@router.post(
    "/job/{job_id}/draft-feedback",
    response_model=BlogJobStatusResponse,
    summary="Submit draft feedback or approval",
    description=(
        "Resume the pipeline after the editor reviews a draft. "
        "Sets waiting_for_draft_feedback=False and stores the feedback. "
        "When approved=true, the draft proceeds without further revision."
    ),
)
def draft_feedback(
    job_id: str,
    request: DraftFeedbackRequest,
    _job: Dict[str, Any] = Depends(
        get_job(
            "submit_draft_feedback",
            waiting_for=(
                "waiting_for_draft_feedback",
                "Job is not currently waiting for draft feedback",
            ),
        )
    ),
) -> BlogJobStatusResponse:
    """Editor submits feedback on a draft or approves it."""
    from agents.blogging.api import main as _main

    _main.submit_draft_feedback(job_id, request.feedback, request.approved)
    updated = _main.get_blog_job(job_id)
    if (
        updated is None
    ):  # pragma: no cover - race-only guard against a job vanishing between submit and re-read; not reachable in unit tests.
        raise HTTPException(status_code=500, detail="Job not found after feedback submission")
    return _blog_job_dict_to_status_response(updated, job_id)
