"""FastAPI endpoints for running the branding strategy team."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from branding_team.models import BrandCheckRequest, BrandingMission, HumanReview, TeamOutput
from branding_team.orchestrator import BrandingTeamOrchestrator

app = FastAPI(title="Branding Team API", version="1.0.0")


class RunBrandingTeamRequest(BaseModel):
    company_name: str = Field(..., min_length=2)
    company_description: str = Field(..., min_length=10)
    target_audience: str = Field(..., min_length=3)
    values: list[str] = Field(default_factory=list)
    differentiators: list[str] = Field(default_factory=list)
    desired_voice: str = Field(default="clear, confident, human")
    existing_brand_material: list[str] = Field(default_factory=list)
    wiki_path: str | None = None
    brand_checks: list[BrandCheckRequest] = Field(default_factory=list)
    human_approved: bool = False
    human_feedback: str = ""


class BrandingQuestion(BaseModel):
    id: str
    question: str
    context: str
    target_field: str
    status: str = "open"
    answer: str | None = None


class BrandingSessionResponse(BaseModel):
    session_id: str
    status: str
    mission: BrandingMission
    latest_output: TeamOutput
    open_questions: list[BrandingQuestion] = Field(default_factory=list)
    answered_questions: list[BrandingQuestion] = Field(default_factory=list)


class AnswerBrandingQuestionRequest(BaseModel):
    answer: str = Field(..., min_length=1)


@dataclass
class BrandingSession:
    mission: BrandingMission
    questions: list[BrandingQuestion]
    latest_output: TeamOutput


class BrandingSessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, BrandingSession] = {}
        self._lock = Lock()

    def create(self, mission: BrandingMission, latest_output: TeamOutput) -> tuple[str, BrandingSession]:
        questions = _build_open_questions(mission)
        session_id = str(uuid4())
        session = BrandingSession(mission=mission, questions=questions, latest_output=latest_output)
        with self._lock:
            self._sessions[session_id] = session
        return session_id, session

    def get(self, session_id: str) -> BrandingSession | None:
        with self._lock:
            return self._sessions.get(session_id)


orchestrator = BrandingTeamOrchestrator()
session_store = BrandingSessionStore()


def _build_open_questions(mission: BrandingMission) -> list[BrandingQuestion]:
    questions: list[BrandingQuestion] = []
    if not mission.values:
        questions.append(
            BrandingQuestion(
                id="core-values",
                question="What are the 3-5 core brand values we should optimize for?",
                context="These values are used in codification, compliance checks, and writing guidelines.",
                target_field="values",
            )
        )
    if not mission.differentiators:
        questions.append(
            BrandingQuestion(
                id="differentiators",
                question="What differentiators should the team emphasize against competitors?",
                context="Differentiators influence positioning, narrative pillars, and asset reviews.",
                target_field="differentiators",
            )
        )
    questions.append(
        BrandingQuestion(
            id="voice-approval",
            question="Do you approve the proposed brand voice, or what adjustment should be made?",
            context="Voice decisions are applied to writing guidelines and future content assets.",
            target_field="desired_voice",
        )
    )
    return questions


def _session_response(session_id: str, session: BrandingSession) -> BrandingSessionResponse:
    open_questions = [q for q in session.questions if q.status == "open"]
    answered_questions = [q for q in session.questions if q.status == "answered"]
    status = "awaiting_user_answers" if open_questions else "ready_for_rollout"
    return BrandingSessionResponse(
        session_id=session_id,
        status=status,
        mission=session.mission,
        latest_output=session.latest_output,
        open_questions=open_questions,
        answered_questions=answered_questions,
    )


def _apply_answer(mission: BrandingMission, question: BrandingQuestion, answer: str) -> BrandingMission:
    normalized = answer.strip()
    if question.target_field in {"values", "differentiators"}:
        entries = [item.strip() for item in normalized.split(",") if item.strip()]
        if question.target_field == "values":
            return mission.model_copy(update={"values": entries})
        return mission.model_copy(update={"differentiators": entries})
    if question.target_field == "desired_voice":
        return mission.model_copy(update={"desired_voice": normalized})
    return mission


@app.post("/branding/run", response_model=TeamOutput)
def run_branding_team(payload: RunBrandingTeamRequest) -> TeamOutput:
    mission = BrandingMission(
        company_name=payload.company_name,
        company_description=payload.company_description,
        target_audience=payload.target_audience,
        values=payload.values,
        differentiators=payload.differentiators,
        desired_voice=payload.desired_voice,
        existing_brand_material=payload.existing_brand_material,
        wiki_path=payload.wiki_path,
    )
    human_review = HumanReview(approved=payload.human_approved, feedback=payload.human_feedback)
    return orchestrator.run(mission=mission, human_review=human_review, brand_checks=payload.brand_checks)


@app.post("/branding/sessions", response_model=BrandingSessionResponse)
def create_branding_session(payload: RunBrandingTeamRequest) -> BrandingSessionResponse:
    mission = BrandingMission(
        company_name=payload.company_name,
        company_description=payload.company_description,
        target_audience=payload.target_audience,
        values=payload.values,
        differentiators=payload.differentiators,
        desired_voice=payload.desired_voice,
        existing_brand_material=payload.existing_brand_material,
        wiki_path=payload.wiki_path,
    )
    output = orchestrator.run(
        mission=mission,
        human_review=HumanReview(approved=False, feedback="Interactive review started."),
        brand_checks=payload.brand_checks,
    )
    session_id, session = session_store.create(mission=mission, latest_output=output)
    return _session_response(session_id, session)


@app.get("/branding/sessions/{session_id}", response_model=BrandingSessionResponse)
def get_branding_session(session_id: str) -> BrandingSessionResponse:
    session = session_store.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return _session_response(session_id, session)


@app.get("/branding/sessions/{session_id}/questions", response_model=list[BrandingQuestion])
def get_branding_questions(session_id: str) -> list[BrandingQuestion]:
    session = session_store.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return [q for q in session.questions if q.status == "open"]


@app.post("/branding/sessions/{session_id}/questions/{question_id}/answer", response_model=BrandingSessionResponse)
def answer_branding_question(
    session_id: str,
    question_id: str,
    payload: AnswerBrandingQuestionRequest,
) -> BrandingSessionResponse:
    session = session_store.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    question = next((q for q in session.questions if q.id == question_id and q.status == "open"), None)
    if not question:
        raise HTTPException(status_code=404, detail="Open question not found")

    question.status = "answered"
    question.answer = payload.answer.strip()
    session.mission = _apply_answer(session.mission, question, payload.answer)

    open_questions = [q for q in session.questions if q.status == "open"]
    human_review = HumanReview(
        approved=not open_questions,
        feedback="Answers applied and branding artifacts refreshed.",
    )
    session.latest_output = orchestrator.run(mission=session.mission, human_review=human_review)
    return _session_response(session_id, session)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
