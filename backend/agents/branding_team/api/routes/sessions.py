"""Branding API — direct synchronous run + interactive review-session endpoints."""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, HTTPException

from branding_team.api import main as _main
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
    mission = _mission_from_payload(payload)
    human_review = HumanReview(approved=payload.human_approved, feedback=payload.human_feedback)
    store = _main.branding_store if (payload.client_id and payload.brand_id) else None
    target_phase = _parse_target_phase(payload.target_phase)
    return _main.orchestrator.run(
        mission=mission,
        human_review=human_review,
        brand_checks=payload.brand_checks,
        store=store,
        client_id=payload.client_id,
        brand_id=payload.brand_id,
        target_phase=target_phase,
    )


@router.post("/sessions", response_model=BrandingSessionResponse)
def create_branding_session(payload: RunBrandingTeamRequest) -> BrandingSessionResponse:
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
    session = session_store.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return _session_response(session_id, session)


@router.get("/sessions/{session_id}/questions", response_model=List[BrandingQuestion])
def get_branding_questions(session_id: str) -> List[BrandingQuestion]:
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
