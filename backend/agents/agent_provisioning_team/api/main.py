"""
FastAPI endpoints for the Agent Provisioning Team.

Provides REST API for provisioning, status tracking, and deprovisioning.
"""

import concurrent.futures
import contextlib
import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from temporalio.exceptions import TemporalError

from job_service_client import (
    JOB_STATUS_FAILED,
    JOB_STATUS_INTERRUPTED,
    RESTARTABLE_STATUSES,
    validate_job_for_action,
)
from shared_observability import init_otel, instrument_fastapi_app  # noqa: E402

from ..models import (
    DeprovisionResponse,
    ProvisioningResult,
    ProvisionJobResponse,
    ProvisionJobsListResponse,
    ProvisionJobSummary,
    ProvisionRequest,
    ProvisionStatusResponse,
)
from ..shared.environment_queries import get_agent_status_dict, list_agent_status_dicts
from ..shared.environment_store import EnvironmentStore
from ..shared.job_store import (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    create_job,
    get_job,
    list_jobs,
    mark_job_failed,
    update_job,
)
from ..shared.job_store import (
    cancel_job as store_cancel_job,
)
from ..shared.job_store import (
    delete_job as store_delete_job,
)
from ..shared.job_store import (
    reset_job as store_reset_job,
)

logger = logging.getLogger(__name__)

init_otel(service_name="agent-provisioning-team", team_key="agent_provisioning")


_TEMPORAL_REQUIRED = "Temporal is required for agent provisioning (set TEMPORAL_ADDRESS)"

# Resume is for interrupted/failed jobs only. ``running``/``pending`` share a stable
# Temporal workflow id (``agent-provisioning-{job_id}``); a second start fails with
# WorkflowAlreadyStartedError while the original keeps running — marking failed on
# that error would corrupt a live job.
PROVISION_RESUMABLE_STATUSES: frozenset[str] = frozenset(
    {
        JOB_STATUS_FAILED,
        JOB_STATUS_INTERRUPTED,
        "agent_crash",
    }
)


def _is_indeterminate_workflow_start(exc: BaseException) -> bool:
    """True when Temporal may still have accepted the start (caller must not fail the job)."""
    return isinstance(exc, (TimeoutError, concurrent.futures.TimeoutError))


def _fail_job_after_start_error(job_id: str, exc: BaseException) -> None:
    """Mark ``job_id`` failed unless ``exc`` leaves start acceptance indeterminate."""
    if _is_indeterminate_workflow_start(exc):
        logger.warning(
            "Temporal start timed out for job=%s; not marking failed (workflow may still run): %s",
            job_id,
            exc,
        )
        return
    mark_job_failed(job_id, error=str(exc))


def _require_provision_starter():
    """Return the ``start_provisioning_workflow`` callable, or raise HTTP 503.

    Provisioning is Temporal-only: there is no in-process thread fallback, so
    every write endpoint that starts a workflow (``/provision``, resume,
    restart) must go through this helper before mutating job-store state.

    Preconditions:
        * None — reads process env / Temporal client state only.
    Postconditions:
        * Returns the ``start_provisioning_workflow`` callable when Temporal
          is enabled and importable.
        * Raises ``HTTPException(status_code=503)`` otherwise (Temporal
          disabled, or the ``temporal`` submodule fails to import).
    """
    try:
        from agent_provisioning_team.temporal.client import is_temporal_enabled
        from agent_provisioning_team.temporal.start_workflow import start_provisioning_workflow
    except ImportError as exc:
        raise HTTPException(status_code=503, detail=_TEMPORAL_REQUIRED) from exc
    if not is_temporal_enabled():
        raise HTTPException(status_code=503, detail=_TEMPORAL_REQUIRED)
    return start_provisioning_workflow


