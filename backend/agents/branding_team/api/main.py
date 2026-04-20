"""FastAPI endpoints for running the branding strategy team."""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import Body, FastAPI, HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Json
from pydantic import BaseModel, Field

from branding_team.assistant import get_conversation_store
from branding_team.assistant.agent import BrandingAssistantAgent
from branding_team.assistant.store import _default_mission
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
from branding_team.shared.job_store import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    cancel_job,
    create_job,
    delete_job,
    get_job,
    is_job_cancelled,
    list_jobs,
    update_job,
)
from branding_team.store import get_default_store
from shared_observability import init_otel, instrument_fastapi_app
from shared_postgres import get_conn
from shared_postgres.metrics import timed_query

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

# Agent Console Runner — mounts POST /_agents/{agent_id}/invoke for the sandbox proxy.
try:
    from shared_agent_invoke import mount_invoke_shim

    mount_invoke_shim(app, team_key="branding")
except Exception:  # pragma: no cover — shim is optional
    logger.warning("Agent Console invoke shim unavailable for branding", exc_info=True)

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


def _session_to_dict(session: BrandingSession) -> dict:
    return {
        "mission": session.mission.model_dump(mode="json"),
        "questions": [q.model_dump(mode="json") for q in session.questions],
        "latest_output": session.latest_output.model_dump(mode="json"),
    }


def _session_from_dict(d: dict) -> BrandingSession:
    return BrandingSession(
        mission=BrandingMission.model_validate(d["mission"]),
        questions=[BrandingQuestion.model_validate(q) for q in d["questions"]],
        latest_output=TeamOutput.model_validate(d["latest_output"]),
    )


class BrandingSessionStore:
    """Postgres-backed session store — shared across worker processes."""

    @timed_query(store="branding_sessions", op="create")
    def create(
        self, mission: BrandingMission, latest_output: TeamOutput
    ) -> tuple[str, BrandingSession]:
        questions = _build_open_questions(mission)
        session_id = str(uuid4())
        session = BrandingSession(mission=mission, questions=questions, latest_output=latest_output)
        now = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO branding_sessions (session_id, session_json, updated_at) "
                "VALUES (%s, %s, %s)",
                (session_id, Json(_session_to_dict(session)), now),
            )
        return session_id, session

    @timed_query(store="branding_sessions", op="get")
    def get(self, session_id: str) -> Optional[BrandingSession]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT session_json FROM branding_sessions WHERE session_id = %s",
                (session_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return _session_from_dict(row["session_json"])

    @timed_query(store="branding_sessions", op="save")
    def save(self, session_id: str, session: BrandingSession) -> None:
        """Persist mutations to an existing session."""
        now = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE branding_sessions SET session_json = %s, updated_at = %s "
                "WHERE session_id = %s",
                (Json(_session_to_dict(session)), now, session_id),
            )


session_store = BrandingSessionStore()


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


class RunBrandJobResponse(BaseModel):
    job_id: str
    status: str = JOB_STATUS_PENDING


class BrandJobStatusResponse(BaseModel):
    job_id: str
    status: str
    client_id: Optional[str] = None
    brand_id: Optional[str] = None
    current_phase: Optional[str] = None
    progress: Optional[int] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class BrandJobListItem(BaseModel):
    job_id: str
    status: str
    client_id: Optional[str] = None
    brand_id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class BrandJobListResponse(BaseModel):
    jobs: List[BrandJobListItem]


def _run_branding_background(
    job_id: str,
    mission: BrandingMission,
    human_review: HumanReview,
    brand_checks: List[BrandCheckRequest],
    client_id: Optional[str],
    brand_id: Optional[str],
    include_market_research: bool,
    include_design_assets: bool,
    target_phase: Optional[BrandPhase],
) -> None:
    try:
        if is_job_cancelled(job_id):
            return
        update_job(job_id, status=JOB_STATUS_RUNNING)
        result = orchestrator.run(
            mission=mission,
            human_review=human_review,
            brand_checks=brand_checks,
            store=branding_store if (client_id and brand_id) else None,
            client_id=client_id,
            brand_id=brand_id,
            include_market_research=include_market_research,
            include_design_assets=include_design_assets,
            target_phase=target_phase,
        )
        if is_job_cancelled(job_id):
            return
        update_job(job_id, status=JOB_STATUS_COMPLETED, result=result.model_dump())
    except Exception as e:
        logger.exception("Branding job %s failed", job_id)
        if is_job_cancelled(job_id):
            return
        update_job(job_id, status=JOB_STATUS_FAILED, error=str(e))


