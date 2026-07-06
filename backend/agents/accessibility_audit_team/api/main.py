"""
FastAPI endpoints for the Digital Accessibility Audit Team.
"""

import asyncio
import logging
import uuid
from typing import Any, Callable, Dict, List, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from job_service_client import (
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    start_stale_job_monitor,
)
from shared_observability import init_otel

from ..audit_execution import (
    CreateAuditRequest,
    execute_audit_job,
    get_job_manager,
    get_orchestrator,
)
from ..models import (
    AccessibilityAuditResult,
    AuditJobResponse,
    AuditStatusResponse,
    BacklogExportResponse,
    FindingsListResponse,
    Severity,
)

init_otel(service_name="accessibility-audit-team", team_key="accessibility_audit")

router = APIRouter()
logger = logging.getLogger(__name__)

_job_manager = get_job_manager()
_stale_monitor_stop = start_stale_job_monitor(
    _job_manager,
    interval_seconds=15.0,
    stale_after_seconds=300.0,
    reason="Job heartbeat stale while pending/running",
)


def mark_all_running_jobs_failed(reason: str) -> None:
    """Mark all running accessibility audit jobs as failed (e.g. on server shutdown)."""
    try:
        _job_manager.mark_stale_active_jobs_failed(stale_after_seconds=0, reason=reason)
    except Exception as e:
        logger.warning("mark_all_running_jobs_failed: %s", e)


def _to_ui_status(status: str) -> str:
    """Map shared manager statuses to frontend-compatible values."""
    if status == "completed":
        return "complete"
    return status


# ---------------------------------------------------------------------------
# Request/Response Models
# ---------------------------------------------------------------------------


class RetestRequest(BaseModel):
    """Request to run retest on specific findings."""

    finding_ids: List[str] = Field(
        default_factory=list,
        description="Finding IDs to retest (empty = all)",
    )


class MonitorBaselineRequest(BaseModel):
    """Request to create a monitoring baseline."""

    env: str = Field(default="prod", description="Environment: stage or prod")
    targets: List[Dict[str, str]] = Field(
        default_factory=list,
        description="Targets: [{url, journey}]",
    )
    checks: List[str] = Field(
        default_factory=lambda: ["axe", "keyboard_flow"],
        description="Checks to run",
    )


class MonitorRunRequest(BaseModel):
    """Request to run monitoring checks."""

    baseline_ref: str = Field(..., description="Baseline reference")
    env: str = Field(default="prod")


class DesignSystemInventoryRequest(BaseModel):
    """Request to build design system component inventory."""

    system_name: str = Field(..., description="Design system name")
    source: str = Field(default="storybook", description="Source: storybook, repo, manual")
    components: List[str] = Field(default_factory=list)


class DesignSystemContractRequest(BaseModel):
    """Request to generate accessibility contract."""

    system_name: str
    component: str
    platform: str = Field(default="web")
    linked_patterns: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Temporal dispatch
# ---------------------------------------------------------------------------


def _get_temporal_dispatcher() -> Optional[Callable[[str, str, dict], str]]:
    """Return the Temporal ``start_*_workflow`` dispatcher when Temporal is enabled.

    Preconditions:
        - None from the caller. Temporal enablement (``TEMPORAL_ADDRESS`` set) is
          checked internally via ``shared_temporal.is_temporal_enabled()``.
    Postconditions:
        - Returns the ``start_accessibility_audit_workflow`` callable when
          ``TEMPORAL_ADDRESS`` is set and the Temporal stack imports cleanly, else
          ``None`` so callers fall back to the in-process background-task path. A
          failed import while Temporal is enabled is logged before returning ``None``.
    """
    try:
        from shared_temporal import is_temporal_enabled
    except ImportError:
        return None
    if not is_temporal_enabled():
        return None
    try:
        from ..temporal.start_workflow import start_accessibility_audit_workflow
    except ImportError:
        logger.warning(
            "Temporal is enabled but the accessibility-audit Temporal stack failed to "
            "import; falling back to in-process execution.",
            exc_info=True,
        )
        return None
    return start_accessibility_audit_workflow


