"""FastAPI application for the Agentic Team Provisioning service."""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse

from agentic_team_provisioning.agent_env_provisioning import schedule_provision_step_agents
from agentic_team_provisioning.assistant.agent import ProcessDesignerAgent
from agentic_team_provisioning.assistant.store import AgenticTeamStore
from agentic_team_provisioning.infrastructure import get_team_infrastructure, provision_team
from agentic_team_provisioning.models import (
    AgentEnvProvisionSummary,
    AgenticTeamAgent,
    AgentQualityScore,
    AssetInfo,
    ConversationStateResponse,
    ConversationSummaryResponse,
    CreateConversationRequest,
    CreateFormRecordRequest,
    CreateTeamRequest,
    CreateTeamResponse,
    CreateTestChatSessionRequest,
    FormRecord,
    ProcessDefinition,
    ProcessOutput,
    ProcessStatus,
    ProcessTrigger,
    RateMessageRequest,
    RecommendAgentsResponse,
    RecommendedAgent,
    RenameTestChatSessionRequest,
    RosterValidationResult,
    SendMessageRequest,
    SendTestChatMessageRequest,
    SetTeamModeRequest,
    StartPipelineRunRequest,
    SubmitPipelineInputRequest,
    SubmitTeamAnswersRequest,
    TeamDetailResponse,
    TeamJobDetail,
    TeamJobSummary,
    TeamPendingQuestion,
    TeamSummary,
    TestChatMessage,
    TestChatSession,
    TestChatSessionDetail,
    TestPipelineRun,
    UpdateFormRecordRequest,
)
from agentic_team_provisioning.postgres import SCHEMA as AGENTIC_POSTGRES_SCHEMA
from agentic_team_provisioning.runtime.agent_builder import (
    build_agent as _build_test_agent,
)
from agentic_team_provisioning.runtime.agent_builder import (
    call_agent as _call_test_agent,
)
from agentic_team_provisioning.runtime.agent_builder import (
    generate_starter_prompts,
)
from agentic_team_provisioning.runtime.pipeline_runner import get_pipeline_runner
from agentic_team_provisioning.testing.store import get_test_store
from shared_observability import init_otel, instrument_fastapi_app

logger = logging.getLogger(__name__)

init_otel(service_name="agentic-team-provisioning", team_key="agentic_team_provisioning")


@asynccontextmanager
async def _lifespan(application: FastAPI):
    try:
        from shared_postgres import register_team_schemas

        register_team_schemas(AGENTIC_POSTGRES_SCHEMA)
    except Exception:
        logger.exception("agentic_team_provisioning postgres schema registration failed")
    yield
    try:
        from shared_postgres import close_pool

        close_pool()
    except Exception:
        logger.warning("agentic_team_provisioning shared_postgres close_pool failed", exc_info=True)


app = FastAPI(
    title="Agentic Team Provisioning API",
    description="Create agentic teams and define their processes through conversation",
    lifespan=_lifespan,
)
instrument_fastapi_app(app, team_key="agentic_team_provisioning")

_store = AgenticTeamStore()
_agent = ProcessDesignerAgent()

# Interactive testing mode singletons
_test_store = get_test_store()
_pipeline_runner = get_pipeline_runner(_test_store)

# Retroactive provisioning: ensure all existing teams have infrastructure
try:
    for _team_row in _store.list_teams():
        get_team_infrastructure(_team_row["team_id"])
except Exception as _e:
    logger.warning("Could not retroactively provision team infrastructure: %s", _e)

GREETING = (
    "Hello! I'm your Process Designer assistant. I'll help you design an agentic "
    "team — its agents and processes. Tell me what the team should do at a high "
    "level, and we'll work through the agents you need and the processes they'll run."
)

DEFAULT_SUGGESTIONS = [
    "I want to define a customer onboarding process",
    "Help me create a content review workflow",
    "I need a process for handling support tickets",
]


def _save_agents_from_llm(team_id: str, agents_data: list | None) -> None:
    """Persist agents roster from the LLM ``agents`` block (if present)."""
    if not agents_data:
        return
    agents: list[AgenticTeamAgent] = []
    for a in agents_data:
        name = a.get("agent_name", "")
        if not name:
            continue
        agents.append(
            AgenticTeamAgent(
                agent_name=name,
                role=a.get("role", ""),
                skills=a.get("skills", []),
                capabilities=a.get("capabilities", []),
                tools=a.get("tools", []),
                expertise=a.get("expertise", []),
            )
        )
    if agents:
        _store.save_team_agents(team_id, agents)


