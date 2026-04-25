"""User Agent Founder API — autonomous startup founder driving the SE team."""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from shared_observability import init_otel, instrument_fastapi_app
from user_agent_founder.agent import FounderAgent
from user_agent_founder.orchestrator import run_workflow
from user_agent_founder.postgres import SCHEMA as USER_AGENT_FOUNDER_POSTGRES_SCHEMA
from user_agent_founder.store import DEFAULT_TARGET_TEAM_KEY, get_founder_store
from user_agent_founder.targets import get_adapter

logger = logging.getLogger(__name__)

init_otel(service_name="user-agent-founder", team_key="user_agent_founder")


@asynccontextmanager
async def _lifespan(application: FastAPI):
    try:
        from shared_postgres import register_team_schemas

        register_team_schemas(USER_AGENT_FOUNDER_POSTGRES_SCHEMA)
    except Exception:
        logger.exception("user_agent_founder postgres schema registration failed")
    yield
    try:
        from shared_postgres import close_pool

        close_pool()
    except Exception:
        logger.warning("user_agent_founder shared_postgres close_pool failed", exc_info=True)


app = FastAPI(
    title="User Agent Founder API",
    description=(
        "Autonomous startup founder agent that generates a product spec, "
        "submits it to the Software Engineering team, and answers all questions "
        "through the lens of a budget-conscious, speed-first, UX-obsessed founder."
    ),
    version="1.0.0",
    lifespan=_lifespan,
)
instrument_fastapi_app(app, team_key="user_agent_founder")

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class StartRunRequest(BaseModel):
    target_team_key: str = Field(
        default=DEFAULT_TARGET_TEAM_KEY,
        description="Which target team this persona run drives. Must be a key in targets.ADAPTERS.",
    )


class StartRunResponse(BaseModel):
    # External-facing key used by the team-assistant launch endpoint and the
    # jobs UI. Internally the team still uses ``run_id`` for its own rows;
    # we only rename at the wire.
    job_id: str
    status: str = "pending"
    message: str = "Founder workflow started. Poll GET /status/{job_id} for progress."


class DecisionResponse(BaseModel):
    decision_id: int
    question_id: str
    question_text: str
    answer_text: str
    rationale: str
    timestamp: str


class RunStatusResponse(BaseModel):
    run_id: str
    status: str
    se_job_id: Optional[str] = None
    analysis_job_id: Optional[str] = None
    spec_content: Optional[str] = None
    repo_path: Optional[str] = None
    target_team_key: str = DEFAULT_TARGET_TEAM_KEY
    created_at: str
    updated_at: str
    error: Optional[str] = None
    decisions: list[DecisionResponse] = Field(default_factory=list)


class RunSummaryResponse(BaseModel):
    run_id: str
    status: str
    se_job_id: Optional[str] = None
    analysis_job_id: Optional[str] = None
    target_team_key: str = DEFAULT_TARGET_TEAM_KEY
    created_at: str
    updated_at: str
    error: Optional[str] = None


class RunListResponse(BaseModel):
    runs: list[RunSummaryResponse]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def _dispatch_founder_run(run_id: str) -> str:
    """Dispatch a founder run via Temporal when enabled, else a daemon thread.

    Returns a short label describing which execution mode was used so the
    caller can include it in the response message. Raises ``RuntimeError``
    if Temporal is enabled but the workflow fails to start.
    """
    try:
        from shared_temporal import is_temporal_enabled

        if is_temporal_enabled():
            from user_agent_founder.temporal.start_workflow import start_founder_workflow

            start_founder_workflow(run_id)
            logger.info("Founder workflow started via Temporal: run_id=%s", run_id)
            return "Temporal"
    except ImportError:
        pass

    store = get_founder_store()
    agent = FounderAgent()
    run = store.get_run(run_id)
    team_key = (run.target_team_key if run is not None else None) or DEFAULT_TARGET_TEAM_KEY
    adapter = get_adapter(team_key)
    thread = threading.Thread(
        target=run_workflow,
        args=(run_id, store, agent, adapter),
        name=f"founder-workflow-{run_id[:8]}",
        daemon=True,
    )
    thread.start()
    logger.info("Founder workflow thread started: run_id=%s", run_id)
    return "thread"