# ---------------------------------------------------------------------------
# Audit Endpoints
# ---------------------------------------------------------------------------


@router.post("/audit/create", response_model=AuditJobResponse)
async def create_audit(
    request: CreateAuditRequest,
    background_tasks: BackgroundTasks,
) -> AuditJobResponse:
    """Create and start a new accessibility audit.

    Dispatch path: when Temporal is enabled (``TEMPORAL_ADDRESS`` set) the audit
    runs as a durable ``AccessibilityAuditWorkflow`` on the
    ``accessibility_audit-queue`` task queue and the response's ``workflow_id`` is
    populated for Temporal-UI correlation. Otherwise it runs in-process via a
    FastAPI background task and ``workflow_id`` is ``None`` (never an empty
    string). Either way the job row is created ``pending`` up front and its
    status transitions (``running`` -> terminal) are owned by whichever path
    executes it, so clients poll ``GET /audit/status/{job_id}`` regardless of
    whether ``workflow_id`` is present.

    Postconditions:
        - A ``pending`` job row exists; the response returns its ``job_id`` /
          ``audit_id`` (and ``workflow_id`` on the Temporal path) for polling.
    """
    job_id = f"job_{uuid.uuid4().hex[:8]}"
    audit_id = f"audit_{uuid.uuid4().hex[:8]}"
    payload = request.model_dump()

    _job_manager.create_job(
        job_id,
        job_type="accessibility_audit_create",
        status=JOB_STATUS_PENDING,
        audit_id=audit_id,
        current_phase="intake",
        progress=0,
        completed_phases=[],
        findings_count=0,
        result=None,
        error=None,
        request_payload=payload,
    )

    # None => the in-process (non-Temporal) path ran; set to the workflow id on a
    # successful Temporal dispatch. It is never an empty string.
    workflow_id: Optional[str] = None
    dispatch = _get_temporal_dispatcher()
    if dispatch is not None:
        try:
            # The dispatcher is synchronous and blocking: it polls for the worker's
            # Temporal client to connect, then makes a blocking round-trip to start
            # the workflow. Calling it directly on the async event loop would freeze
            # every other in-flight request, so offload it to a worker thread.
            workflow_id = await asyncio.to_thread(dispatch, job_id, audit_id, payload)
        except Exception as e:
            # Fail fast rather than re-running in-process: the workflow may have
            # been accepted server-side, so an in-process fallback could execute
            # the same audit twice. Mark the job failed and surface a 500. The
            # exception type/message (e.g. connection refused vs. client-not-ready
            # timeout) is included so operators can tell the failures apart.
            logger.warning(
                "Temporal dispatch failed for job %s: %s: %s",
                job_id,
                type(e).__name__,
                e,
                exc_info=True,
            )
            _job_manager.update_job(
                job_id, status=JOB_STATUS_FAILED, error=f"Temporal dispatch failed: {e}"
            )
            raise HTTPException(
                status_code=500, detail="Failed to dispatch audit to Temporal"
            ) from e
        # Record the workflow id for correlation only; the worker activity owns
        # all status transitions (pending -> running -> terminal), so the API
        # must not also write status here or it can race the activity. The
        # workflow is already accepted server-side at this point, so a failure
        # to persist the correlation id is logged, not raised — the job is not
        # failed and the request still succeeds.
        try:
            _job_manager.update_job(job_id, workflow_id=workflow_id)
        except Exception:
            logger.warning("Failed to record workflow_id for job %s", job_id, exc_info=True)
        message = "Audit queued (Temporal). Poll /audit/status/{job_id} for progress."
    else:
        background_tasks.add_task(execute_audit_job, job_id, audit_id, request)
        message = "Audit queued. Poll /audit/status/{job_id} for progress."

    return AuditJobResponse(
        job_id=job_id,
        audit_id=audit_id,
        status=JOB_STATUS_PENDING,
        message=message,
        workflow_id=workflow_id,
    )