def _after_process_saved(team_id: str, process: ProcessDefinition) -> None:
    """Provision per-step agent environments via agent_provisioning_team (background)."""
    schedule_provision_step_agents(team_id, process, _store)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health")
def health():
    return {"status": "ok", "service": "agentic-team-provisioning"}


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------


@app.post("/teams", response_model=CreateTeamResponse)
def create_team(req: CreateTeamRequest):
    team = _store.create_team(name=req.name, description=req.description)
    provision_team(team.team_id)
    return CreateTeamResponse(
        team_id=team.team_id,
        name=team.name,
        description=team.description,
        created_at=team.created_at,
    )


@app.get("/teams", response_model=list[TeamSummary])
def list_teams():
    rows = _store.list_teams()
    return [TeamSummary(**r) for r in rows]


@app.get("/teams/{team_id}", response_model=TeamDetailResponse)
def get_team(team_id: str):
    team = _store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    return TeamDetailResponse(team=team)


# ---------------------------------------------------------------------------
# Team agents pool
# ---------------------------------------------------------------------------


@app.get("/teams/{team_id}/agents", response_model=list[AgenticTeamAgent])
def list_team_agents(team_id: str):
    """Named agents pool (roster) for this team."""
    team = _store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    return team.agents


@app.get("/teams/{team_id}/roster/validation", response_model=RosterValidationResult)
def validate_team_roster(team_id: str):
    """Validate whether the roster fully covers the team's process needs."""
    from agentic_team_provisioning.roster_validation import validate_roster

    team = _store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    return validate_roster(team)


# ---------------------------------------------------------------------------
# Processes (direct CRUD — processes can also be created via conversation)
# ---------------------------------------------------------------------------


@app.get("/teams/{team_id}/processes", response_model=list[ProcessDefinition])
def list_processes(team_id: str):
    team = _store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    return team.processes


@app.get("/processes/{process_id}", response_model=ProcessDefinition)
def get_process(process_id: str):
    process = _store.get_process(process_id)
    if not process:
        raise HTTPException(status_code=404, detail="Process not found")
    return process


@app.post("/teams/{team_id}/processes", response_model=ProcessDefinition, status_code=201)
def create_process(team_id: str):
    """Create a new blank process for the team."""
    team = _store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    process = ProcessDefinition(
        process_id=str(uuid.uuid4()),
        name="New Process",
        description="",
        trigger=ProcessTrigger(),
        steps=[],
        output=ProcessOutput(),
        status=ProcessStatus.DRAFT,
    )
    _store.save_process(team_id, process)
    return process