def _require_deprovision_runner():
    """Return the ``run_deprovision_workflow`` callable, or raise HTTP 503.

    Mirrors :func:`_require_provision_starter` for the deprovision path:
    deprovisioning is Temporal-only, no in-process orchestrator fallback.

    Preconditions:
        * None — reads process env / Temporal client state only.
    Postconditions:
        * Returns the ``run_deprovision_workflow`` callable when Temporal is
          enabled and importable.
        * Raises ``HTTPException(status_code=503)`` otherwise.
    """
    try:
        from agent_provisioning_team.temporal.client import is_temporal_enabled
        from agent_provisioning_team.temporal.start_workflow import run_deprovision_workflow
    except ImportError as exc:
        raise HTTPException(status_code=503, detail=_TEMPORAL_REQUIRED) from exc
    if not is_temporal_enabled():
        raise HTTPException(status_code=503, detail=_TEMPORAL_REQUIRED)
    return run_deprovision_workflow


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    """API lifespan.

    Provisioning/deprovisioning are Temporal-owned. On pod restart this process
    must not mark running jobs failed or compensate agents — that would race
    durable workflows that continue on the Temporal worker and may later update
    the same job_store rows.
    """
    yield


app = FastAPI(
    title="Agent Provisioning API",
    description="API for provisioning sandboxed environments and tool accounts for AI agents",
    version="1.0.0",
    lifespan=lifespan,
)
instrument_fastapi_app(app, team_key="agent_provisioning")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_environment_store = EnvironmentStore()


def _get_agent_status(agent_id: str) -> Optional[Dict[str, Any]]:
    """Read-only agent status via shared ``EnvironmentStore`` queries."""
    return get_agent_status_dict(_environment_store, agent_id)


def _list_agents(status: Optional[str] = None) -> List[Dict[str, Any]]:
    """List provisioned agents via shared ``EnvironmentStore`` queries."""
    return list_agent_status_dicts(_environment_store, status=status)


@app.post(
    "/provision",
    response_model=ProvisionJobResponse,
    summary="Start provisioning job",
    description="Start an asynchronous provisioning job for a new agent. "
    "Returns a job_id to poll for status.",
)
def start_provisioning(request: ProvisionRequest) -> ProvisionJobResponse:
    """Start a new provisioning job.

    Preconditions:
        * Temporal must be enabled (raises HTTP 503 otherwise, before any
          job-store row is created).
    Postconditions:
        * On success, a job row exists in ``JOB_STATUS_RUNNING`` and the
          returned ``ProvisionJobResponse`` carries its ``job_id``.
        * If dispatch to Temporal itself fails after the job row was
          created, the job is marked failed and HTTP 503 is raised.
    """
    starter = _require_provision_starter()

    job_id = str(uuid.uuid4())
    create_job(
        job_id=job_id,
        agent_id=request.agent_id,
        manifest_path=request.manifest_path,
    )

    try:
        starter(
            job_id,
            request.agent_id,
            request.manifest_path,
            skip_phases=None,
            prior_results=None,
        )
    except Exception as exc:
        _fail_job_after_start_error(job_id, exc)
        raise HTTPException(
            status_code=503, detail=f"Failed to start Temporal workflow: {exc}"
        ) from exc

    return ProvisionJobResponse(
        job_id=job_id,
        status=JOB_STATUS_RUNNING,
        message="Provisioning started (Temporal). Poll GET /provision/status/{job_id} for progress.",
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


@app.post(
    "/provision/job/{job_id}/resume",
    response_model=ProvisionJobResponse,
    summary="Resume an interrupted provisioning job",
    description="Re-enter the provisioning pipeline at the last completed phase.",
)
def resume_provision_job(job_id: str) -> ProvisionJobResponse:
    """Resume a provisioning job from its last checkpoint.

    Preconditions:
        * Temporal must be enabled (raises HTTP 503 otherwise, before the
          job's status is mutated).
    Postconditions:
        * On success, the job is set to ``JOB_STATUS_RUNNING`` and the
          workflow is restarted skipping ``completed_phases``.
    """
    try:
        data = validate_job_for_action(
            get_job(job_id), job_id, PROVISION_RESUMABLE_STATUSES, "resumed"
        )
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=code, detail=str(exc)) from exc

    agent_id = data.get("agent_id")
    manifest_path = data.get("manifest_path")
    if not agent_id or not manifest_path:
        raise HTTPException(status_code=400, detail="Job is missing agent_id or manifest_path.")

    completed = data.get("completed_phases", [])
    phase_results = data.get("phase_results", {})

    from ..models import Phase

    phase_values = {ph.value for ph in Phase}
    completed_values = [p for p in completed if p in phase_values]

    starter = _require_provision_starter()
    update_job(job_id, status=JOB_STATUS_RUNNING, error=None)

    try:
        starter(
            job_id,
            agent_id,
            manifest_path,
            skip_phases=completed_values,
            prior_results=phase_results,
            replace_existing=True,
        )
    except Exception as exc:
        _fail_job_after_start_error(job_id, exc)
        raise HTTPException(
            status_code=503, detail=f"Failed to resume Temporal workflow: {exc}"
        ) from exc

    return ProvisionJobResponse(
        job_id=job_id,
        status="running",
        message="Job resumed (Temporal). Skipping completed phases.",
    )