@router.get("/audit/status/{job_id}", response_model=AuditStatusResponse)
async def get_audit_status(job_id: str) -> AuditStatusResponse:
    """
    Get the status of an audit job.
    """
    job = _job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    result = job.get("result")
    if isinstance(result, dict):
        try:
            result = AccessibilityAuditResult.model_validate(result)
        except Exception:
            result = None

    return AuditStatusResponse(
        job_id=job_id,
        audit_id=job["audit_id"],
        status=_to_ui_status(job.get("status", JOB_STATUS_PENDING)),
        current_phase=result.current_phase.value if result else None,
        progress=job.get("progress", 0),
        completed_phases=[p.value for p in result.completed_phases] if result else [],
        findings_count=result.total_findings if result else 0,
        error=job.get("error"),
        result=result,
    )


@router.get("/audit/{audit_id}/findings", response_model=FindingsListResponse)
async def get_audit_findings(
    audit_id: str,
    severity: Optional[str] = None,
    state: Optional[str] = None,
    offset: int = 0,
    limit: int = 50,
) -> FindingsListResponse:
    """
    Get findings for an audit with optional filters and pagination.
    """
    orchestrator = get_orchestrator()

    severity_filter = Severity(severity) if severity else None
    findings = orchestrator.get_findings(audit_id, severity_filter, state)

    if not findings:
        # Check if audit exists
        status = orchestrator.get_audit_status(audit_id)
        if status.get("status") == "not_found":
            raise HTTPException(status_code=404, detail=f"Audit {audit_id} not found")

    total = len(findings)

    # Count by severity (over full result set, before pagination)
    by_severity = {}
    for sev in Severity:
        count = sum(1 for f in findings if f.severity == sev)
        if count > 0:
            by_severity[sev.value] = count

    # Count by issue type
    by_issue_type = {}
    for f in findings:
        issue_type = f.issue_type.value
        by_issue_type[issue_type] = by_issue_type.get(issue_type, 0) + 1

    # Apply pagination
    paginated = findings[offset : offset + limit]

    return FindingsListResponse(
        audit_id=audit_id,
        total=total,
        findings=paginated,
        by_severity=by_severity,
        by_issue_type=by_issue_type,
        offset=offset,
        limit=limit,
        has_more=(offset + limit) < total,
    )


@router.get("/audit/{audit_id}/report")
async def get_audit_report(audit_id: str) -> Dict[str, Any]:
    """
    Get the final report for a completed audit.
    """
    orchestrator = get_orchestrator()
    status = orchestrator.get_audit_status(audit_id)

    if status.get("status") == "not_found":
        raise HTTPException(status_code=404, detail=f"Audit {audit_id} not found")

    if status.get("status") != "complete":
        raise HTTPException(
            status_code=400,
            detail=f"Audit {audit_id} is not complete yet",
        )

    orchestrator.get_findings(audit_id)
    patterns = orchestrator.get_patterns(audit_id)

    # Build report
    return {
        "audit_id": audit_id,
        "summary": status.get("summary"),
        "findings_count": status.get("findings_count"),
        "by_severity": {
            "critical": status.get("critical_count"),
            "high": status.get("high_count"),
            "medium": status.get("medium_count"),
            "low": status.get("low_count"),
        },
        "patterns_count": status.get("patterns_count"),
        "patterns": [p.model_dump() for p in patterns],
        "completed_phases": status.get("completed_phases"),
    }


