"""FastAPI endpoints for running the branding strategy team."""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple
from uuid import uuid4

from fastapi import Body, FastAPI, HTTPException
from pydantic import BaseModel, Field

from branding_team.assistant import get_conversation_store
from branding_team.assistant.agent import BrandingAssistantAgent
from branding_team.assistant.store import _default_mission
from branding_team.db import get_db_path as _get_db_path
from branding_team.models import (
    Brand,
    BrandCheckRequest,
    BrandingMission,
    BrandPhase,
    Client,
    CompetitiveSnapshot,
    DesignAssetRequestResult,
    HumanReview,
    TeamOutput,
)
from branding_team.orchestrator import BrandingTeamOrchestrator
from branding_team.postgres import SCHEMA as BRANDING_POSTGRES_SCHEMA
from branding_team.store import get_default_store
from shared_observability import init_otel, instrument_fastapi_app

logger = logging.getLogger(__name__)

init_otel(service_name="branding-team", team_key="branding")


@asynccontextmanager
async def _lifespan(application: FastAPI):
    # Register Postgres schema (no-op when POSTGRES_HOST is unset).
    try:
        from shared_postgres import register_team_schemas

        register_team_schemas(BRANDING_POSTGRES_SCHEMA)
    except Exception:
        logger.exception("branding postgres schema registration failed")
    yield
    try:
        from shared_postgres import close_pool

        close_pool()
    except Exception:
        logger.warning("branding shared_postgres close_pool failed", exc_info=True)


app = FastAPI(title="Branding Team API", version="2.0.0", lifespan=_lifespan)
instrument_fastapi_app(app, team_key="branding")
branding_store = get_default_store()
orchestrator = BrandingTeamOrchestrator()
conversation_store = get_conversation_store()

# Public name so tests can patch 'branding_team.api.main.assistant_agent'.
assistant_agent: Optional[BrandingAssistantAgent] = None


def _get_assistant_agent() -> BrandingAssistantAgent:
    """Lazy-init the branding assistant so the app mounts even if llm_service is unavailable."""
    global assistant_agent
    if assistant_agent is None:
        try:
            assistant_agent = BrandingAssistantAgent()
        except Exception:
            raise HTTPException(
                status_code=503,
                detail="Branding assistant is temporarily unavailable. LLM service may not be configured.",
            )
    return assistant_agent


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CreateClientRequest(BaseModel):
    name: str = Field(..., min_length=1)
    contact_info: Optional[str] = None
    notes: Optional[str] = None


class CreateBrandRequest(BaseModel):
    company_name: str = Field(..., min_length=2)
    company_description: str = Field(..., min_length=10)
    target_audience: str = Field(..., min_length=3)
    name: Optional[str] = None
    values: List[str] = Field(default_factory=list)
    differentiators: List[str] = Field(default_factory=list)
    desired_voice: str = Field(default="clear, confident, human")
    existing_brand_material: List[str] = Field(default_factory=list)
    wiki_path: Optional[str] = None
    conversation_id: Optional[str] = None


class UpdateBrandRequest(BaseModel):
    company_name: Optional[str] = Field(None, min_length=2)
    company_description: Optional[str] = Field(None, min_length=10)
    target_audience: Optional[str] = Field(None, min_length=3)
    name: Optional[str] = Field(None, min_length=1)
    values: Optional[List[str]] = None
    differentiators: Optional[List[str]] = None
    desired_voice: Optional[str] = None
    existing_brand_material: Optional[List[str]] = None
    wiki_path: Optional[str] = None
    status: Optional[str] = None


class RunBrandRequest(BaseModel):
    human_approved: bool = True
    human_feedback: str = ""
    include_market_research: bool = False
    include_design_assets: bool = False
    brand_checks: List[BrandCheckRequest] = Field(default_factory=list)
    target_phase: Optional[str] = None


class RunBrandingTeamRequest(BaseModel):
    company_name: str = Field(..., min_length=2)
    company_description: str = Field(..., min_length=10)
    target_audience: str = Field(..., min_length=3)
    values: List[str] = Field(default_factory=list)
    differentiators: List[str] = Field(default_factory=list)
    desired_voice: str = Field(default="clear, confident, human")
    existing_brand_material: List[str] = Field(default_factory=list)
    wiki_path: Optional[str] = None
    brand_checks: List[BrandCheckRequest] = Field(default_factory=list)
    human_approved: bool = False
    human_feedback: str = ""
    client_id: Optional[str] = None
    brand_id: Optional[str] = None
    target_phase: Optional[str] = None


