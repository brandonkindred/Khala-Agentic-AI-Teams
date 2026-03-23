"""
FastAPI endpoints for the Digital Accessibility Audit Team.
"""

import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from job_service_client import (
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    JobServiceClient,
    start_stale_job_monitor,
)

from ..models import (
    AccessibilityAuditResult,
    AuditJobResponse,
    AuditRequest,
    AuditStatusResponse,
    BacklogExportResponse,
    FindingsListResponse,
    MobileAppTarget,
    Severity,
    WCAGLevel,
)
from ..orchestrator import AccessibilityAuditOrchestrator

router = APIRouter()
logger = logging.getLogger(__name__)

_job_manager = JobServiceClient(team="accessibility_audit_team")
_stale_monitor_stop = start_stale_job_monitor(
    _job_manager,
    interval_seconds=15.0,
    stale_after_seconds=300.0,
    reason="Job heartbeat stale while pending/running",
)
_orchestrator: Optional[AccessibilityAuditOrchestrator] = None


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


def get_orchestrator() -> AccessibilityAuditOrchestrator:
    """Get or create the orchestrator singleton."""
    global _orchestrator
    if _orchestrator is None:
        from llm_service import get_client

        _orchestrator = AccessibilityAuditOrchestrator(llm_client=get_client("accessibility_audit"))
    return _orchestrator


# ---------------------------------------------------------------------------
# Request/Response Models
# ---------------------------------------------------------------------------


class CreateAuditRequest(BaseModel):
    """Request to create a new accessibility audit."""

    name: str = Field(default="", description="Human-readable audit name")
    web_urls: List[str] = Field(default_factory=list, description="Web URLs to audit")
    mobile_apps: List[Dict[str, str]] = Field(
        default_factory=list,
        description="Mobile apps: [{platform, name, version, build}]",
    )
    critical_journeys: List[str] = Field(default_factory=list, description="Critical user journeys")
    timebox_hours: Optional[int] = Field(default=None, description="Maximum hours for the audit")
    auth_required: bool = Field(default=False)
    max_pages: Optional[int] = Field(default=None)
    sampling_strategy: str = Field(default="journey_based")
    wcag_levels: List[str] = Field(default_factory=lambda: ["A", "AA"])
    tech_stack: Dict[str, str] = Field(default_factory=lambda: {"web": "other", "mobile": "other"})


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
# Audit Endpoints
# ---------------------------------------------------------------------------


@router.post("/audit/create", response_model=AuditJobResponse)
async def create_audit(
    request: CreateAuditRequest,
    background_tasks: BackgroundTasks,
) -> AuditJobResponse:
    """
    Create and start a new accessibility audit.

    Returns a job ID that can be used to poll for status.
    """
    job_id = f"job_{uuid.uuid4().hex[:8]}"
    audit_id = f"audit_{uuid.uuid4().hex[:8]}"

    # Convert mobile apps
    mobile_app_targets = [
        MobileAppTarget(
            platform=app.get("platform", "ios"),
            name=app.get("name", ""),
            version=app.get("version", ""),
            build=app.get("build", ""),
        )
        for app in request.mobile_apps
    ]

    # Convert WCAG levels
    wcag_levels = [WCAGLevel(level) for level in request.wcag_levels if level in ["A", "AA", "AAA"]]

    # Create audit request
    audit_request = AuditRequest(
        audit_id=audit_id,
        name=request.name,
        web_urls=request.web_urls,
        mobile_apps=mobile_app_targets,
        critical_journeys=request.critical_journeys,
        timebox_hours=request.timebox_hours,
        auth_required=request.auth_required,
        max_pages=request.max_pages,
        sampling_strategy=request.sampling_strategy,
        wcag_levels=wcag_levels or [WCAGLevel.A, WCAGLevel.AA],
    )

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
        request_payload=request.model_dump(),
    )

    # Run audit in background
    async def run_audit_task():
        try:
            _job_manager.update_job(
                job_id, status=JOB_STATUS_RUNNING, current_phase="discovery", progress=20
            )
            orchestrator = get_orchestrator()
            result = await orchestrator.run_audit(audit_request, request.tech_stack)
            _job_manager.update_job(
                job_id,
                status="completed" if result.success else JOB_STATUS_FAILED,
                progress=100,
                current_phase=result.current_phase.value if result else "report_packaging",
                completed_phases=[p.value for p in result.completed_phases] if result else [],
                findings_count=result.total_findings if result else 0,
                result=result.model_dump() if result else None,
                error=result.failure_reason if result and not result.success else None,
            )
            if not result.success:
                _job_manager.update_job(
                    job_id, status=JOB_STATUS_FAILED, error=result.failure_reason
                )
        except Exception as e:
            _job_manager.update_job(job_id, status=JOB_STATUS_FAILED, error=str(e))

    background_tasks.add_task(run_audit_task)

    return AuditJobResponse(
        job_id=job_id,
        audit_id=audit_id,
        status="running",
        message="Audit started. Poll /audit/status/{job_id} for progress.",
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
) -> FindingsListResponse:
    """
    Get findings for an audit with optional filters.
    """
    orchestrator = get_orchestrator()

    severity_filter = Severity(severity) if severity else None
    findings = orchestrator.get_findings(audit_id, severity_filter, state)

    if not findings:
        # Check if audit exists
        status = orchestrator.get_audit_status(audit_id)
        if status.get("status") == "not_found":
            raise HTTPException(status_code=404, detail=f"Audit {audit_id} not found")

    # Count by severity
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

    return FindingsListResponse(
        audit_id=audit_id,
        total=len(findings),
        findings=findings,
        by_severity=by_severity,
        by_issue_type=by_issue_type,
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
    format: str = "json",
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
        format=format,
        include_evidence=include_evidence,
    )

    return BacklogExportResponse(
        audit_id=audit_id,
        format=format,
        artifact_ref=result["artifact_ref"],
        counts=result["counts"],
    )


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
    baseline_ref = f"baseline_{uuid.uuid4().hex[:8]}"

    # Store baseline (would be persistent in production)
    return {
        "baseline_ref": baseline_ref,
        "env": request.env,
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
    run_id = f"monitor_run_{uuid.uuid4().hex[:8]}"

    # Would run actual checks in production
    return {
        "run_id": run_id,
        "baseline_ref": request.baseline_ref,
        "env": request.env,
        "status": "complete",
        "new_issues": 0,
        "resolved_issues": 0,
        "unchanged_issues": 0,
    }


@router.get("/monitor/diff/{run_id}")
async def get_monitoring_diff(run_id: str) -> Dict[str, Any]:
    """
    Get the diff between a monitoring run and its baseline.
    """
    return {
        "run_id": run_id,
        "new_issues": [],
        "resolved_issues": [],
        "unchanged_issues": [],
        "alerts_triggered": 0,
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
    inventory_ref = f"inventory_{uuid.uuid4().hex[:8]}"

    return {
        "inventory_ref": inventory_ref,
        "system_name": request.system_name,
        "source": request.source,
        "components_count": len(request.components),
        "status": "created",
    }


@router.post("/designsystem/contract")
async def generate_a11y_contract(
    request: DesignSystemContractRequest,
) -> Dict[str, Any]:
    """
    Generate an accessibility contract for a component.
    """
    contract_ref = f"contract_{uuid.uuid4().hex[:8]}"

    return {
        "contract_ref": contract_ref,
        "system_name": request.system_name,
        "component": request.component,
        "platform": request.platform,
        "status": "created",
        "requirements": {
            "keyboard_accessible": True,
            "focus_visible": True,
            "proper_labeling": True,
            "sufficient_contrast": True,
        },
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