@router.post("/audit/{audit_id}/retest", response_model=AuditJobResponse)
async def retest_findings(
    audit_id: str,
    request: RetestRequest,
    background_tasks: BackgroundTasks,
) -> AuditJobResponse:
    """
    Run retest on specific findings or all findings.
    """
    orchestrator = get_orchestrator()
    status = orchestrator.get_audit_status(audit_id)

    if status.get("status") == "not_found":
        raise HTTPException(status_code=404, detail=f"Audit {audit_id} not found")

    job_id = f"retest_{uuid.uuid4().hex[:8]}"

    _job_manager.create_job(
        job_id,
        job_type="accessibility_audit_retest",
        status=JOB_STATUS_PENDING,
        audit_id=audit_id,
        current_phase="retest",
        progress=0,
        completed_phases=[],
        findings_count=0,
        result=None,
        error=None,
        request_payload=request.model_dump(),
    )

    async def run_retest_task():
        try:
            _job_manager.update_job(job_id, status=JOB_STATUS_RUNNING, progress=30)
            result = await orchestrator.run_retest(audit_id, request.finding_ids)
            _job_manager.update_job(
                job_id,
                status="completed" if result.success else JOB_STATUS_FAILED,
                progress=100,
                result=result.model_dump(),
                error=result.failure_reason if not result.success else None,
            )
        except Exception as e:
            _job_manager.update_job(job_id, status=JOB_STATUS_FAILED, error=str(e))

    background_tasks.add_task(run_retest_task)

    return AuditJobResponse(
        job_id=job_id,
        audit_id=audit_id,
        status="running",
        message="Retest started.",
    )


@router.post("/audit/{audit_id}/export", response_model=BacklogExportResponse)
async def export_backlog(
    audit_id: str,
    export_format: str = "json",
    include_evidence: bool = True,
) -> BacklogExportResponse:
    """
    Export the findings backlog in the specified format.
    """
    from ..phases.report_packaging import export_final_report

    orchestrator = get_orchestrator()
    status = orchestrator.get_audit_status(audit_id)

    if status.get("status") == "not_found":
        raise HTTPException(status_code=404, detail=f"Audit {audit_id} not found")

    findings = orchestrator.get_findings(audit_id)
    patterns = orchestrator.get_patterns(audit_id)

    result = await export_final_report(
        audit_id=audit_id,
        findings=findings,
        patterns=patterns,
        export_format=export_format,
        include_evidence=include_evidence,
    )

    return BacklogExportResponse(
        audit_id=audit_id,
        format=export_format,
        artifact_ref=result["artifact_ref"],
        counts=result["counts"],
    )


# ---------------------------------------------------------------------------
# Case Study Endpoints
# ---------------------------------------------------------------------------


class CaseStudyRequest(BaseModel):
    """Request body for case study generation."""

    template_key: Literal[
        "comprehensive",
        "basic_audit",
        "premium_assessment",
        "enterprise_analysis",
        "executive_summary",
        "video_script",
    ] = Field(
        default="comprehensive",
        description="Template variant",
    )
    industry: Optional[Literal["ecommerce", "saas", "healthcare"]] = Field(
        default=None,
        description="Industry-specific template override",
    )
    client_context: Dict[str, Any] = Field(
        default_factory=dict,
        description="Client-provided data for template placeholders",
    )


@router.post("/audit/{audit_id}/case-study")
async def generate_audit_case_study(
    audit_id: str,
    request: CaseStudyRequest,
) -> Dict[str, Any]:
    """
    Generate a case study document from audit findings using the case study templates.
    """
    from ..tools.audit.generate_case_study import (
        GenerateCaseStudyInput,
        generate_case_study,
    )

    orchestrator = get_orchestrator()
    status = orchestrator.get_audit_status(audit_id)

    if status.get("status") == "not_found":
        raise HTTPException(status_code=404, detail=f"Audit {audit_id} not found")

    findings = orchestrator.get_findings(audit_id)

    input_data = GenerateCaseStudyInput(
        audit_id=audit_id,
        findings=findings,
        client_context=request.client_context,
        template_key=request.template_key,
        industry=request.industry,
    )

    result = await generate_case_study(input_data)

    return {
        "audit_id": audit_id,
        "artifact_ref": result.artifact_ref,
        "template_used": result.template_used,
        "template_key": result.template_key,
        "industry": result.industry,
        "sections": result.sections,
        "metrics": result.metrics,
    }


@router.get("/case-study-templates")
async def list_case_study_templates() -> Dict[str, Any]:
    """
    List all available case study templates and their descriptions.
    """
    from ..tools.audit.generate_case_study import list_available_templates

    return await list_available_templates()