@app.post("/start", response_model=StartRunResponse)
def start_founder_workflow(request: StartRunRequest | None = None) -> StartRunResponse:
    """Kick off the autonomous founder workflow.

    The agent will:
    1. Generate a task management product spec
    2. Submit it to the target team for product analysis
    3. Answer all target-team questions autonomously
    4. Trigger the full target-team build pipeline

    The run is registered with the centralized job service **before**
    dispatch so it appears in the Jobs Dashboard immediately.
    """
    from user_agent_founder.shared import job_store

    target_team_key = request.target_team_key if request else DEFAULT_TARGET_TEAM_KEY
    # Validate up-front so an unknown key returns 400 instead of crashing the
    # background dispatch thread later.
    try:
        get_adapter(target_team_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    store = get_founder_store()
    run_id = store.create_run(target_team_key=target_team_key)

    job_store.create_job(
        run_id,
        status=job_store.JOB_STATUS_RUNNING,
        label="Testing Personas workflow",
        current_phase="starting",
    )

    try:
        mode = _dispatch_founder_run(run_id)
    except Exception as exc:
        logger.exception("Failed to dispatch founder workflow for %s", run_id)
        job_store.update_job(
            run_id, status=job_store.JOB_STATUS_FAILED, error=f"Dispatch failed: {exc}"[:500]
        )
        store.update_run(run_id, status="failed", error=f"Dispatch failed: {exc}"[:1000])
        raise HTTPException(status_code=500, detail=f"Failed to start workflow: {exc}") from exc

    return StartRunResponse(
        job_id=run_id,
        status=job_store.JOB_STATUS_RUNNING,
        message=f"Founder workflow started ({mode}). Poll GET /status/{run_id} for progress.",
    )


@app.get("/status/{run_id}", response_model=RunStatusResponse)
def get_run_status(run_id: str) -> RunStatusResponse:
    """Get the current status of a founder workflow run, including all decisions made."""
    store = get_founder_store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    decisions = store.get_decisions(run_id)
    return RunStatusResponse(
        run_id=run.run_id,
        status=run.status,
        se_job_id=run.se_job_id,
        analysis_job_id=run.analysis_job_id,
        spec_content=run.spec_content,
        repo_path=run.repo_path,
        target_team_key=run.target_team_key,
        created_at=run.created_at,
        updated_at=run.updated_at,
        error=run.error,
        decisions=[
            DecisionResponse(
                decision_id=d.decision_id,
                question_id=d.question_id,
                question_text=d.question_text,
                answer_text=d.answer_text,
                rationale=d.rationale,
                timestamp=d.timestamp,
            )
            for d in decisions
        ],
    )


@app.get("/runs", response_model=RunListResponse)
def list_runs() -> RunListResponse:
    """List all founder workflow runs."""
    store = get_founder_store()
    runs = store.list_runs()
    return RunListResponse(
        runs=[
            RunSummaryResponse(
                run_id=r.run_id,
                status=r.status,
                se_job_id=r.se_job_id,
                analysis_job_id=r.analysis_job_id,
                target_team_key=r.target_team_key,
                created_at=r.created_at,
                updated_at=r.updated_at,
                error=r.error,
            )
            for r in runs
        ]
    )


@app.get("/decisions/{run_id}", response_model=list[DecisionResponse])
def get_decisions(run_id: str) -> list[DecisionResponse]:
    """Get all decisions and rationale for a specific run."""
    store = get_founder_store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    decisions = store.get_decisions(run_id)
    return [
        DecisionResponse(
            decision_id=d.decision_id,
            question_id=d.question_id,
            question_text=d.question_text,
            answer_text=d.answer_text,
            rationale=d.rationale,
            timestamp=d.timestamp,
        )
        for d in decisions
    ]


class PersonaInfo(BaseModel):
    id: str
    name: str
    description: str
    icon: str


class PersonaListResponse(BaseModel):
    personas: list[PersonaInfo]


class ChatMessageResponse(BaseModel):
    message_id: int
    role: str
    content: str
    message_type: str
    metadata: Optional[dict[str, Any]] = None
    timestamp: str


class ChatHistoryResponse(BaseModel):
    run_id: str
    messages: list[ChatMessageResponse]


class SendChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)


class RunArtifactsResponse(BaseModel):
    run_id: str
    se_job_id: Optional[str] = None
    se_job_status: Optional[dict[str, Any]] = None
    repo_path: Optional[str] = None
    spec_content: Optional[str] = None


@app.get("/personas", response_model=PersonaListResponse)
def list_personas() -> PersonaListResponse:
    """Return the list of available project personas for SE team testing."""
    return PersonaListResponse(
        personas=[
            PersonaInfo(
                id="startup-founder",
                name="Startup Founder",
                description=(
                    "Alex Chen — a bootstrapped startup founder building TaskFlow. "
                    "Budget-conscious, speed-first, UX-obsessed. Generates a task management "
                    "product spec and drives the SE team autonomously."
                ),
                icon="rocket_launch",
            ),
        ]
    )


