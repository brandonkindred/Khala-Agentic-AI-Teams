"""
FastAPI endpoints for the Agent Provisioning Team.

Provides REST API for provisioning, status tracking, and deprovisioning.
"""

import threading
import uuid
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from ..models import (
    AccessTier,
    DeprovisionResponse,
    ProvisioningResult,
    ProvisionJobResponse,
    ProvisionJobsListResponse,
    ProvisionJobSummary,
    ProvisionRequest,
    ProvisionStatusResponse,
)
from ..orchestrator import ProvisioningOrchestrator
from ..phases.deliver import redact_credentials_for_response
from ..shared.job_store import (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    create_job,
    get_job,
    list_jobs,
    mark_job_completed,
    mark_job_failed,
    mark_job_running,
    update_job,
)
from ..shared.job_store import (
    cancel_job as store_cancel_job,
)
from ..shared.job_store import (
    delete_job as store_delete_job,
)

app = FastAPI(
    title="Agent Provisioning API",
    description="API for provisioning sandboxed environments and tool accounts for AI agents",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

orchestrator = ProvisioningOrchestrator()


def _run_provisioning_background(
    job_id: str,
    agent_id: str,
    manifest_path: str,
    access_tier: AccessTier,
) -> None:
    """Background thread function for running provisioning workflow."""
    try:
        mark_job_running(job_id)

        def job_updater(
            current_phase: Optional[str] = None,
            progress: Optional[int] = None,
            current_tool: Optional[str] = None,
            tools_completed: Optional[int] = None,
            tools_total: Optional[int] = None,
            status_text: Optional[str] = None,
        ) -> None:
            """Callback to update job status during workflow execution."""
            updates: Dict[str, Any] = {}

            if current_phase is not None:
                updates["current_phase"] = current_phase
            if progress is not None:
                updates["progress"] = progress
            if current_tool is not None:
                updates["current_tool"] = current_tool
            if tools_completed is not None:
                updates["tools_completed"] = tools_completed
            if tools_total is not None:
                updates["tools_total"] = tools_total
            if status_text is not None:
                updates["status_text"] = status_text

            if updates:
                update_job(job_id, **updates)

        result = orchestrator.run_workflow(
            agent_id=agent_id,
            manifest_path=manifest_path,
            access_tier=access_tier,
            job_updater=job_updater,
        )

        if result.success:
            redacted = redact_credentials_for_response(result)
            mark_job_completed(job_id, result=redacted.model_dump())
        else:
            mark_job_failed(job_id, error=result.error or "Provisioning failed")

    except Exception as e:
        mark_job_failed(job_id, error=str(e))


@app.post(
    "/provision",
    response_model=ProvisionJobResponse,
    summary="Start provisioning job",
    description="Start an asynchronous provisioning job for a new agent. "
    "Returns a job_id to poll for status.",
)
def start_provisioning(request: ProvisionRequest) -> ProvisionJobResponse:
    """Start a new provisioning job."""
    job_id = str(uuid.uuid4())

    create_job(
        job_id=job_id,
        agent_id=request.agent_id,
        manifest_path=request.manifest_path,
        access_tier=request.access_tier.value,
    )

    try:
        from agent_provisioning_team.temporal.client import is_temporal_enabled
        from agent_provisioning_team.temporal.start_workflow import start_provisioning_workflow

        if is_temporal_enabled():
            start_provisioning_workflow(
                job_id,
                request.agent_id,
                request.manifest_path,
                request.access_tier.value,
            )
            return ProvisionJobResponse(
                job_id=job_id,
                status=JOB_STATUS_RUNNING,
                message="Provisioning started (Temporal). Poll GET /provision/status/{job_id} for progress.",
            )
    except ImportError:
        pass

    thread = threading.Thread(
        target=_run_provisioning_background,
        args=(job_id, request.agent_id, request.manifest_path, request.access_tier),
        daemon=True,
    )
    thread.start()

    return ProvisionJobResponse(
        job_id=job_id,
        status=JOB_STATUS_RUNNING,
        message="Provisioning started. Poll GET /provision/status/{job_id} for progress.",
    )


@app.get(
    "/provision/status/{job_id}",
    response_model=ProvisionStatusResponse,
    summary="Get provisioning job status",
    description="Get the current status of a provisioning job including phase progress.",
)
def get_provisioning_status(job_id: str) -> ProvisionStatusResponse:
    """Get status of a provisioning job."""
    data = get_job(job_id)

    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    result = None
    if data.get("status") == JOB_STATUS_COMPLETED and data.get("result"):
        result = ProvisioningResult(**data["result"])

    return ProvisionStatusResponse(
        job_id=job_id,
        status=data.get("status", JOB_STATUS_PENDING),
        agent_id=data.get("agent_id"),
        current_phase=data.get("current_phase"),
        current_tool=data.get("current_tool"),
        progress=data.get("progress", 0),
        tools_completed=data.get("tools_completed", 0),
        tools_total=data.get("tools_total", 0),
        completed_phases=data.get("completed_phases", []),
        error=data.get("error"),
        result=result,
    )


@app.get(
    "/provision/jobs",
    response_model=ProvisionJobsListResponse,
    summary="List provisioning jobs",
    description="List all provisioning jobs, optionally filtered to running only.",
)
def list_provisioning_jobs(
    running_only: bool = Query(False, description="Filter to running/pending jobs only"),
) -> ProvisionJobsListResponse:
    """List all provisioning jobs."""
    jobs_data = list_jobs(running_only=running_only)

    jobs = [
        ProvisionJobSummary(
            job_id=j["job_id"],
            agent_id=j.get("agent_id", ""),
            status=j.get("status", JOB_STATUS_PENDING),
            created_at=j.get("created_at"),
            current_phase=j.get("current_phase"),
            progress=j.get("progress", 0),
        )
        for j in jobs_data
    ]

    return ProvisionJobsListResponse(jobs=jobs)


class CancelProvisionJobResponse(BaseModel):
    job_id: str
    status: str = "cancelled"
    message: str = "Job cancellation requested."


class DeleteProvisionJobResponse(BaseModel):
    job_id: str
    message: str = "Job deleted."


@app.post(
    "/provision/job/{job_id}/cancel",
    response_model=CancelProvisionJobResponse,
    summary="Cancel a provisioning job",
    description="Set job status to cancelled. Only allowed for pending or running jobs.",
)
def cancel_provision_job(job_id: str) -> CancelProvisionJobResponse:
    """Cancel a pending or running provisioning job."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    current = data.get("status", JOB_STATUS_PENDING)
    if current not in (JOB_STATUS_PENDING, JOB_STATUS_RUNNING):
        raise HTTPException(
            status_code=400,
            detail=f"Job is already in terminal state: {current}. Cannot cancel.",
        )
    store_cancel_job(job_id)
    return CancelProvisionJobResponse(job_id=job_id, message="Job cancellation requested.")


@app.delete(
    "/provision/job/{job_id}",
    response_model=DeleteProvisionJobResponse,
    summary="Delete a provisioning job",
    description="Remove the job from the store. Returns 404 if not found.",
)
def delete_provision_job(job_id: str) -> DeleteProvisionJobResponse:
    """Delete a provisioning job by id."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if not store_delete_job(job_id):
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return DeleteProvisionJobResponse(job_id=job_id, message="Job deleted.")


@app.delete(
    "/environments/{agent_id}",
    response_model=DeprovisionResponse,
    summary="Deprovision an agent",
    description="Remove all resources and access for an agent.",
)
def deprovision_agent(
    agent_id: str,
    force: bool = Query(False, description="Force removal even if errors occur"),
) -> DeprovisionResponse:
    """Deprovision an agent and remove all resources."""
    return orchestrator.deprovision(agent_id, force=force)


class AgentStatusResponse(BaseModel):
    """Response for agent status queries."""

    agent_id: str
    status: str
    container_id: Optional[str] = None
    container_name: Optional[str] = None
    tools_provisioned: List[str] = Field(default_factory=list)
    created_at: Optional[str] = None


@app.get(
    "/environments/{agent_id}",
    response_model=AgentStatusResponse,
    summary="Get agent environment status",
    description="Get the current status of a provisioned agent environment.",
)
def get_agent_status(agent_id: str) -> AgentStatusResponse:
    """Get status of a provisioned agent."""
    status = orchestrator.get_agent_status(agent_id)

    if status is None:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    return AgentStatusResponse(**status)


class AgentListResponse(BaseModel):
    """Response for listing agents."""

    agents: List[AgentStatusResponse] = Field(default_factory=list)


@app.get(
    "/environments",
    response_model=AgentListResponse,
    summary="List provisioned agents",
    description="List all provisioned agent environments.",
)
def list_agents(
    status: Optional[str] = Query(None, description="Filter by status (running, ready, etc.)"),
) -> AgentListResponse:
    """List all provisioned agents."""
    agents_data = orchestrator.list_agents(status=status)

    agents = [
        AgentStatusResponse(
            agent_id=a["agent_id"],
            status=a["status"],
            container_name=a.get("container_name"),
            tools_provisioned=a.get("tools_provisioned", []),
            created_at=a.get("created_at"),
        )
        for a in agents_data
    ]

    return AgentListResponse(agents=agents)


@app.get("/health", summary="Health check")
def health_check() -> Dict[str, str]:
    """Health check endpoint."""
    return {"status": "healthy", "service": "agent-provisioning"}


@app.get("/", summary="API info")
def api_info() -> Dict[str, str]:
    """API information endpoint."""
    return {
        "service": "Agent Provisioning API",
        "version": "1.0.0",
        "docs": "/docs",
    }
