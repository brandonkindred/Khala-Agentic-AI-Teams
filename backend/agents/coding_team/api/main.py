"""
FastAPI app for coding_team: GET /health, POST /run, GET /status/{job_id}, GET /jobs.
"""

from __future__ import annotations

import logging
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure backend/agents is on path for coding_team and shared_job_management
_agents_root = Path(__file__).resolve().parent.parent.parent
if str(_agents_root) not in sys.path:
    sys.path.insert(0, str(_agents_root))

from fastapi import FastAPI, HTTPException  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from coding_team.job_store import (  # noqa: E402
    DEFAULT_CACHE_DIR,
    create_job,
    get_job,
    list_jobs,
    update_job,
)
from coding_team.models import CodingTeamPlanInput  # noqa: E402
from coding_team.orchestrator import run_coding_team_orchestrator  # noqa: E402
from shared_observability import init_otel, instrument_fastapi_app  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

init_otel(service_name="coding-team", team_key="coding_team")

app = FastAPI(
    title="Coding Team API",
    description="Tech Lead and Senior SWEs with Task Graph. POST /run to start a job; poll GET /status/{job_id}.",
)
instrument_fastapi_app(app, team_key="coding_team")


class RunRequest(BaseModel):
    """Request body for POST /run."""

    repo_path: str = Field(..., description="Path to the repository")
    plan_input: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional plan from Planning team (CodingTeamPlanInput); if omitted, job is created but orchestrator expects to be run in-process with plan_input.",
    )


class RunResponse(BaseModel):
    job_id: str
    status: str = "pending"
    message: str = "Job started. Poll GET /status/{job_id} for progress."


class StatusResponse(BaseModel):
    job_id: str
    status: str
    phase: Optional[str] = None
    status_text: Optional[str] = None
    repo_path: Optional[str] = None
    task_graph_snapshot: List[Dict[str, Any]] = Field(default_factory=list)
    agent_task_map: Dict[str, str] = Field(default_factory=dict)
    error: Optional[str] = None


class JobListItem(BaseModel):
    job_id: str
    status: str
    repo_path: Optional[str] = None
    phase: Optional[str] = None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "coding-team"}


@app.post("/run", response_model=RunResponse)
def post_run(request: RunRequest) -> RunResponse:
    """Start a coding_team job. If plan_input is provided, runs orchestrator in background."""
    job_id = str(uuid.uuid4())
    create_job(job_id=job_id, repo_path=request.repo_path, plan_input=request.plan_input)
    if request.plan_input:
        plan = CodingTeamPlanInput.model_validate(
            {**request.plan_input, "repo_path": request.repo_path}
        )

        def run() -> None:
            try:
                run_coding_team_orchestrator(
                    job_id,
                    request.repo_path,
                    plan,
                    update_job_fn=lambda **kw: update_job(job_id, **kw),
                    get_job_fn=lambda jid: get_job(jid),
                    cache_dir=DEFAULT_CACHE_DIR,
                )
            except Exception as e:
                logger.exception("Coding team orchestrator failed: %s", e)
                update_job(job_id, status="failed", error=str(e))

        t = threading.Thread(target=run, daemon=True)
        t.start()
    return RunResponse(job_id=job_id, status="pending")


@app.get("/status/{job_id}", response_model=StatusResponse)
def get_status(job_id: str) -> StatusResponse:
    """Get job status and task graph summary."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Job not found")
    return StatusResponse(
        job_id=data.get("job_id", job_id),
        status=data.get("status", "pending"),
        phase=data.get("phase"),
        status_text=data.get("status_text"),
        repo_path=data.get("repo_path"),
        task_graph_snapshot=data.get("task_graph_snapshot", []),
        agent_task_map=data.get("agent_task_map", {}),
        error=data.get("error"),
    )


@app.get("/jobs", response_model=List[JobListItem])
def get_jobs() -> List[JobListItem]:
    """List coding_team jobs."""
    jobs = list_jobs()
    return [
        JobListItem(
            job_id=j.get("job_id", ""),
            status=j.get("status", "pending"),
            repo_path=j.get("repo_path"),
            phase=j.get("phase"),
        )
        for j in jobs
    ]