@app.get("/runs/{run_id}/artifacts", response_model=RunArtifactsResponse)
def get_run_artifacts(run_id: str) -> RunArtifactsResponse:
    """Get artifacts produced during a persona test run.

    Proxies to the target-team job status (resolved via the run's
    ``target_team_key``) to retrieve task results, task states, and
    other pipeline outputs.
    """
    store = get_founder_store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    se_job_status: dict[str, Any] | None = None
    if run.se_job_id:
        try:
            adapter = get_adapter(run.target_team_key)
        except ValueError:
            logger.warning(
                "Unknown target_team_key %r on run %s; cannot fetch artifacts",
                run.target_team_key,
                run.run_id,
            )
        else:
            try:
                with httpx.Client() as client:
                    payload = adapter.poll_build(client, run.se_job_id)
                if not payload.get("_poll_error"):
                    se_job_status = payload
            except httpx.HTTPError:
                logger.warning("Failed to fetch target-team job status for %s", run.se_job_id)

    return RunArtifactsResponse(
        run_id=run.run_id,
        se_job_id=run.se_job_id,
        se_job_status=se_job_status,
        repo_path=run.repo_path,
        spec_content=run.spec_content,
    )


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------


@app.get("/runs/{run_id}/chat", response_model=ChatHistoryResponse)
def get_chat_history(run_id: str, since_id: int = 0) -> ChatHistoryResponse:
    """Get chat messages for a run, optionally only messages after since_id."""
    store = get_founder_store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    messages = store.get_chat_messages(run_id, since_id=since_id)
    return ChatHistoryResponse(
        run_id=run_id,
        messages=[
            ChatMessageResponse(
                message_id=m.message_id,
                role=m.role,
                content=m.content,
                message_type=m.message_type,
                metadata=m.metadata,
                timestamp=m.timestamp,
            )
            for m in messages
        ],
    )


@app.post("/runs/{run_id}/chat", response_model=ChatHistoryResponse)
def send_chat_message(run_id: str, request: SendChatRequest) -> ChatHistoryResponse:
    """Send a message to the founder persona and get a response."""
    store = get_founder_store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    # Store user message
    store.add_chat_message(run_id, "user", request.message, "chat")

    # Build context for the persona
    decisions = store.get_decisions(run_id)
    context: dict[str, Any] = {
        "status": run.status,
        "recent_decisions": [
            {"question_text": d.question_text, "answer_text": d.answer_text} for d in decisions[-5:]
        ],
    }

    # Get persona response
    agent = FounderAgent()
    try:
        response = agent.chat(request.message, context)
    except Exception as exc:
        logger.exception("Chat LLM call failed for run %s", run_id)
        response = f"Sorry, I'm having trouble responding right now. ({str(exc)[:100]})"

    store.add_chat_message(run_id, "assistant", response, "chat")

    # Return recent messages
    messages = store.get_chat_messages(run_id)
    return ChatHistoryResponse(
        run_id=run_id,
        messages=[
            ChatMessageResponse(
                message_id=m.message_id,
                role=m.role,
                content=m.content,
                message_type=m.message_type,
                metadata=m.metadata,
                timestamp=m.timestamp,
            )
            for m in messages
        ],
    )


# ---------------------------------------------------------------------------
# Job management (centralized job service integration for Jobs Dashboard)
# ---------------------------------------------------------------------------


class FounderJobSummary(BaseModel):
    job_id: str
    status: str
    label: str = "Persona: founder workflow"
    current_phase: Optional[str] = None
    created_at: Optional[str] = None
    error: Optional[str] = None


class FounderJobListResponse(BaseModel):
    jobs: list[FounderJobSummary]


def _cancellable_statuses() -> frozenset[str]:
    from user_agent_founder.shared import job_store

    return frozenset({job_store.JOB_STATUS_PENDING, job_store.JOB_STATUS_RUNNING})


@app.get("/jobs", response_model=FounderJobListResponse)
def list_jobs(running_only: bool = False) -> FounderJobListResponse:
    """List founder workflow jobs from the centralized job service."""
    from user_agent_founder.shared import job_store

    statuses = (
        [job_store.JOB_STATUS_RUNNING, job_store.JOB_STATUS_PENDING] if running_only else None
    )
    raw = job_store.list_jobs(statuses=statuses)
    jobs = []
    for j in raw:
        data = j.get("data", j)
        jobs.append(
            FounderJobSummary(
                job_id=j.get("job_id", ""),
                status=j.get("status", data.get("status", "unknown")),
                label=data.get("label", "Testing Personas workflow"),
                current_phase=data.get("current_phase"),
                created_at=j.get("created_at", data.get("created_at")),
                error=data.get("error"),
            )
        )
    return FounderJobListResponse(jobs=jobs)


