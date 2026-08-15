"""Branding API — direct synchronous run + interactive review-session endpoints.

``main`` is imported inside each handler, not at module scope: ``main`` mounts
this router at the bottom of its own import (after ``app`` and its globals are
defined), so a module-scope ``from branding_team.api import main`` here would
form a load-time cycle — this module would be re-entered by main before
``router`` (line below) is even defined. The function-local import keeps this
router importable in any order.
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, HTTPException

from branding_team.api.models import (
    AnswerBrandingQuestionRequest,
    BrandingQuestion,
    BrandingSessionResponse,
    RunBrandingTeamRequest,
)
from branding_team.api.state import (
    _apply_answer,
    _mission_from_payload,
    _parse_target_phase,
    _session_response,
    session_store,
)
from branding_team.models import HumanReview, TeamOutput

router = APIRouter()


@router.post("/run", response_model=TeamOutput)
def run_branding_team(payload: RunBrandingTeamRequest) -> TeamOutput:
    """Run the branding pipeline synchronously and return its ``TeamOutput``.

    Preconditions:
        ``payload`` is a validated ``RunBrandingTeamRequest``.
    Postconditions:
        Returns the assembled ``TeamOutput`` on success.
        When persistence fails because the brand row disappeared mid-run
        (``orchestrator.run`` raises ``BrandVersionAppendConflict``), maps
        that to HTTP 409 instead of an unhandled 500. Other ``RuntimeError``
        values (e.g. LLM/provider failures) are not remapped — they keep the
        server's default 500 handling rather than being treated as conflicts.
    """
    from branding_team.api import main as _main
    from branding_team.store import BrandVersionAppendConflict

    mission = _mission_from_payload(payload)
    human_review = HumanReview(approved=payload.human_approved, feedback=payload.human_feedback)
    store = _main.branding_store if (payload.client_id and payload.brand_id) else None
    target_phase = _parse_target_phase(payload.target_phase)
    try:
        return _main.orchestrator.run(
            mission=mission,
            human_review=human_review,
            brand_checks=payload.brand_checks,
            store=store,
            client_id=payload.client_id,
            brand_id=payload.brand_id,
            target_phase=target_phase,
        )
    except BrandVersionAppendConflict as exc:
        # Brand deleted between resolve and append_brand_version — controlled
        # conflict response rather than an unhandled 500.
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/sessions", response_model=BrandingSessionResponse)
def create_branding_session(payload: RunBrandingTeamRequest) -> BrandingSessionResponse:
    """Start an interactive review session from an initial pipeline run.

    Runs the orchestrator with ``HumanReview(approved=False)`` so the pipeline
    produces a first draft and surfaces open review questions rather than
    finalizing.

    Preconditions:
        ``payload`` is a validated ``RunBrandingTeamRequest``.
        ``payload.target_phase`` is either unset or a recognized phase name.
    Postconditions:
        Runs the pipeline once, persists the resulting output under a new session
        via ``session_store.create``, and returns that session's
        ``BrandingSessionResponse`` (including any open questions).
    """
    from branding_team.api import main as _main

    mission = _mission_from_payload(payload)
    target_phase = _parse_target_phase(payload.target_phase)
    output = _main.orchestrator.run(
        mission=mission,
        human_review=HumanReview(approved=False, feedback="Interactive review started."),
        brand_checks=payload.brand_checks,
        target_phase=target_phase,
    )
    session_id, session = session_store.create(mission=mission, latest_output=output)
    return _session_response(session_id, session)


@router.get("/sessions/{session_id}", response_model=BrandingSessionResponse)
def get_branding_session(session_id: str) -> BrandingSessionResponse:
    """Fetch an interactive review session by id.

    Preconditions:
        ``session_id`` is a non-empty path string.
    Postconditions:
        Returns the session's ``BrandingSessionResponse``. Raises 404 "Session not
        found" when no such session exists.
    """
    session = session_store.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return _session_response(session_id, session)


@router.get("/sessions/{session_id}/questions", response_model=List[BrandingQuestion])
def get_branding_questions(session_id: str) -> List[BrandingQuestion]:
    """List the still-open review questions for a session.

    Preconditions:
        ``session_id`` is a non-empty path string.
    Postconditions:
        Returns only the session's questions whose ``status == "open"`` (a
        possibly empty list; answered questions are excluded). Raises 404
        "Session not found" when the session is unknown.
    """
    session = session_store.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return [q for q in session.questions if q.status == "open"]


@router.post(
    "/sessions/{session_id}/questions/{question_id}/answer", response_model=BrandingSessionResponse
)
def answer_branding_question(
    session_id: str,
    question_id: str,
    payload: AnswerBrandingQuestionRequest,
) -> BrandingSessionResponse:
    """Record an answer to an open review question and refresh the session.

    Applies the answer to the session mission and, to avoid wasted work,
    debounces regeneration: the full ~40-agent pipeline is re-run exactly once —
    only when answering leaves no open questions remaining — since answers only
    refine Phase 1 inputs that would be rebuilt again on the next answer.

    Preconditions:
        ``session_id`` and ``question_id`` are non-empty path strings; ``payload``
        is a validated ``AnswerBrandingQuestionRequest``.
    Postconditions:
        Marks the question answered, applies the answer to the mission via
        ``_apply_answer``, and persists the session. Re-runs the pipeline with
        ``HumanReview(approved=True)`` and updates ``latest_output`` only when the
        answered question was the last open one. Returns the updated
        ``BrandingSessionResponse``. Raises 404 "Session not found" when the
        session is unknown and 404 "Open question not found" when no open question
        matches ``question_id``.
    """
    from branding_team.api import main as _main

    session = session_store.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    question = next(
        (q for q in session.questions if q.id == question_id and q.status == "open"), None
    )
    if not question:
        raise HTTPException(status_code=404, detail="Open question not found")

    question.status = "answered"
    question.answer = payload.answer.strip()
    session.mission = _apply_answer(session.mission, question, payload.answer)

    open_questions = [q for q in session.questions if q.status == "open"]
    # Debounce regeneration. Answers only refine Phase 1 inputs (values,
    # differentiators, voice), and any artifacts rebuilt now would be rebuilt
    # again on the next answer. So while questions remain we keep the existing
    # artifacts untouched and regenerate the full ~40-agent pipeline exactly
    # once — when the final question is answered.
    if not open_questions:
        human_review = HumanReview(
            approved=True,
            feedback="Answers applied and branding artifacts refreshed.",
        )
        session.latest_output = _main.orchestrator.run(
            mission=session.mission, human_review=human_review
        )
    session_store.save(session_id, session)
    return _session_response(session_id, session)