@app.post(
    "/provision/job/{job_id}/restart",
    response_model=ProvisionJobResponse,
    summary="Restart a provisioning job from scratch",
    description="Reset the job and re-run the full pipeline with the same inputs.",
)
def restart_provision_job(job_id: str) -> ProvisionJobResponse:
    """Restart a provisioning job from the beginning.

    Preconditions:
        * Temporal must be enabled (raises HTTP 503 otherwise, before the
          job is reset).
    Postconditions:
        * On success, the job is reset and a fresh workflow run is started
          with no skipped phases.
    """
    try:
        data = validate_job_for_action(get_job(job_id), job_id, RESTARTABLE_STATUSES, "restarted")
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=code, detail=str(exc)) from exc

    agent_id = data.get("agent_id")
    manifest_path = data.get("manifest_path")
    if not agent_id or not manifest_path:
        raise HTTPException(status_code=400, detail="Job is missing agent_id or manifest_path.")

    starter = _require_provision_starter()
    store_reset_job(job_id)

    try:
        starter(
            job_id,
            agent_id,
            manifest_path,
            skip_phases=None,
            prior_results=None,
            replace_existing=True,
        )
    except Exception as exc:
        _fail_job_after_start_error(job_id, exc)
        raise HTTPException(
            status_code=503, detail=f"Failed to restart Temporal workflow: {exc}"
        ) from exc

    return ProvisionJobResponse(
        job_id=job_id,
        status="running",
        message="Job restarted (Temporal) from scratch.",
    )


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
    """Deprovision an agent and remove all resources.

    Runs as a durable ``AgentDeprovisioningWorkflow`` (execute-and-wait, so
    the response shape is unchanged). This handler is a sync ``def`` —
    FastAPI runs it in its threadpool — so the blocking execute-and-wait
    dispatch does not stall the event loop.

    Preconditions:
        * Temporal must be enabled and the shared client/loop must be ready
          (raises HTTP 503 otherwise).
    Postconditions:
        * Pre-start / Temporal-unavailable failures raise ``HTTPException(503)``.
        * Once execute-and-wait has begun, workflow/application failures and
          client-wait timeouts (``TimeoutError``) are returned as
          ``DeprovisionResponse(success=False, ...)`` (not 500).
    """
    runner = _require_deprovision_runner()
    try:
        return DeprovisionResponse.model_validate(runner(agent_id, force))
    except HTTPException:
        raise
    except RuntimeError as exc:
        # execute_workflow_sync raises this when the shared Temporal client/loop
        # never becomes available — same "Temporal required" contract as provision.
        if "Temporal client not available" in str(exc):
            raise HTTPException(
                status_code=503,
                detail=f"{_TEMPORAL_REQUIRED}: {exc}",
            ) from exc
        logger.exception("Durable deprovision failed for agent=%s", agent_id)
        return DeprovisionResponse(
            agent_id=agent_id,
            success=False,
            details={},
            error=f"Deprovision workflow failed: {exc}",
        )
    except TemporalError as exc:
        # Expected Temporal / workflow execution failures — return as payload, not 500.
        logger.exception("Durable deprovision failed for agent=%s", agent_id)
        return DeprovisionResponse(
            agent_id=agent_id,
            success=False,
            details={},
            error=f"Deprovision workflow failed: {exc}",
        )
    except (TimeoutError, concurrent.futures.TimeoutError) as exc:
        # Client wait exceeded DEPROVISION_CLIENT_TIMEOUT_S; the workflow may
        # still be running on Temporal — do not 500 the HTTP caller.
        logger.exception("Durable deprovision timed out for agent=%s", agent_id)
        return DeprovisionResponse(
            agent_id=agent_id,
            success=False,
            details={},
            error=f"Deprovision workflow timed out waiting for result: {exc}",
        )


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
    status = _get_agent_status(agent_id)

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
    agents_data = _list_agents(status=status)

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