@app.post("/job/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, str]:
    """Cancel a running founder workflow job."""
    from user_agent_founder.shared import job_store

    try:
        job_store.validate_job_for_action(
            job_store.get_job(job_id), job_id, _cancellable_statuses(), "cancelled"
        )
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=code, detail=str(exc)) from exc

    job_store.update_job(job_id, status=job_store.JOB_STATUS_CANCELLED, error="Cancelled by user")
    store = get_founder_store()
    store.update_run(job_id, status="failed", error="Cancelled by user")
    return {"status": job_store.JOB_STATUS_CANCELLED, "job_id": job_id}


@app.post("/job/{job_id}/resume", response_model=StartRunResponse)
def resume_job(job_id: str) -> StartRunResponse:
    """Resume a failed or interrupted founder workflow.

    The founder workflow has no user inputs, so "resume" re-runs the
    three phases from the beginning — the store retains any previous
    decisions and chat history for audit.
    """
    from user_agent_founder.shared import job_store

    try:
        job_store.validate_job_for_action(
            job_store.get_job(job_id), job_id, job_store.RESUMABLE_STATUSES, "resumed"
        )
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=code, detail=str(exc)) from exc

    job_store.update_job(
        job_id, status=job_store.JOB_STATUS_RUNNING, error=None, current_phase="resuming"
    )
    store = get_founder_store()
    store.update_run(job_id, status="pending", error=None)

    try:
        mode = _dispatch_founder_run(job_id)
    except Exception as exc:
        logger.exception("Failed to resume founder workflow for %s", job_id)
        job_store.update_job(
            job_id,
            status=job_store.JOB_STATUS_FAILED,
            error=f"Resume dispatch failed: {exc}"[:500],
        )
        store.update_run(job_id, status="failed", error=f"Resume dispatch failed: {exc}"[:1000])
        raise HTTPException(status_code=500, detail=f"Failed to resume workflow: {exc}") from exc

    return StartRunResponse(
        job_id=job_id,
        status=job_store.JOB_STATUS_RUNNING,
        message=f"Founder workflow resumed ({mode}).",
    )


@app.post("/job/{job_id}/restart", response_model=StartRunResponse)
def restart_job(job_id: str) -> StartRunResponse:
    """Restart a completed/failed/cancelled founder workflow from scratch.

    Clears any prior error/phase on both the central job record and the
    founder store row, then re-dispatches the pipeline.
    """
    from user_agent_founder.shared import job_store

    try:
        job_store.validate_job_for_action(
            job_store.get_job(job_id), job_id, job_store.RESTARTABLE_STATUSES, "restarted"
        )
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=code, detail=str(exc)) from exc

    job_store.reset_job(job_id)
    job_store.update_job(job_id, status=job_store.JOB_STATUS_RUNNING, current_phase="starting")
    store = get_founder_store()
    # Clear every checkpoint column so the orchestrator's resume short-circuit
    # (which keys off non-NULL spec_content / analysis_job_id / repo_path /
    # se_job_id) cannot turn a restart into a stale replay.
    store.update_run(
        job_id,
        status="pending",
        error=None,
        spec_content=None,
        analysis_job_id=None,
        repo_path=None,
        se_job_id=None,
    )

    try:
        mode = _dispatch_founder_run(job_id)
    except Exception as exc:
        logger.exception("Failed to restart founder workflow for %s", job_id)
        job_store.update_job(
            job_id,
            status=job_store.JOB_STATUS_FAILED,
            error=f"Restart dispatch failed: {exc}"[:500],
        )
        store.update_run(job_id, status="failed", error=f"Restart dispatch failed: {exc}"[:1000])
        raise HTTPException(status_code=500, detail=f"Failed to restart workflow: {exc}") from exc

    return StartRunResponse(
        job_id=job_id,
        status=job_store.JOB_STATUS_RUNNING,
        message=f"Founder workflow restarted ({mode}).",
    )


@app.delete("/job/{job_id}")
def delete_job(job_id: str) -> dict[str, str]:
    """Delete a founder workflow job from both the job service and store."""
    from user_agent_founder.shared import job_store

    if job_store.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    job_store.delete_job(job_id)
    store = get_founder_store()
    try:
        store.delete_run(job_id)
    except Exception:
        logger.debug("Founder store delete_run failed for %s (non-fatal)", job_id, exc_info=True)
    return {"deleted": "true", "job_id": job_id}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
