"""User Agent Founder API — autonomous startup founder driving the SE team."""

from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from shared_observability import init_otel, instrument_fastapi_app
from user_agent_founder.agent import get_founder_agent
from user_agent_founder.orchestrator import run_workflow
from user_agent_founder.postgres import SCHEMA as USER_AGENT_FOUNDER_POSTGRES_SCHEMA
from user_agent_founder.store import get_founder_store

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


class StartRunResponse(BaseModel):
    run_id: str
    status: str = "pending"
    message: str = "Founder workflow started. Poll GET /status/{run_id} for progress."


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
    created_at: str
    updated_at: str
    error: Optional[str] = None
    decisions: list[DecisionResponse] = Field(default_factory=list)


class RunSummaryResponse(BaseModel):
    run_id: str
    status: str
    se_job_id: Optional[str] = None
    analysis_job_id: Optional[str] = None
    created_at: str
    updated_at: str
    error: Optional[str] = None


class RunListResponse(BaseModel):
    runs: list[RunSummaryResponse]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post("/start", response_model=StartRunResponse)
def start_founder_workflow() -> StartRunResponse:
    """Kick off the autonomous founder workflow.

    The agent will:
    1. Generate a task management product spec
    2. Submit it to the SE team for product analysis
    3. Answer all SE team questions autonomously
    4. Trigger the full SE team build pipeline
    """
    store = get_founder_store()
    agent = get_founder_agent()
    run_id = store.create_run()

    thread = threading.Thread(
        target=run_workflow,
        args=(run_id, store, agent),
        name=f"founder-workflow-{run_id[:8]}",
        daemon=True,
    )
    thread.start()
    logger.info("Founder workflow thread started: run_id=%s", run_id)

    return StartRunResponse(run_id=run_id)


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


class RunArtifactsResponse(BaseModel):
    run_id: str
    se_job_id: Optional[str] = None
    se_job_status: Optional[dict[str, Any]] = None
    repo_path: Optional[str] = None
    spec_content: Optional[str] = None


UNIFIED_API_BASE = os.environ.get("UNIFIED_API_BASE_URL", "http://localhost:8080")
SE_PREFIX = "/api/software-engineering"
_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


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

    Proxies to the SE team job status to retrieve task results,
    task states, and other pipeline outputs.
    """
    store = get_founder_store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    se_job_status: dict[str, Any] | None = None
    if run.se_job_id:
        try:
            with httpx.Client() as client:
                resp = client.get(
                    f"{UNIFIED_API_BASE}{SE_PREFIX}/run-team/{run.se_job_id}",
                    timeout=_HTTP_TIMEOUT,
                )
                if resp.status_code < 400:
                    se_job_status = resp.json()
        except httpx.HTTPError:
            logger.warning("Failed to fetch SE job status for %s", run.se_job_id)

    return RunArtifactsResponse(
        run_id=run.run_id,
        se_job_id=run.se_job_id,
        se_job_status=se_job_status,
        repo_path=run.repo_path,
        spec_content=run.spec_content,
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
