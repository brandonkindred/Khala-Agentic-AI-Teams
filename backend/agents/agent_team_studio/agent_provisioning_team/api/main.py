"""
FastAPI endpoints for the Agent Provisioning Team.

Provides REST API for provisioning, status tracking, and deprovisioning.
"""

import concurrent.futures
import contextlib
import logging
import os
import uuid
from typing import Any, Dict, List, Optional, Protocol, Union

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError
from temporalio.exceptions import TemporalError, WorkflowAlreadyStartedError

from job_service_client import (
    JOB_STATUS_FAILED,
    JOB_STATUS_INTERRUPTED,
    RESTARTABLE_STATUSES,
    validate_job_for_action,
)
from shared.observability import init_otel, instrument_fastapi_app  # noqa: E402

from ..models import (
    DeprovisionResponse,
    Phase,
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
from ..shared.path_safety import safe_path_component
from ..shared.visibility_query import find_open_pre_patch_executions

logger = logging.getLogger(__name__)

init_otel(service_name="agent-provisioning-team", team_key="agent_provisioning")


_TEMPORAL_REQUIRED_MESSAGE = "Temporal is required for agent provisioning (set TEMPORAL_ADDRESS)"

# Env var gating the rollout drain gate below. Defaults to enabled: an
# unset/unrecognized value must never silently turn the gate off. Documented
# in docs/ENV_VARS.md alongside the cutoff var.
DRAIN_GATE_ENABLED_ENV_VAR = "AGENT_PROVISIONING_DRAIN_GATE_ENABLED"
_DRAIN_GATE_DISABLE_VALUES = frozenset({"0", "false", "no", "off"})

# How long a rejected caller should wait before retrying, surfaced via the
# response's Retry-After header. Not tied to any workflow timeout — this is
# just a reasonable client-side backoff hint.
_DRAIN_GATE_RETRY_AFTER_S = 30

# The visibility query's own client-ready wait: on a request path (unlike the
# gate's originating rollout runbook use case) this must fail fast rather than
# block the caller for the library default's full ready-wait ceiling.
_DRAIN_GATE_CLIENT_READY_TIMEOUT_S = 2.0


def _drain_gate_enabled() -> bool:
    """Whether the pre-patch drain gate should run before a new request.

    Preconditions:
        * None.
    Postconditions:
        * Returns ``False`` only when ``DRAIN_GATE_ENABLED_ENV_VAR`` is set to
          one of ``_DRAIN_GATE_DISABLE_VALUES`` (case-insensitive).
        * Returns ``True`` otherwise, including when the var is unset —
          fail-safe default is "gate on", matching this rollout's goal of not
          silently reopening the race it exists to close.
    """
    return (
        os.environ.get(DRAIN_GATE_ENABLED_ENV_VAR, "").strip().lower()
        not in _DRAIN_GATE_DISABLE_VALUES
    )


def _reject_if_pre_patch_open(agent_id: str) -> Optional[JSONResponse]:
    """Refuse a new provisioning/deprovisioning request racing an open pre-patch execution.

    Preconditions:
        * ``agent_id`` is non-empty.
    Postconditions:
        * Returns ``None`` when the gate is disabled, the visibility query
          could not be answered (Temporal client not ready / query timeout —
          logged and treated as fail-open so a visibility-RPC hiccup never
          blocks all provisioning/deprovisioning traffic), or no open
          pre-patch execution exists for ``agent_id``: the caller should
          proceed.
        * Returns a ``JSONResponse(status_code=409, headers={"Retry-After": ...})``
          when at least one open pre-patch execution exists for ``agent_id``:
          the caller must return this response as-is instead of proceeding.
    """
    assert agent_id, "agent_id must be non-empty"
    if not _drain_gate_enabled():
        return None
    try:
        blocking = find_open_pre_patch_executions(
            agent_id=agent_id, client_ready_timeout_s=_DRAIN_GATE_CLIENT_READY_TIMEOUT_S
        )
    except (RuntimeError, concurrent.futures.TimeoutError) as exc:
        logger.warning(
            "Drain-gate visibility check failed for agent_id=%s; proceeding without it (%s)",
            agent_id,
            exc,
        )
        return None
    if not blocking:
        return None
    logger.warning(
        "Refusing request for agent_id=%s: %d open pre-lock-patch execution(s) still running (%s)",
        agent_id,
        len(blocking),
        [b.workflow_id for b in blocking],
    )
    return JSONResponse(
        status_code=409,
        headers={"Retry-After": str(_DRAIN_GATE_RETRY_AFTER_S)},
        content={
            "detail": (
                f"Cannot start a new request for agent_id={agent_id!r}: "
                f"{len(blocking)} pre-lock-patch workflow execution(s) for this agent are "
                "still open. Retry once they have drained."
            ),
            "agent_id": agent_id,
            "open_pre_patch_executions": len(blocking),
        },
    )


def _require_safe_agent_id(agent_id: str) -> str:
    """Validate an ``agent_id`` path parameter before it reaches the stores.

    Path parameters bypass the request-model ``field_validator``, so the
    ``{agent_id}`` routes call this to mirror that guard. Returns ``agent_id``
    unchanged, or raises ``HTTPException(422)`` if it is not a safe filename
    component — so a traversal attempt is a clean client error, not a 500.
    """
    try:
        return safe_path_component(agent_id, kind="agent_id")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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

# When start acceptance times out we leave pending/running; those are recoverable
# only if Temporal has no open execution for the stable workflow id. Replacing an
# open live run would terminate it without compensation and can leak resources.
_STRANDED_START_STATUSES: frozenset[str] = frozenset({JOB_STATUS_PENDING, JOB_STATUS_RUNNING})


def _is_indeterminate_workflow_start(exc: BaseException) -> bool:
    """True when Temporal may still have accepted the start (caller must not fail the job)."""
    return isinstance(exc, (TimeoutError, concurrent.futures.TimeoutError))


def _fail_job_after_start_error(job_id: str, exc: BaseException) -> None:
    """Mark ``job_id`` failed unless ``exc`` leaves start acceptance indeterminate.

    Preconditions:
        * ``job_id`` is a non-empty string identifying an existing job row
          (or a row the job service will accept updates for).
        * ``exc`` is the exception raised while dispatching the Temporal start.
    Postconditions:
        * If ``exc`` is an indeterminate workflow-start timeout
          (``TimeoutError`` / ``concurrent.futures.TimeoutError``): the job
          store is left unchanged and a warning is logged — Temporal may still
          accept and run the workflow.
        * If ``exc`` is ``WorkflowAlreadyStartedError``: the job is left
          unchanged (a concurrent start already owns the live run).
        * Otherwise: the job is marked failed via ``mark_job_failed`` with
          ``error=str(exc)``.
    """
    assert job_id, "job_id must be non-empty"
    if _is_indeterminate_workflow_start(exc):
        logger.warning(
            "Temporal start timed out for job=%s; not marking failed (workflow may still run): %s",
            job_id,
            exc,
        )
        return
    if isinstance(exc, WorkflowAlreadyStartedError):
        logger.warning(
            "Temporal workflow already started for job=%s; not marking failed: %s",
            job_id,
            exc,
        )
        return
    mark_job_failed(job_id, error=str(exc))


class ProvisionStarter(Protocol):
    """Callable that starts ``AgentProvisioningWorkflow`` for a job."""

    def __call__(
        self,
        job_id: str,
        agent_id: str,
        manifest_path: str,
        *,
        skip_phases: Optional[List[str]] = None,
        prior_results: Optional[Dict[str, Any]] = None,
        replace_existing: bool = False,
    ) -> None: ...


def _invoke_provision_starter(
    starter: ProvisionStarter,
    *,
    job_id: str,
    agent_id: str,
    manifest_path: str,
    skip_phases: Optional[List[str]],
    prior_results: Optional[Dict[str, Any]],
    success_message: str,
    failure_verb: str,
    replace_existing: bool = False,
    indeterminate_status: str = JOB_STATUS_PENDING,
) -> ProvisionJobResponse:
    """Dispatch a provision workflow start and normalize success / error responses.

    Preconditions:
        * ``starter`` is the callable returned by ``_require_provision_starter``.
        * ``job_id``, ``agent_id``, and ``manifest_path`` are non-empty.
        * Job-store mutations that prepare the row (create / update / reset)
          have already run for ``job_id``.
    Postconditions:
        * On accepted start: returns ``ProvisionJobResponse`` with
          ``status=running`` and ``success_message``.
        * On indeterminate start timeout: does **not** mark the job failed;
          returns ``ProvisionJobResponse`` with ``job_id`` and
          ``indeterminate_status`` so the caller can poll
          ``GET /provision/status/{job_id}``.
        * On ``WorkflowAlreadyStartedError``: raises HTTP 409 without failing
          the job (concurrent resume/restart lost the race).
        * On other start failures: marks the job failed (via
          ``_fail_job_after_start_error``) and raises HTTP 503.
    """
    assert job_id, "job_id must be non-empty"
    assert agent_id, "agent_id must be non-empty"
    assert manifest_path, "manifest_path must be non-empty"
    try:
        kwargs: Dict[str, Any] = {
            "skip_phases": skip_phases,
            "prior_results": prior_results,
        }
        if replace_existing:
            kwargs["replace_existing"] = True
        starter(job_id, agent_id, manifest_path, **kwargs)
    except WorkflowAlreadyStartedError as exc:
        _fail_job_after_start_error(job_id, exc)
        raise HTTPException(
            status_code=409,
            detail=(
                f"A provisioning workflow is already running for job {job_id}; "
                f"poll GET /provision/status/{job_id}."
            ),
        ) from exc
    except Exception as exc:
        _fail_job_after_start_error(job_id, exc)
        if _is_indeterminate_workflow_start(exc):
            return ProvisionJobResponse(
                job_id=job_id,
                status=indeterminate_status,
                message=(
                    f"Temporal {failure_verb} acceptance timed out; the workflow may still "
                    f"run. Poll GET /provision/status/{job_id}."
                ),
            )
        raise HTTPException(
            status_code=503,
            detail=f"Failed to {failure_verb} Temporal workflow: {exc}",
        ) from exc
    return ProvisionJobResponse(
        job_id=job_id,
        status=JOB_STATUS_RUNNING,
        message=success_message,
    )


def _validate_job_for_reprovision(
    job_id: str,
    allowed_statuses: frozenset[str],
    action_label: str,
) -> Dict[str, Any]:
    """Validate resume/restart without replacing a live Temporal execution.

    Preconditions:
        * ``job_id`` is non-empty.
        * ``allowed_statuses`` is the normal gate for ``action_label``.
    Postconditions:
        * Returns job data when status is allowed (or stranded pending/running)
          and no open Temporal workflow exists for the stable id.
        * Raises ``ValueError`` when the job is missing, has an open workflow,
          or is otherwise not actionable.
    """
    data = get_job(job_id)
    if not data:
        raise ValueError(f"Job {job_id} not found")
    try:
        validate_job_for_action(data, job_id, allowed_statuses, action_label)
    except ValueError:
        status = data.get("status", JOB_STATUS_PENDING)
        if status not in _STRANDED_START_STATUSES:
            raise
        logger.info(
            "Candidate stranded job=%s status=%s for %s; checking Temporal open state",
            job_id,
            status,
            action_label,
        )
    # Deferred import: api.main is loaded at process boot; keep Temporal
    # start_workflow off the top-level import graph to avoid cycles with
    # shared Temporal client/worker wiring.
    from agent_team_studio.agent_provisioning_team.temporal.start_workflow import (
        provisioning_workflow_is_open,
    )

    if provisioning_workflow_is_open(job_id):
        raise ValueError(f"Job {job_id} has an active Temporal workflow; cannot be {action_label}")
    return data


def _ensure_temporal_enabled() -> None:
    """Raise HTTP 503 when Temporal is disabled or unavailable.

    Preconditions:
        * None — reads process env / Temporal client state only.
    Postconditions:
        * Returns normally when Temporal is enabled.
        * Raises ``HTTPException(status_code=503)`` when Temporal is disabled,
          the temporal client module fails to import, or the availability check
          raises.
    """
    try:
        from shared.temporal.client import is_temporal_enabled
    except ImportError as exc:
        raise HTTPException(status_code=503, detail=_TEMPORAL_REQUIRED_MESSAGE) from exc
    try:
        enabled = is_temporal_enabled()
    except Exception as exc:
        logger.exception("Temporal availability check failed")
        raise HTTPException(status_code=503, detail=_TEMPORAL_REQUIRED_MESSAGE) from exc
    if not enabled:
        raise HTTPException(status_code=503, detail=_TEMPORAL_REQUIRED_MESSAGE)


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
    _ensure_temporal_enabled()
    try:
        from agent_team_studio.agent_provisioning_team.temporal.start_workflow import (
            start_provisioning_workflow,
        )
    except ImportError as exc:
        raise HTTPException(status_code=503, detail=_TEMPORAL_REQUIRED_MESSAGE) from exc
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
    _ensure_temporal_enabled()
    try:
        from agent_team_studio.agent_provisioning_team.temporal.start_workflow import (
            run_deprovision_workflow,
        )
    except ImportError as exc:
        raise HTTPException(status_code=503, detail=_TEMPORAL_REQUIRED_MESSAGE) from exc
    return run_deprovision_workflow


def _start_temporal_worker_backstop() -> None:
    """Start the Temporal worker when serving the app outside team_service.

    The team_service entrypoint normally starts the worker via
    ``TEAM_TEMPORAL_WORKER_MODULE`` / ``TEAM_TEMPORAL_WORKER_FUNC`` before
    uvicorn accepts requests. This backstop covers standalone runs
    (``uvicorn agent_team_studio.agent_provisioning_team.api.main:app``) after package import
    stopped auto-booting the worker. Idempotent with the entrypoint path.

    Preconditions:
        - None (safe to call once at app startup).

    Postconditions:
        - Starts the worker thread when Temporal is enabled; a no-op when
          ``TEMPORAL_ADDRESS`` is unset. Never raises — any failure is logged
          as a warning so it cannot abort app boot.
    """
    try:
        from agent_team_studio.agent_provisioning_team.temporal.worker import (
            start_agent_provisioning_temporal_worker_thread,
        )

        start_agent_provisioning_temporal_worker_thread()
    except Exception:  # noqa: BLE001 - backstop must not abort app boot
        logger.warning(
            "agent_provisioning Temporal worker start (lifespan backstop) failed",
            exc_info=True,
        )


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    """API lifespan.

    Starts the Temporal worker as a standalone-dev backstop, then yields.
    Provisioning/deprovisioning are Temporal-owned: on pod restart this process
    must not mark running jobs failed or compensate agents — that would race
    durable workflows that continue on the Temporal worker and may later update
    the same job_store rows.

    Preconditions:
        - ``app`` is the FastAPI application being started.
    Postconditions:
        - Worker backstop has been invoked (best-effort) before the first
          request is accepted; no shutdown compensation of Temporal-owned jobs.
    """
    _start_temporal_worker_backstop()
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
def start_provisioning(request: ProvisionRequest) -> Union[ProvisionJobResponse, JSONResponse]:
    """Start a new provisioning job.

    Preconditions:
        * Temporal must be enabled (raises HTTP 503 otherwise, before any
          job-store row is created).
    Postconditions:
        * On accepted Temporal start: a job row exists and the returned
          ``ProvisionJobResponse`` carries its ``job_id`` with ``running`` status.
        * On indeterminate start timeout: the job is left non-terminal and the
          response still includes ``job_id`` so the client can poll status.
        * On other Temporal dispatch failures: the job is marked failed and
          HTTP 503 is raised.
        * If an open pre-lock-patch execution still exists for
          ``request.agent_id``, no job-store row is created and a
          ``JSONResponse(status_code=409)`` is returned instead.
    """
    starter = _require_provision_starter()

    drain_gate_rejection = _reject_if_pre_patch_open(request.agent_id)
    if drain_gate_rejection is not None:
        return drain_gate_rejection

    job_id = str(uuid.uuid4())
    create_job(
        job_id=job_id,
        agent_id=request.agent_id,
        manifest_path=request.manifest_path,
    )

    return _invoke_provision_starter(
        starter,
        job_id=job_id,
        agent_id=request.agent_id,
        manifest_path=request.manifest_path,
        skip_phases=None,
        prior_results=None,
        success_message=(
            "Provisioning started (Temporal). Poll GET /provision/status/{job_id} for progress."
        ).format(job_id=job_id),
        failure_verb="start",
        indeterminate_status=JOB_STATUS_PENDING,
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
    starter = _require_provision_starter()
    try:
        data = _validate_job_for_reprovision(job_id, PROVISION_RESUMABLE_STATUSES, "resumed")
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=code, detail=str(exc)) from exc

    agent_id = data.get("agent_id")
    manifest_path = data.get("manifest_path")
    if not agent_id or not manifest_path:
        raise HTTPException(status_code=400, detail="Job is missing agent_id or manifest_path.")

    completed = data.get("completed_phases", [])
    phase_results = data.get("phase_results", {})

    phase_values = {ph.value for ph in Phase}
    completed_values = [p for p in completed if p in phase_values]

    update_job(job_id, status=JOB_STATUS_RUNNING, error=None)

    return _invoke_provision_starter(
        starter,
        job_id=job_id,
        agent_id=agent_id,
        manifest_path=manifest_path,
        skip_phases=completed_values,
        prior_results=phase_results,
        replace_existing=True,
        success_message="Job resumed (Temporal). Skipping completed phases.",
        failure_verb="resume",
        indeterminate_status=JOB_STATUS_RUNNING,
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
        * On indeterminate start timeout: the reset job is left non-terminal
          and the response still includes ``job_id`` for polling.
    """
    starter = _require_provision_starter()
    try:
        data = _validate_job_for_reprovision(job_id, RESTARTABLE_STATUSES, "restarted")
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=code, detail=str(exc)) from exc

    agent_id = data.get("agent_id")
    manifest_path = data.get("manifest_path")
    if not agent_id or not manifest_path:
        raise HTTPException(status_code=400, detail="Job is missing agent_id or manifest_path.")

    store_reset_job(job_id)

    return _invoke_provision_starter(
        starter,
        job_id=job_id,
        agent_id=agent_id,
        manifest_path=manifest_path,
        skip_phases=None,
        prior_results=None,
        replace_existing=True,
        success_message="Job restarted (Temporal) from scratch.",
        failure_verb="restart",
        indeterminate_status=JOB_STATUS_PENDING,
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
) -> Union[DeprovisionResponse, JSONResponse]:
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
        * If an open pre-lock-patch execution still exists for ``agent_id``,
          the workflow is never started and a
          ``JSONResponse(status_code=409)`` is returned instead.
    """
    _require_safe_agent_id(agent_id)
    runner = _require_deprovision_runner()

    drain_gate_rejection = _reject_if_pre_patch_open(agent_id)
    if drain_gate_rejection is not None:
        return drain_gate_rejection

    try:
        return DeprovisionResponse.model_validate(runner(agent_id, force))
    except ValidationError:
        logger.exception("Invalid deprovision workflow response for agent=%s", agent_id)
        return DeprovisionResponse(
            agent_id=agent_id,
            success=False,
            details={},
            error="Invalid deprovision workflow response",
        )
    except RuntimeError as exc:
        # execute_workflow_sync raises this when the shared Temporal client/loop
        # never becomes available — same "Temporal required" contract as provision.
        if "Temporal client not available" in str(exc):
            raise HTTPException(
                status_code=503,
                detail=f"{_TEMPORAL_REQUIRED_MESSAGE}: {exc}",
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
    updated_at: Optional[str] = None


@app.get(
    "/environments/{agent_id}",
    response_model=AgentStatusResponse,
    summary="Get agent environment status",
    description="Get the current status of a provisioned agent environment.",
)
def get_agent_status(agent_id: str) -> AgentStatusResponse:
    """Get status of a provisioned agent."""
    _require_safe_agent_id(agent_id)
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
            updated_at=a.get("updated_at"),
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
