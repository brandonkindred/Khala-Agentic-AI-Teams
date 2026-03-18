"""FastAPI endpoints for the StudioGrid design-system workflow."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from studiogrid.runtime.runtime_factory import build_orchestrator

app = FastAPI(title="StudioGrid API", version="1.0.0")


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class StartRunRequest(BaseModel):
    project_name: str = Field(..., min_length=1, max_length=500)
    intake: dict[str, Any] = Field(..., description="Intake payload (free-form JSON)")
    idempotency_key: str | None = Field(
        default=None, description="Client-supplied idempotency key; a UUID is generated if omitted"
    )


class StartRunResponse(BaseModel):
    project_id: str
    run_id: str
    status: str
    phase: str


class ResolveDecisionRequest(BaseModel):
    option: str = Field(..., min_length=1, description="The option key to select")


class FindAgentsRequest(BaseModel):
    problem: str = Field(..., min_length=1)
    skills: list[str] = Field(default_factory=list)
    limit: int = Field(default=5, ge=1, le=20)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post("/studio-grid/runs", response_model=StartRunResponse)
def start_run(payload: StartRunRequest) -> StartRunResponse:
    """Create a new design project and run, persisting the intake artifact."""
    orch = build_orchestrator()
    idem_key = payload.idempotency_key or uuid.uuid4().hex
    project_id = orch.create_project(
        name=payload.project_name,
        idempotency_key=f"project:create:{idem_key}",
    )
    ctx = orch.create_run(
        project_id=project_id,
        idempotency_key=f"{project_id}:CreateRun:{idem_key}",
    )
    orch.persist_artifact(
        ctx=ctx,
        artifact_payload={"artifact_type": "intake", "format": "json", "payload": payload.intake},
        raw_bytes=None,
        idempotency_key=f"{ctx.run_id}:PersistArtifact:intake:v1",
    )
    return StartRunResponse(
        project_id=project_id,
        run_id=ctx.run_id,
        status="RUNNING",
        phase="INTAKE",
    )


@app.get("/studio-grid/runs/{run_id}")
def get_run_status(run_id: str) -> dict[str, Any]:
    """Get the current status and phase of a design run."""
    orch = build_orchestrator()
    run = orch.store.runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    return run


@app.get("/studio-grid/runs/{run_id}/decisions")
def list_decisions(run_id: str) -> dict[str, Any]:
    """List all decisions (human approval checkpoints) for a run."""
    orch = build_orchestrator()
    if run_id not in orch.store.runs:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    decisions = [row for row in orch.store.decisions.values() if row["run_id"] == run_id]
    return {"run_id": run_id, "decisions": decisions}


@app.post("/studio-grid/decisions/{decision_id}/resolve")
def resolve_decision(decision_id: str, payload: ResolveDecisionRequest) -> dict[str, Any]:
    """Resolve a pending decision to unblock a run."""
    orch = build_orchestrator()
    if decision_id not in orch.store.decisions:
        raise HTTPException(status_code=404, detail=f"Decision '{decision_id}' not found")
    orch.resolve_decision(
        decision_id=decision_id,
        selected_option_key=payload.option,
        idempotency_key=f"decision:resolve:{decision_id}:{payload.option}",
    )
    return orch.get_decision(decision_id=decision_id)


@app.get("/studio-grid/registry/agents")
def list_agents() -> dict[str, Any]:
    """List all agents in the registry."""
    from pathlib import Path

    from studiogrid.runtime.registry_loader import RegistryLoader

    root = Path(__file__).resolve().parents[2]
    registry = RegistryLoader(root)
    return {"agents": registry.list_agents()}


@app.post("/studio-grid/registry/find")
def find_agents(payload: FindAgentsRequest) -> dict[str, Any]:
    """Find agents matching a problem description and required skills."""
    from pathlib import Path

    from studiogrid.runtime.registry_loader import RegistryLoader

    root = Path(__file__).resolve().parents[2]
    registry = RegistryLoader(root)
    candidates = registry.find_assisting_agents(
        problem_description=payload.problem,
        required_skills=payload.skills,
        limit=payload.limit,
    )
    return {
        "problem": payload.problem,
        "required_skills": payload.skills,
        "assisting_agents": candidates,
        "should_spawn_sub_agents": len(candidates) == 0,
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