# ---------------------------------------------------------------------------
# Monitoring Endpoints (ARM Add-on)
# ---------------------------------------------------------------------------


@router.post("/monitor/baseline")
async def create_monitoring_baseline(
    request: MonitorBaselineRequest,
) -> Dict[str, Any]:
    """
    Create a monitoring baseline for regression detection.
    """
    from ..addons.monitoring_agent import AccessibilityMonitoringAgent

    arm = AccessibilityMonitoringAgent()
    baseline = await arm.create_baseline(
        audit_id="",
        env=request.env,
        targets=request.targets,
        checks=request.checks,
    )

    return {
        "baseline_ref": baseline.baseline_ref,
        "env": baseline.env,
        "targets_count": len(request.targets),
        "checks": request.checks,
        "status": "created",
    }


@router.post("/monitor/run")
async def run_monitoring_checks(
    request: MonitorRunRequest,
) -> Dict[str, Any]:
    """
    Run monitoring checks against a baseline.
    """
    from ..addons.monitoring_agent import AccessibilityMonitoringAgent

    arm = AccessibilityMonitoringAgent()
    run_result = await arm.run_checks(
        baseline_ref=request.baseline_ref,
        env=request.env,
    )

    return {
        "run_id": run_result.run_id,
        "baseline_ref": run_result.baseline_ref,
        "env": run_result.env,
        "status": "complete",
        "new_issues": len(run_result.findings),
        "resolved_issues": 0,
        "unchanged_issues": 0,
    }


@router.get("/monitor/diff/{run_id}")
async def get_monitoring_diff(run_id: str) -> Dict[str, Any]:
    """
    Get the diff between a monitoring run and its baseline.
    """
    from ..addons.monitoring_agent import AccessibilityMonitoringAgent

    arm = AccessibilityMonitoringAgent()
    diff = await arm.diff_against_baseline(
        monitor_run_id=run_id,
        baseline_ref="",
    )

    return {
        "run_id": diff.run_id,
        "new_issues": diff.new_issues,
        "resolved_issues": diff.resolved_issues,
        "unchanged_issues": diff.unchanged_issues,
        "alerts_triggered": diff.alerts_triggered,
    }


# ---------------------------------------------------------------------------
# Design System Endpoints (ADSE Add-on)
# ---------------------------------------------------------------------------


@router.post("/designsystem/inventory")
async def build_component_inventory(
    request: DesignSystemInventoryRequest,
) -> Dict[str, Any]:
    """
    Build an inventory of design system components.
    """
    from ..addons.design_system_agent import AccessibleDesignSystemAgent

    adse = AccessibleDesignSystemAgent()
    inventory = await adse.build_inventory(
        system_name=request.system_name,
        source=request.source,
        components=request.components,
    )

    return {
        "inventory_ref": inventory.inventory_ref,
        "system_name": inventory.system_name,
        "source": inventory.source,
        "components_count": len(inventory.components),
        "status": "created",
    }


@router.post("/designsystem/contract")
async def generate_a11y_contract(
    request: DesignSystemContractRequest,
) -> Dict[str, Any]:
    """
    Generate an accessibility contract for a component.
    """
    from ..addons.design_system_agent import AccessibleDesignSystemAgent

    adse = AccessibleDesignSystemAgent()
    contract = await adse.generate_contract(
        system_name=request.system_name,
        component=request.component,
        platform=request.platform,
        linked_patterns=request.linked_patterns,
    )

    return {
        "contract_ref": contract.contract_ref,
        "system_name": contract.system_name,
        "component": contract.component,
        "platform": contract.platform,
        "status": "created",
        "requirements": contract.requirements,
    }


# ---------------------------------------------------------------------------
# Health Check
# ---------------------------------------------------------------------------


@router.get("/health")
async def health_check() -> Dict[str, str]:
    """
    Health check endpoint.
    """
    return {
        "status": "healthy",
        "service": "accessibility_audit_team",
    }