@app.put("/processes/{process_id}", response_model=ProcessDefinition)
def update_process(process_id: str, process: ProcessDefinition):
    """Update a process definition (visual editor saves)."""
    existing = _store.get_process(process_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Process not found")
    if process.process_id != process_id:
        raise HTTPException(status_code=400, detail="process_id in body must match URL")
    # Find team_id from the store
    team_id = _store.get_process_team_id(process_id)
    if not team_id:
        raise HTTPException(status_code=404, detail="Process team not found")
    _store.save_process(team_id, process)
    _after_process_saved(team_id, process)
    return process


@app.post(
    "/processes/{process_id}/steps/{step_id}/recommend-agents",
    response_model=RecommendAgentsResponse,
)
def recommend_agents_for_step(process_id: str, step_id: str):
    """Recommend agents for a specific process step based on its description."""
    process = _store.get_process(process_id)
    if not process:
        raise HTTPException(status_code=404, detail="Process not found")
    step = next((s for s in process.steps if s.step_id == step_id), None)
    if not step:
        raise HTTPException(status_code=404, detail="Step not found")

    team_id = _store.get_process_team_id(process_id)
    recommendations: list[RecommendedAgent] = []

    # Recommend matching roster agents
    if team_id:
        team = _store.get_team(team_id)
        if team:
            search_tokens = {
                t.lower() for t in f"{step.name} {step.description}".split() if len(t) > 2
            }
            for agent in team.agents:
                agent_tokens = {
                    t.lower()
                    for t in (agent.skills + agent.capabilities + agent.tools + agent.expertise)
                }
                overlap = len(search_tokens & agent_tokens)
                if overlap > 0:
                    recommendations.append(
                        RecommendedAgent(
                            agent_name=agent.agent_name,
                            source="roster",
                            role=agent.role,
                            skills=agent.skills,
                            tools=agent.tools,
                            match_score=float(overlap),
                        )
                    )

    # Sort by score descending
    recommendations.sort(key=lambda r: -r.match_score)

    return RecommendAgentsResponse(
        step_id=step_id,
        step_name=step.name,
        recommended_agents=recommendations[:10],
    )


# ---------------------------------------------------------------------------
# Conversations (chat-based process design)
# ---------------------------------------------------------------------------


def _build_state_response(
    conversation_id: str,
    team_id: str,
    process: Optional[ProcessDefinition],
    suggested_questions: list[str],
) -> ConversationStateResponse:
    messages = _store.get_messages(conversation_id)
    return ConversationStateResponse(
        conversation_id=conversation_id,
        team_id=team_id,
        messages=messages,
        current_process=process,
        suggested_questions=suggested_questions,
    )


@app.post("/conversations", response_model=ConversationStateResponse)
def create_conversation(req: CreateConversationRequest):
    # Validate team exists
    team = _store.get_team(req.team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    conversation_id = _store.create_conversation(team_id=req.team_id)

    if req.initial_message:
        _store.append_message(conversation_id, "user", req.initial_message)

        existing_agents = [
            {"agent_name": a.agent_name, "role": a.role}
            for a in _store.list_team_agents(req.team_id)
        ] or None

        reply, process, suggestions, agents_data = _agent.respond(
            conversation_history=[],
            current_process=None,
            user_message=req.initial_message,
            current_agents=existing_agents,
        )

        _store.append_message(conversation_id, "assistant", reply)
        _save_agents_from_llm(req.team_id, agents_data)
        if process:
            _store.save_process(req.team_id, process)
            _store.set_conversation_process(conversation_id, process.process_id)
            _after_process_saved(req.team_id, process)

        return _build_state_response(conversation_id, req.team_id, process, suggestions)

    # No initial message — just add the greeting
    _store.append_message(conversation_id, "assistant", GREETING)
    return _build_state_response(conversation_id, req.team_id, None, DEFAULT_SUGGESTIONS)


@app.post("/conversations/{conversation_id}/messages", response_model=ConversationStateResponse)
def send_message(conversation_id: str, req: SendMessageRequest):
    team_id = _store.get_conversation_team_id(conversation_id)
    if not team_id:
        raise HTTPException(status_code=404, detail="Conversation not found")

    process_id = _store.get_conversation_process_id(conversation_id)
    current_process = _store.get_process(process_id) if process_id else None

    existing_agents = [
        {"agent_name": a.agent_name, "role": a.role} for a in _store.list_team_agents(team_id)
    ] or None

    existing_messages = _store.get_messages(conversation_id)
    history = [(m.role, m.content) for m in existing_messages]

    _store.append_message(conversation_id, "user", req.message)

    reply, updated_process, suggestions, agents_data = _agent.respond(
        conversation_history=history,
        current_process=current_process,
        user_message=req.message,
        current_agents=existing_agents,
    )

    _store.append_message(conversation_id, "assistant", reply)
    _save_agents_from_llm(team_id, agents_data)

    effective_process = current_process
    if updated_process:
        _store.save_process(team_id, updated_process)
        _store.set_conversation_process(conversation_id, updated_process.process_id)
        effective_process = updated_process
        _after_process_saved(team_id, updated_process)

    return _build_state_response(conversation_id, team_id, effective_process, suggestions)


@app.put("/conversations/{conversation_id}/process")
def set_conversation_process(conversation_id: str, body: dict):
    """Link a process to the active conversation so chat stays in sync with the visual editor."""
    team_id = _store.get_conversation_team_id(conversation_id)
    if not team_id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    process_id = body.get("process_id")
    if not process_id:
        raise HTTPException(status_code=400, detail="process_id is required")
    process = _store.get_process(process_id)
    if not process:
        raise HTTPException(status_code=404, detail="Process not found")
    _store.set_conversation_process(conversation_id, process_id)
    return {"conversation_id": conversation_id, "process_id": process_id}


@app.get("/conversations/{conversation_id}", response_model=ConversationStateResponse)
def get_conversation(conversation_id: str):
    team_id = _store.get_conversation_team_id(conversation_id)
    if not team_id:
        raise HTTPException(status_code=404, detail="Conversation not found")

    process_id = _store.get_conversation_process_id(conversation_id)
    process = _store.get_process(process_id) if process_id else None

    return _build_state_response(conversation_id, team_id, process, [])


@app.get("/teams/{team_id}/conversations", response_model=list[ConversationSummaryResponse])
def list_conversations(team_id: str):
    team = _store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    rows = _store.list_conversations(team_id)
    return [ConversationSummaryResponse(**r) for r in rows]


@app.get("/teams/{team_id}/agent-environments", response_model=List[AgentEnvProvisionSummary])
def list_team_agent_environments(team_id: str):
    """Per-step agent provisioning status (Agent Provisioning team / sandboxed envs)."""
    team = _store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    rows = _store.list_agent_env_provisions(team_id)
    return [AgentEnvProvisionSummary(**r) for r in rows]


# ---------------------------------------------------------------------------
# Per-team infrastructure helper
# ---------------------------------------------------------------------------


def _get_infra_or_404(team_id: str):  # noqa: ANN202
    team = _store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    return get_team_infrastructure(team_id)


# ---------------------------------------------------------------------------
# Team / Job Status
# ---------------------------------------------------------------------------


@app.get("/teams/{team_id}/jobs", response_model=List[TeamJobSummary])
def list_team_jobs(team_id: str):
    """List all jobs for a provisioned team."""
    infra = _get_infra_or_404(team_id)
    raw_jobs = infra.job_client.list_jobs() or []
    return [
        TeamJobSummary(
            job_id=j.get("job_id", ""),
            status=j.get("status", "unknown"),
            created_at=j.get("created_at", ""),
            updated_at=j.get("updated_at", ""),
        )
        for j in raw_jobs
    ]


@app.get("/teams/{team_id}/jobs/{job_id}", response_model=TeamJobDetail)
def get_team_job(team_id: str, job_id: str):
    """Get a single job's detail."""
    infra = _get_infra_or_404(team_id)
    job = infra.job_client.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return TeamJobDetail(
        job_id=job.get("job_id", job_id),
        status=job.get("status", "unknown"),
        data=job,
    )


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------


@app.get("/teams/{team_id}/questions", response_model=List[TeamPendingQuestion])
def list_team_questions(team_id: str):
    """Collect pending questions from all active jobs for a team."""
    infra = _get_infra_or_404(team_id)
    active_jobs = infra.job_client.list_jobs(statuses=["pending", "running"]) or []
    result: List[TeamPendingQuestion] = []
    for j in active_jobs:
        jid = j.get("job_id", "")
        for q in j.get("pending_questions", []):
            result.append(TeamPendingQuestion(job_id=jid, question=q))
    return result


@app.post("/teams/{team_id}/questions/{job_id}/answers")
def submit_team_answers(team_id: str, job_id: str, req: SubmitTeamAnswersRequest):
    """Submit answers to pending questions for a job."""
    infra = _get_infra_or_404(team_id)
    job = infra.job_client.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    infra.job_client.atomic_update(
        job_id,
        merge_fields={"pending_questions": [], "waiting_for_answers": False},
        append_to={"submitted_answers": req.answers},
    )
    return {"job_id": job_id, "message": "Answers submitted"}


# ---------------------------------------------------------------------------
# Assets (File System)
# ---------------------------------------------------------------------------


def _safe_asset_name(name: str) -> str:
    """Sanitize asset name to prevent path traversal."""
    sanitized = Path(name).name
    if not sanitized or sanitized in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid asset name")
    return sanitized


@app.get("/teams/{team_id}/assets", response_model=List[AssetInfo])
def list_team_assets(team_id: str):
    """List files in the team's asset directory."""
    infra = _get_infra_or_404(team_id)
    assets: List[AssetInfo] = []
    if infra.assets_dir.is_dir():
        for p in sorted(infra.assets_dir.iterdir()):
            if p.is_file():
                stat = p.stat()
                assets.append(
                    AssetInfo(
                        name=p.name,
                        size_bytes=stat.st_size,
                        modified_at=datetime.fromtimestamp(
                            stat.st_mtime, tz=timezone.utc
                        ).isoformat(),
                    )
                )
    return assets


@app.get("/teams/{team_id}/assets/{name}")
def download_team_asset(team_id: str, name: str):
    """Download a specific asset file."""
    infra = _get_infra_or_404(team_id)
    safe_name = _safe_asset_name(name)
    path = infra.assets_dir / safe_name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Asset not found")
    return FileResponse(str(path), filename=safe_name)


@app.post("/teams/{team_id}/assets", response_model=AssetInfo)
async def upload_team_asset(team_id: str, file: UploadFile):
    """Upload a file to the team's asset directory."""
    infra = _get_infra_or_404(team_id)
    safe_name = _safe_asset_name(file.filename or "upload")
    dest = infra.assets_dir / safe_name
    content = await file.read()
    dest.write_bytes(content)
    stat = dest.stat()
    return AssetInfo(
        name=safe_name,
        size_bytes=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------------------
# Form Information (Database)
# ---------------------------------------------------------------------------


@app.get("/teams/{team_id}/forms", response_model=List[str])
def list_team_form_keys(team_id: str):
    """List distinct form keys that have records."""
    infra = _get_infra_or_404(team_id)
    return infra.form_store.list_form_keys()


@app.get("/teams/{team_id}/forms/{form_key}", response_model=List[FormRecord])
def list_team_form_records(team_id: str, form_key: str):
    """Get all records for a form key."""
    infra = _get_infra_or_404(team_id)
    rows = infra.form_store.get_records(form_key)
    return [FormRecord(**r) for r in rows]


@app.post("/teams/{team_id}/forms/{form_key}", response_model=FormRecord, status_code=201)
def create_team_form_record(team_id: str, form_key: str, req: CreateFormRecordRequest):
    """Create a new form record."""
    infra = _get_infra_or_404(team_id)
    record = infra.form_store.create_record(form_key, req.data)
    return FormRecord(**record)


@app.put("/teams/{team_id}/forms/{form_key}/{record_id}", response_model=FormRecord)
def update_team_form_record(
    team_id: str, form_key: str, record_id: str, req: UpdateFormRecordRequest
):
    """Update an existing form record."""
    infra = _get_infra_or_404(team_id)
    if not infra.form_store.update_record(record_id, req.data):
        raise HTTPException(status_code=404, detail="Record not found")
    record = infra.form_store.get_record(record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found after update")
    return FormRecord(**record)


@app.delete("/teams/{team_id}/forms/{form_key}/{record_id}", status_code=204)
def delete_team_form_record(team_id: str, form_key: str, record_id: str):
    """Delete a form record."""
    infra = _get_infra_or_404(team_id)
    if not infra.form_store.delete_record(record_id):
        raise HTTPException(status_code=404, detail="Record not found")


# ---------------------------------------------------------------------------
# Interactive Testing Mode
# ---------------------------------------------------------------------------


@app.put("/teams/{team_id}/mode")
def set_team_mode(team_id: str, req: SetTeamModeRequest):
    """Toggle team between development and testing mode."""
    team = _store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    _test_store.set_team_mode(team_id, req.mode.value)
    return {"team_id": team_id, "mode": req.mode.value}


# ---------------------------------------------------------------------------
# Agent Chat Testing
# ---------------------------------------------------------------------------


def _find_agent_in_roster(team_id: str, agent_name: str) -> AgenticTeamAgent:
    """Look up an agent by name in the team roster."""
    agents = _store.list_team_agents(team_id)
    for a in agents:
        if a.agent_name == agent_name:
            return a
    raise HTTPException(status_code=404, detail=f"Agent '{agent_name}' not found in team roster")


@app.post("/teams/{team_id}/test-chat/sessions", response_model=TestChatSession, status_code=201)
def create_test_chat_session(team_id: str, req: CreateTestChatSessionRequest):
    """Create a new chat test session for an agent."""
    team = _store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    _find_agent_in_roster(team_id, req.agent_name)
    session_id = str(uuid.uuid4())
    row = _test_store.create_chat_session(session_id, team_id, req.agent_name)
    return TestChatSession(**row)


@app.get("/teams/{team_id}/test-chat/sessions", response_model=List[TestChatSession])
def list_test_chat_sessions(team_id: str, agent_name: Optional[str] = None):
    """List chat test sessions for a team, optionally filtered by agent."""
    rows = _test_store.list_chat_sessions(team_id, agent_name=agent_name)
    return [TestChatSession(**r) for r in rows]


@app.get("/teams/{team_id}/test-chat/sessions/{session_id}", response_model=TestChatSessionDetail)
def get_test_chat_session(team_id: str, session_id: str):
    """Get a chat session with full message history and suggested prompts."""
    session_row = _test_store.get_chat_session(session_id)
    if not session_row or session_row["team_id"] != team_id:
        raise HTTPException(status_code=404, detail="Session not found")

    messages = _test_store.list_chat_messages(session_id)
    session = TestChatSession(**session_row)

    # Generate suggested prompts if no messages yet
    prompts: list[str] = []
    if not messages:
        try:
            agent_def = _find_agent_in_roster(team_id, session.agent_name)
            prompts = generate_starter_prompts(
                agent_def.agent_name, agent_def.role, agent_def.skills, agent_def.expertise
            )
        except HTTPException:
            pass

    return TestChatSessionDetail(
        session=session,
        messages=[TestChatMessage(**m) for m in messages],
        suggested_prompts=prompts,
    )


@app.put("/teams/{team_id}/test-chat/sessions/{session_id}/name")
def rename_test_chat_session(team_id: str, session_id: str, req: RenameTestChatSessionRequest):
    """Rename a chat test session."""
    session_row = _test_store.get_chat_session(session_id)
    if not session_row or session_row["team_id"] != team_id:
        raise HTTPException(status_code=404, detail="Session not found")
    _test_store.rename_chat_session(session_id, req.session_name)
    return {"session_id": session_id, "session_name": req.session_name}


@app.delete("/teams/{team_id}/test-chat/sessions/{session_id}", status_code=204)
def delete_test_chat_session(team_id: str, session_id: str):
    """Delete a chat test session and its messages."""
    session_row = _test_store.get_chat_session(session_id)
    if not session_row or session_row["team_id"] != team_id:
        raise HTTPException(status_code=404, detail="Session not found")
    _test_store.delete_chat_session(session_id)


@app.post("/teams/{team_id}/test-chat/sessions/{session_id}/messages")
def send_test_chat_message(team_id: str, session_id: str, req: SendTestChatMessageRequest):
    """Send a message to an agent and get a synchronous response.

    The full conversation history is sent to the agent for multi-turn
    context. Both user and assistant messages are stored.
    """
    session_row = _test_store.get_chat_session(session_id)
    if not session_row or session_row["team_id"] != team_id:
        raise HTTPException(status_code=404, detail="Session not found")

    agent_name = session_row["agent_name"]
    agent_def = _find_agent_in_roster(team_id, agent_name)

    # Store user message
    user_msg_id = str(uuid.uuid4())
    _test_store.create_chat_message(user_msg_id, session_id, "user", req.content)

    # Build conversation context from history
    history = _test_store.list_chat_messages(session_id)
    context_parts = []
    for msg in history[:-1]:  # Exclude the just-added user message (will add below)
        prefix = "User" if msg["role"] == "user" else "Assistant"
        context_parts.append(f"{prefix}: {msg['content']}")
    context_parts.append(f"User: {req.content}")
    full_context = "\n\n".join(context_parts)

    # Build and invoke agent
    agent_instance = _build_test_agent(
        agent_def.agent_name,
        agent_def.role,
        agent_def.skills,
        agent_def.capabilities,
        agent_def.tools,
        agent_def.expertise,
    )
    response_text = _call_test_agent(agent_instance, full_context)

    # Store assistant message
    asst_msg_id = str(uuid.uuid4())
    _test_store.create_chat_message(asst_msg_id, session_id, "assistant", response_text)

    # Return all messages
    all_messages = _test_store.list_chat_messages(session_id)
    return {
        "session": TestChatSession(**session_row),
        "messages": [TestChatMessage(**m) for m in all_messages],
    }


@app.get("/teams/{team_id}/test-chat/sessions/{session_id}/export")
def export_test_chat_session(team_id: str, session_id: str):
    """Export a chat session transcript as Markdown text."""
    session_row = _test_store.get_chat_session(session_id)
    if not session_row or session_row["team_id"] != team_id:
        raise HTTPException(status_code=404, detail="Session not found")

    messages = _test_store.list_chat_messages(session_id)
    agent_name = session_row["agent_name"]
    session_name = session_row.get("session_name") or f"Chat with {agent_name}"

    lines = [f"# {session_name}", f"Agent: {agent_name}", ""]
    for msg in messages:
        role_label = "**User**" if msg["role"] == "user" else f"**{agent_name}**"
        rating_str = ""
        if msg.get("rating"):
            rating_str = " \u2705" if msg["rating"] == "thumbs_up" else " \u274c"
        lines.append(f"{role_label}{rating_str}:")
        lines.append(msg["content"])
        lines.append("")

    return Response(
        content="\n".join(lines),
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{session_id}.md"'},
    )


@app.put("/teams/{team_id}/test-chat/messages/{message_id}/rating")
def rate_test_chat_message(team_id: str, message_id: str, req: RateMessageRequest):
    """Rate an assistant message (thumbs up/thumbs down)."""
    if not _test_store.update_message_rating(message_id, req.rating.value):
        raise HTTPException(status_code=404, detail="Message not found")
    return {"message_id": message_id, "rating": req.rating.value}


@app.get("/teams/{team_id}/test-chat/quality-scores", response_model=List[AgentQualityScore])
def get_agent_quality_scores(team_id: str):
    """Get aggregated quality scores per agent based on chat ratings."""
    rows = _test_store.get_agent_quality_scores(team_id)
    return [AgentQualityScore(**r) for r in rows]


# ---------------------------------------------------------------------------
# Pipeline Testing (End-to-End Walkthrough)
# ---------------------------------------------------------------------------


@app.post("/teams/{team_id}/test-pipeline/runs", response_model=TestPipelineRun, status_code=201)
def start_pipeline_run(team_id: str, req: StartPipelineRunRequest):
    """Start an end-to-end pipeline test run."""
    team = _store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    # Find the process
    process = None
    for p in team.processes:
        if p.process_id == req.process_id:
            process = p
            break
    if process is None:
        raise HTTPException(status_code=404, detail="Process not found")

    # Build ProcessDefinition from stored data
    process_def = (
        process if isinstance(process, ProcessDefinition) else ProcessDefinition(**process)
    )

    run_id = str(uuid.uuid4())
    run_row = _test_store.create_pipeline_run(run_id, team_id, req.process_id, req.initial_input)

    # Gather team agents
    team_agents_raw = _store.list_team_agents(team_id)
    team_agents = [
        a if isinstance(a, AgenticTeamAgent) else AgenticTeamAgent(**a) for a in team_agents_raw
    ]

    # Start the pipeline in a background thread
    _pipeline_runner.start_run(run_id, team_agents, process_def)

    return TestPipelineRun(**run_row)


@app.get("/teams/{team_id}/test-pipeline/runs", response_model=List[TestPipelineRun])
def list_pipeline_runs(team_id: str):
    """List pipeline test runs for a team."""
    rows = _test_store.list_pipeline_runs(team_id)
    return [TestPipelineRun(**r) for r in rows]


@app.get("/teams/{team_id}/test-pipeline/runs/{run_id}", response_model=TestPipelineRun)
def get_pipeline_run(team_id: str, run_id: str):
    """Get the current status and step results of a pipeline test run."""
    row = _test_store.get_pipeline_run(run_id)
    if not row or row["team_id"] != team_id:
        raise HTTPException(status_code=404, detail="Pipeline run not found")
    return TestPipelineRun(**row)


@app.post("/teams/{team_id}/test-pipeline/runs/{run_id}/input")
def submit_pipeline_input(team_id: str, run_id: str, req: SubmitPipelineInputRequest):
    """Submit human input at a WAIT step to resume the pipeline."""
    row = _test_store.get_pipeline_run(run_id)
    if not row or row["team_id"] != team_id:
        raise HTTPException(status_code=404, detail="Pipeline run not found")
    if row["status"] != "waiting_for_input":
        raise HTTPException(status_code=400, detail="Pipeline is not waiting for input")
    _pipeline_runner.submit_human_input(run_id, req.input)
    updated = _test_store.get_pipeline_run(run_id)
    return TestPipelineRun(**(updated or row))


@app.post("/teams/{team_id}/test-pipeline/runs/{run_id}/cancel")
def cancel_pipeline_run(team_id: str, run_id: str):
    """Cancel a running or waiting pipeline test run."""
    row = _test_store.get_pipeline_run(run_id)
    if not row or row["team_id"] != team_id:
        raise HTTPException(status_code=404, detail="Pipeline run not found")
    if row["status"] not in ("running", "waiting_for_input"):
        raise HTTPException(status_code=400, detail="Pipeline is not in a cancellable state")
    _pipeline_runner.cancel_run(run_id)
    updated = _test_store.get_pipeline_run(run_id)
    return TestPipelineRun(**(updated or row))