def _submit_brand_run(
    client_id: str,
    brand_id: str,
    payload: RunBrandRequest,
    target_phase: Optional[BrandPhase],
) -> RunBrandJobResponse:
    brand = branding_store.get_brand(client_id, brand_id)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    human_review = HumanReview(approved=payload.human_approved, feedback=payload.human_feedback)
    job_id = str(uuid4())
    create_job(
        job_id,
        client_id=client_id,
        brand_id=brand_id,
        current_phase=target_phase.value if target_phase else None,
    )
    thread = threading.Thread(
        target=_run_branding_background,
        args=(
            job_id,
            brand.mission,
            human_review,
            payload.brand_checks,
            client_id,
            brand_id,
            payload.include_market_research,
            payload.include_design_assets,
            target_phase,
        ),
        daemon=True,
    )
    thread.start()
    return RunBrandJobResponse(job_id=job_id, status=JOB_STATUS_PENDING)


@app.post("/clients/{client_id}/brands/{brand_id}/run", response_model=RunBrandJobResponse)
def run_brand(
    client_id: str, brand_id: str, payload: RunBrandRequest
) -> RunBrandJobResponse:
    """Submit a branding run job. Poll GET /branding/status/{job_id} for results."""
    target_phase = _parse_target_phase(payload.target_phase)
    return _submit_brand_run(client_id, brand_id, payload, target_phase)


@app.post(
    "/clients/{client_id}/brands/{brand_id}/run/{phase}", response_model=RunBrandJobResponse
)
def run_brand_phase(
    client_id: str, brand_id: str, phase: str, payload: RunBrandRequest
) -> RunBrandJobResponse:
    """Submit a branding run job scoped to a specific phase."""
    target_phase = _parse_target_phase(phase)
    if target_phase is None:
        raise HTTPException(status_code=400, detail=f"Invalid phase: {phase}")
    return _submit_brand_run(client_id, brand_id, payload, target_phase)


@app.get("/branding/status/{job_id}", response_model=BrandJobStatusResponse)
def get_branding_job_status(job_id: str) -> BrandJobStatusResponse:
    data = get_job(job_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return BrandJobStatusResponse(
        job_id=data.get("job_id", job_id),
        status=data.get("status", JOB_STATUS_PENDING),
        client_id=data.get("client_id"),
        brand_id=data.get("brand_id"),
        current_phase=data.get("current_phase"),
        progress=data.get("progress"),
        result=data.get("result"),
        error=data.get("error"),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
    )


@app.get("/branding/jobs", response_model=BrandJobListResponse)
def list_branding_jobs(running_only: bool = False) -> BrandJobListResponse:
    statuses = [JOB_STATUS_PENDING, JOB_STATUS_RUNNING] if running_only else None
    items = [
        BrandJobListItem(
            job_id=j.get("job_id", ""),
            status=j.get("status", JOB_STATUS_PENDING),
            client_id=j.get("client_id"),
            brand_id=j.get("brand_id"),
            created_at=j.get("created_at"),
            updated_at=j.get("updated_at"),
        )
        for j in list_jobs(statuses=statuses)
    ]
    return BrandJobListResponse(jobs=items)


@app.post("/branding/jobs/{job_id}/cancel")
def cancel_branding_job(job_id: str) -> Dict[str, Any]:
    data = get_job(job_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if cancel_job(job_id):
        return {"job_id": job_id, "status": JOB_STATUS_CANCELLED, "success": True}
    return {
        "job_id": job_id,
        "status": data.get("status"),
        "success": False,
        "message": f"Cannot cancel job in status {data.get('status')}",
    }


@app.delete("/branding/jobs/{job_id}")
def delete_branding_job(job_id: str) -> Dict[str, Any]:
    if get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if not delete_job(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job_id": job_id, "deleted": True}


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

    # Run Phase 1 to get strategic core for design asset request
    from branding_team.models import HumanReview

    phase1_result = orchestrator.run_phase(
        mission=brand.mission,
        phase=BrandPhase.STRATEGIC_CORE,
        human_review=HumanReview(approved=True),
    )
    return request_design_assets(phase1_result.strategic_core, brand.mission.company_name)


# ---------------------------------------------------------------------------
# Direct run endpoint
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