class BrandingQuestion(BaseModel):
    id: str
    question: str
    context: str
    target_field: str
    status: str = "open"
    answer: Optional[str] = None


class BrandingSessionResponse(BaseModel):
    session_id: str
    status: str
    current_phase: str = "strategic_core"
    mission: BrandingMission
    latest_output: TeamOutput
    open_questions: List[BrandingQuestion] = Field(default_factory=list)
    answered_questions: List[BrandingQuestion] = Field(default_factory=list)


class AnswerBrandingQuestionRequest(BaseModel):
    answer: str = Field(..., min_length=1)


# Conversation (chat) API models
class CreateConversationRequest(BaseModel):
    initial_message: Optional[str] = None
    brand_id: Optional[str] = None


class SendMessageRequest(BaseModel):
    message: str = Field(..., min_length=1)


class ConversationMessage(BaseModel):
    role: str = Field(..., pattern="^(user|assistant)$")
    content: str
    timestamp: str = ""


class ConversationStateResponse(BaseModel):
    conversation_id: str
    brand_id: Optional[str] = None
    messages: List[ConversationMessage] = Field(default_factory=list)
    mission: BrandingMission
    latest_output: Optional[TeamOutput] = None
    suggested_questions: List[str] = Field(default_factory=list)


class ConversationSummaryResponse(BaseModel):
    conversation_id: str
    brand_id: Optional[str] = None
    brand_name: Optional[str] = None
    created_at: str
    updated_at: str
    message_count: int


class AttachConversationBrandRequest(BaseModel):
    brand_id: str = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# Session store
# ---------------------------------------------------------------------------


@dataclass
class BrandingSession:
    mission: BrandingMission
    questions: List[BrandingQuestion]
    latest_output: TeamOutput


_SESSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS branding_sessions (
    session_id   TEXT PRIMARY KEY,
    session_json TEXT NOT NULL
);
"""


def _session_to_dict(session: BrandingSession) -> dict:
    return {
        "mission": session.mission.model_dump(),
        "questions": [q.model_dump() for q in session.questions],
        "latest_output": session.latest_output.model_dump(),
    }


def _session_from_dict(d: dict) -> BrandingSession:
    return BrandingSession(
        mission=BrandingMission.model_validate(d["mission"]),
        questions=[BrandingQuestion.model_validate(q) for q in d["questions"]],
        latest_output=TeamOutput.model_validate(d["latest_output"]),
    )


class BrandingSessionStore:
    """SQLite-backed session store — survives worker restarts and is shared
    across all worker processes via WAL-mode SQLite.

    Pass ``db_path=None`` (default) for an isolated in-memory database, which
    is useful when the class is instantiated directly in tests.
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._lock = threading.Lock()
        if db_path is None:
            self._file_path: Optional[str] = None
            self._mem_conn: Optional[sqlite3.Connection] = sqlite3.connect(
                ":memory:", check_same_thread=False
            )
            self._mem_conn.row_factory = sqlite3.Row
            self._mem_conn.executescript(_SESSION_SCHEMA)
            self._mem_conn.commit()
        else:
            self._file_path = str(db_path)
            self._mem_conn = None
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(self._file_path, timeout=15)
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.executescript(_SESSION_SCHEMA)
            _conn.commit()
            _conn.close()

    @contextlib.contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        if self._mem_conn is not None:
            with self._lock:
                self._mem_conn.row_factory = sqlite3.Row
                yield self._mem_conn
                self._mem_conn.commit()
        else:
            conn = sqlite3.connect(self._file_path, check_same_thread=False, timeout=15)  # type: ignore[arg-type]
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def create(
        self, mission: BrandingMission, latest_output: TeamOutput
    ) -> Tuple[str, BrandingSession]:
        questions = _build_open_questions(mission)
        session_id = str(uuid4())
        session = BrandingSession(mission=mission, questions=questions, latest_output=latest_output)
        with self._db() as conn:
            conn.execute(
                "INSERT INTO branding_sessions (session_id, session_json) VALUES (?, ?)",
                (session_id, json.dumps(_session_to_dict(session))),
            )
        return session_id, session

    def get(self, session_id: str) -> Optional[BrandingSession]:
        with self._db() as conn:
            row = conn.execute(
                "SELECT session_json FROM branding_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return _session_from_dict(json.loads(row[0]))

    def save(self, session_id: str, session: BrandingSession) -> None:
        """Persist mutations to an existing session."""
        with self._db() as conn:
            conn.execute(
                "UPDATE branding_sessions SET session_json = ? WHERE session_id = ?",
                (json.dumps(_session_to_dict(session)), session_id),
            )


session_store = BrandingSessionStore(db_path=_get_db_path())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_target_phase(raw: Optional[str]) -> Optional[BrandPhase]:
    """Parse a target_phase string into a BrandPhase enum, or None."""
    if not raw:
        return None
    try:
        return BrandPhase(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid target_phase: {raw}")


def _mission_has_brand_name(mission: BrandingMission) -> bool:
    """True if company_name is a real value (not a placeholder)."""
    placeholders = ("TBD", "To be discussed.", "—", "")
    return (mission.company_name or "").strip() not in placeholders


def _mission_has_minimal_required_fields(mission: BrandingMission) -> bool:
    """True if we have real company name, description, and target audience (not placeholders)."""
    placeholders = ("TBD", "To be discussed.", "—", "")
    name_ok = (mission.company_name or "").strip() not in placeholders
    desc_ok = (mission.company_description or "").strip() not in placeholders
    audience_ok = (mission.target_audience or "").strip() not in placeholders
    return name_ok and desc_ok and audience_ok


def _run_orchestrator_if_ready(mission: BrandingMission) -> Optional[TeamOutput]:
    """If mission has minimal required fields, run orchestrator and return TeamOutput; else return None."""
    if not _mission_has_minimal_required_fields(mission):
        return None
    return orchestrator.run(
        mission=mission,
        human_review=HumanReview(approved=False, feedback="Building brand from conversation."),
    )


def _brand_exists(brand_id: str) -> bool:
    for client in branding_store.list_clients():
        if branding_store.get_brand(client.id, brand_id):
            return True
    return False


def _build_open_questions(mission: BrandingMission) -> List[BrandingQuestion]:
    questions: List[BrandingQuestion] = []
    if not mission.values:
        questions.append(
            BrandingQuestion(
                id="core-values",
                question="What are the 3-5 core brand values we should optimize for?",
                context="These values are the foundation of Phase 1 (Strategic Core). They define behavioral expectations and drive all downstream brand decisions.",
                target_field="values",
            )
        )
    if not mission.differentiators:
        questions.append(
            BrandingQuestion(
                id="differentiators",
                question="What differentiators should the team emphasize against competitors?",
                context="Differentiation pillars are critical to Phase 1 (Strategic Core). They shape positioning, narrative, and competitive strategy.",
                target_field="differentiators",
            )
        )
    questions.append(
        BrandingQuestion(
            id="voice-approval",
            question="Do you approve the proposed brand voice, or what adjustment should be made?",
            context="Voice decisions bridge Phase 1 (Strategic Core) and Phase 2 (Narrative & Messaging). They must be locked before messaging work begins.",
            target_field="desired_voice",
        )
    )
    return questions


def _session_response(session_id: str, session: BrandingSession) -> BrandingSessionResponse:
    open_questions = [q for q in session.questions if q.status == "open"]
    answered_questions = [q for q in session.questions if q.status == "answered"]
    status = "awaiting_user_answers" if open_questions else "ready_for_rollout"
    current_phase = (
        session.latest_output.current_phase.value if session.latest_output else "strategic_core"
    )
    return BrandingSessionResponse(
        session_id=session_id,
        status=status,
        current_phase=current_phase,
        mission=session.mission,
        latest_output=session.latest_output,
        open_questions=open_questions,
        answered_questions=answered_questions,
    )


def _apply_answer(
    mission: BrandingMission, question: BrandingQuestion, answer: str
) -> BrandingMission:
    normalized = answer.strip()
    if question.target_field in {"values", "differentiators"}:
        entries = [item.strip() for item in normalized.split(",") if item.strip()]
        if question.target_field == "values":
            return mission.model_copy(update={"values": entries})
        return mission.model_copy(update={"differentiators": entries})
    if question.target_field == "desired_voice":
        return mission.model_copy(update={"desired_voice": normalized})
    return mission


# ---------------------------------------------------------------------------
# Client endpoints
# ---------------------------------------------------------------------------


@app.post("/clients", response_model=Client, status_code=201)
def create_client(payload: CreateClientRequest) -> Client:
    return branding_store.create_client(
        name=payload.name,
        contact_info=payload.contact_info,
        notes=payload.notes,
    )


@app.get("/clients", response_model=List[Client])
def list_clients() -> List[Client]:
    return branding_store.list_clients()


@app.get("/clients/{client_id}", response_model=Client)
def get_client(client_id: str) -> Client:
    client = branding_store.get_client(client_id)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    return client


# ---------------------------------------------------------------------------
# Brand CRUD endpoints
# ---------------------------------------------------------------------------


@app.get("/clients/{client_id}/brands", response_model=List[Brand])
def list_brands(client_id: str) -> List[Brand]:
    if not branding_store.get_client(client_id):
        raise HTTPException(status_code=404, detail="Client not found")
    return branding_store.list_brands_for_client(client_id)


@app.post("/clients/{client_id}/brands", response_model=Brand, status_code=201)
def create_brand(client_id: str, payload: CreateBrandRequest) -> Brand:
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

    brand = branding_store.create_brand(client_id=client_id, mission=mission, name=payload.name)
    if not brand:
        raise HTTPException(status_code=404, detail="Client not found")

    # Attach an existing conversation if provided, otherwise create a new one.
    existing_conv_id = (payload.conversation_id or "").strip() or None
    if existing_conv_id and conversation_store.get(existing_conv_id) is not None:
        existing_brand = conversation_store.get_conversation_brand_id(existing_conv_id)
        if existing_brand:
            raise HTTPException(
                status_code=409,
                detail="Conversation is already attached to another brand",
            )
        conversation_store.set_brand(existing_conv_id, brand.id)
        conversation_store.update_mission(existing_conv_id, mission)
        conv_id = existing_conv_id
    else:
        conv_id = conversation_store.create(brand_id=brand.id, mission=mission)
    brand = branding_store.update_brand(client_id, brand.id, conversation_id=conv_id)

    return brand


@app.get("/clients/{client_id}/brands/{brand_id}", response_model=Brand)
def get_brand(client_id: str, brand_id: str) -> Brand:
    brand = branding_store.get_brand(client_id, brand_id)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return brand


@app.put("/clients/{client_id}/brands/{brand_id}", response_model=Brand)
def update_brand(client_id: str, brand_id: str, payload: UpdateBrandRequest) -> Brand:
    brand = branding_store.get_brand(client_id, brand_id)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    mission = None
    if any(
        [
            payload.company_name is not None,
            payload.company_description is not None,
            payload.target_audience is not None,
            payload.values is not None,
            payload.differentiators is not None,
            payload.desired_voice is not None,
            payload.existing_brand_material is not None,
            payload.wiki_path is not None,
        ]
    ):
        mission = brand.mission.model_copy(
            update={
                k: v
                for k, v in {
                    "company_name": payload.company_name,
                    "company_description": payload.company_description,
                    "target_audience": payload.target_audience,
                    "values": payload.values,
                    "differentiators": payload.differentiators,
                    "desired_voice": payload.desired_voice,
                    "existing_brand_material": payload.existing_brand_material,
                    "wiki_path": payload.wiki_path,
                }.items()
                if v is not None
            }
        )
    from branding_team.models import BrandStatus

    status = None
    if payload.status is not None:
        try:
            status = BrandStatus(payload.status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {payload.status}")
    updated = branding_store.update_brand(
        client_id=client_id,
        brand_id=brand_id,
        mission=mission,
        status=status,
        name=payload.name,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Brand not found")
    return updated


@app.get(
    "/clients/{client_id}/brands/{brand_id}/conversation", response_model=ConversationStateResponse
)
def get_brand_conversation(client_id: str, brand_id: str) -> ConversationStateResponse:
    """Return the single conversation for a brand."""
    brand = branding_store.get_brand(client_id, brand_id)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    result = conversation_store.get_by_brand_id(brand_id)
    if not result:
        raise HTTPException(status_code=404, detail="Brand has no conversation")
    cid, messages, mission, latest_output = result
    return _conversation_to_response(cid, brand_id, messages, mission, latest_output, [])


# ---------------------------------------------------------------------------
# Brand run endpoints
# ---------------------------------------------------------------------------


@app.post("/clients/{client_id}/brands/{brand_id}/run", response_model=TeamOutput)
def run_brand(client_id: str, brand_id: str, payload: RunBrandRequest) -> TeamOutput:
    brand = branding_store.get_brand(client_id, brand_id)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    human_review = HumanReview(approved=payload.human_approved, feedback=payload.human_feedback)
    target_phase = _parse_target_phase(payload.target_phase)
    return orchestrator.run(
        mission=brand.mission,
        human_review=human_review,
        brand_checks=payload.brand_checks,
        store=branding_store,
        client_id=client_id,
        brand_id=brand_id,
        include_market_research=payload.include_market_research,
        include_design_assets=payload.include_design_assets,
        target_phase=target_phase,
    )


@app.post("/clients/{client_id}/brands/{brand_id}/run/{phase}", response_model=TeamOutput)
def run_brand_phase(
    client_id: str, brand_id: str, phase: str, payload: RunBrandRequest
) -> TeamOutput:
    """Run the branding pipeline up to a specific phase."""
    brand = branding_store.get_brand(client_id, brand_id)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    target_phase = _parse_target_phase(phase)
    if target_phase is None:
        raise HTTPException(status_code=400, detail=f"Invalid phase: {phase}")
    human_review = HumanReview(approved=payload.human_approved, feedback=payload.human_feedback)
    return orchestrator.run(
        mission=brand.mission,
        human_review=human_review,
        brand_checks=payload.brand_checks,
        store=branding_store,
        client_id=client_id,
        brand_id=brand_id,
        include_market_research=payload.include_market_research,
        include_design_assets=payload.include_design_assets,
        target_phase=target_phase,
    )


# ---------------------------------------------------------------------------
# Integration endpoints
# ---------------------------------------------------------------------------


@app.post(
    "/clients/{client_id}/brands/{brand_id}/request-market-research",
    response_model=CompetitiveSnapshot,
)
def request_market_research_for_brand(client_id: str, brand_id: str) -> CompetitiveSnapshot:
    brand = branding_store.get_brand(client_id, brand_id)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        from branding_team.adapters.market_research import request_market_research

        snapshot = request_market_research(brand.mission)
    except Exception:
        raise HTTPException(status_code=503, detail="Market research service unavailable")
    if not snapshot:
        raise HTTPException(status_code=503, detail="Market research service unavailable")
    return snapshot


@app.post(
    "/clients/{client_id}/brands/{brand_id}/request-design-assets",
    response_model=DesignAssetRequestResult,
)
def request_design_assets_for_brand(client_id: str, brand_id: str) -> DesignAssetRequestResult:
    brand = branding_store.get_brand(client_id, brand_id)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    from branding_team.adapters.design_assets import request_design_assets

    codification = orchestrator.codifier.codify(brand.mission)
    return request_design_assets(codification, brand.mission.company_name)


# ---------------------------------------------------------------------------
# Legacy run endpoint
# ---------------------------------------------------------------------------


@app.post("/run", response_model=TeamOutput)
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
    store = branding_store if (payload.client_id and payload.brand_id) else None
    target_phase = _parse_target_phase(payload.target_phase)
    return orchestrator.run(
        mission=mission,
        human_review=human_review,
        brand_checks=payload.brand_checks,
        store=store,
        client_id=payload.client_id,
        brand_id=payload.brand_id,
        target_phase=target_phase,
    )


# ---------------------------------------------------------------------------
# Session endpoints
# ---------------------------------------------------------------------------


@app.post("/sessions", response_model=BrandingSessionResponse)
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
    target_phase = _parse_target_phase(payload.target_phase)
    output = orchestrator.run(
        mission=mission,
        human_review=HumanReview(approved=False, feedback="Interactive review started."),
        brand_checks=payload.brand_checks,
        target_phase=target_phase,
    )
    session_id, session = session_store.create(mission=mission, latest_output=output)
    return _session_response(session_id, session)


@app.get("/sessions/{session_id}", response_model=BrandingSessionResponse)
def get_branding_session(session_id: str) -> BrandingSessionResponse:
    session = session_store.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return _session_response(session_id, session)


@app.get("/sessions/{session_id}/questions", response_model=List[BrandingQuestion])
def get_branding_questions(session_id: str) -> List[BrandingQuestion]:
    session = session_store.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return [q for q in session.questions if q.status == "open"]


@app.post(
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
    human_review = HumanReview(
        approved=not open_questions,
        feedback="Answers applied and branding artifacts refreshed.",
    )
    session.latest_output = orchestrator.run(mission=session.mission, human_review=human_review)
    session_store.save(session_id, session)
    return _session_response(session_id, session)


# ---------------------------------------------------------------------------
# Conversation (chat) endpoints
# ---------------------------------------------------------------------------


def _conversation_to_response(
    conversation_id: str,
    brand_id: Optional[str],
    messages: list,
    mission: BrandingMission,
    latest_output: Optional[TeamOutput],
    suggested_questions: List[str],
) -> ConversationStateResponse:
    msg_list = [
        ConversationMessage(role=m.role, content=m.content, timestamp=m.timestamp) for m in messages
    ]
    return ConversationStateResponse(
        conversation_id=conversation_id,
        brand_id=brand_id,
        messages=msg_list,
        mission=mission,
        latest_output=latest_output,
        suggested_questions=suggested_questions or [],
    )


@app.post("/conversations", response_model=ConversationStateResponse)
def create_branding_conversation(
    body: Optional[CreateConversationRequest] = Body(default=None),
) -> ConversationStateResponse:
    req = body or CreateConversationRequest()
    brand_id = (req.brand_id or "").strip() or None
    if brand_id:
        if not _brand_exists(brand_id):
            raise HTTPException(status_code=404, detail="Brand not found")

    # Conversations are created unattached; auto-create-brand logic in
    # send_message will attach them once the mission has enough info.
    conversation_id = conversation_store.create(brand_id=brand_id)
    initial_message = (req.initial_message or "").strip()
    suggested_questions: List[str] = []

    if initial_message:
        conversation_store.append_message(conversation_id, "user", initial_message)
        messages, mission, _ = conversation_store.get(conversation_id) or (
            [],
            _default_mission(),
            None,
        )
        msg_pairs = [(m.role, m.content) for m in messages]
        reply, updated_mission, suggested_questions = _get_assistant_agent().respond(
            msg_pairs[:-1], mission, initial_message
        )
        conversation_store.update_mission(conversation_id, updated_mission)
        conversation_store.append_message(conversation_id, "assistant", reply)
        output = _run_orchestrator_if_ready(updated_mission)
        if output is not None:
            conversation_store.update_output(conversation_id, output)

        # Auto-create a brand when the user provided enough info in the initial message.
        if not brand_id and _mission_has_brand_name(updated_mission):
            client_id = _ensure_default_client()
            brand = branding_store.create_brand(
                client_id=client_id,
                mission=updated_mission,
                name=updated_mission.company_name,
            )
            if brand:
                conversation_store.set_brand(conversation_id, brand.id)
                branding_store.update_brand(client_id, brand.id, conversation_id=conversation_id)
                if output:
                    branding_store.append_brand_version(client_id, brand.id, output)
                brand_id = brand.id
                logger.info(
                    "Auto-created brand %s from initial message in conversation %s",
                    brand.id,
                    conversation_id,
                )

        messages, mission, latest_output = conversation_store.get(conversation_id) or (
            [],
            updated_mission,
            output,
        )
    else:
        reply = (
            "Hi! I'm your branding lead. I'll guide you through our 5-phase brand development framework — "
            "starting with your Strategic Core. Let's begin: what's your company or product name?"
        )
        conversation_store.append_message(conversation_id, "assistant", reply)
        suggested_questions = [
            "What's your company name?",
            "Who is your target audience?",
            "What does your company do?",
        ]
        messages, mission, latest_output = conversation_store.get(conversation_id) or (
            [],
            _default_mission(),
            None,
        )

    return _conversation_to_response(
        conversation_id, brand_id, messages, mission, latest_output, suggested_questions
    )


def _ensure_default_client() -> str:
    """Find or create a default workspace client; return client_id."""
    clients = branding_store.list_clients()
    if clients:
        return clients[0].id
    client = branding_store.create_client(name="My brands")
    return client.id


@app.post("/conversations/{conversation_id}/messages", response_model=ConversationStateResponse)
def send_branding_conversation_message(
    conversation_id: str, payload: SendMessageRequest
) -> ConversationStateResponse:
    state = conversation_store.get(conversation_id)
    if not state:
        raise HTTPException(status_code=404, detail="Conversation not found")
    messages, mission, _ = state
    brand_id = conversation_store.get_conversation_brand_id(conversation_id)
    conversation_store.append_message(conversation_id, "user", payload.message)
    msg_pairs = [(m.role, m.content) for m in messages]
    msg_pairs.append(("user", payload.message))
    reply, updated_mission, suggested_questions = _get_assistant_agent().respond(
        msg_pairs[:-1], mission, payload.message
    )
    conversation_store.update_mission(conversation_id, updated_mission)
    conversation_store.append_message(conversation_id, "assistant", reply)
    output = _run_orchestrator_if_ready(updated_mission)
    if output is not None:
        conversation_store.update_output(conversation_id, output)

    # Auto-create a brand when the user has provided at least a company name and conversation is unattached.
    if not brand_id and _mission_has_brand_name(updated_mission):
        client_id = _ensure_default_client()
        brand = branding_store.create_brand(
            client_id=client_id,
            mission=updated_mission,
            name=updated_mission.company_name,
        )
        if brand:
            conversation_store.set_brand(conversation_id, brand.id)
            branding_store.update_brand(client_id, brand.id, conversation_id=conversation_id)
            if output:
                branding_store.append_brand_version(client_id, brand.id, output)
            brand_id = brand.id
            logger.info("Auto-created brand %s from conversation %s", brand.id, conversation_id)

    messages, mission, latest_output = conversation_store.get(conversation_id) or (
        [],
        updated_mission,
        output,
    )
    return _conversation_to_response(
        conversation_id, brand_id, messages, mission, latest_output, suggested_questions
    )


@app.get("/conversations/{conversation_id}", response_model=ConversationStateResponse)
def get_branding_conversation(conversation_id: str) -> ConversationStateResponse:
    state = conversation_store.get(conversation_id)
    if not state:
        raise HTTPException(status_code=404, detail="Conversation not found")
    messages, mission, latest_output = state
    brand_id = conversation_store.get_conversation_brand_id(conversation_id)
    return _conversation_to_response(
        conversation_id, brand_id, messages, mission, latest_output, []
    )


@app.get("/conversations", response_model=List[ConversationSummaryResponse])
def list_branding_conversations(
    brand_id: Optional[str] = None,
) -> List[ConversationSummaryResponse]:
    summaries = conversation_store.list_conversations(brand_id=brand_id)
    brand_names: Dict[str, str] = {}
    for client in branding_store.list_clients():
        for brand in branding_store.list_brands_for_client(client.id):
            brand_names[brand.id] = brand.name
    return [
        ConversationSummaryResponse(
            conversation_id=s.conversation_id,
            brand_id=s.brand_id,
            brand_name=brand_names.get(s.brand_id) if s.brand_id else None,
            created_at=s.created_at,
            updated_at=s.updated_at,
            message_count=s.message_count,
        )
        for s in summaries
    ]


@app.post("/conversations/{conversation_id}/brand", response_model=ConversationStateResponse)
def attach_conversation_to_brand(
    conversation_id: str, payload: AttachConversationBrandRequest
) -> ConversationStateResponse:
    brand_id = payload.brand_id.strip()
    if not _brand_exists(brand_id):
        raise HTTPException(status_code=404, detail="Brand not found")
    state = conversation_store.get(conversation_id)
    if not state:
        raise HTTPException(status_code=404, detail="Conversation not found")
    conversation_store.set_brand(conversation_id, brand_id)
    messages, mission, latest_output = state
    return _conversation_to_response(
        conversation_id, brand_id, messages, mission, latest_output, []
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}
