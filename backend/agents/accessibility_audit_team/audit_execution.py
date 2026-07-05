"""Side-effect-free audit execution core.

Shared by the FastAPI layer (``api.main``) and the Temporal activity
(``temporal``). Importing this module must NOT start threads, init telemetry,
or open network clients — the Temporal worker imports it to run an audit and
must not inherit the API process's stale-job monitor / OTel setup. Long-lived
resources (job-service client, orchestrator) are created lazily on first use.
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from job_service_client import JOB_STATUS_FAILED, JOB_STATUS_RUNNING, JobServiceClient

from .models import AuditRequest, MobileAppTarget, WCAGLevel
from .orchestrator import AccessibilityAuditOrchestrator

logger = logging.getLogger(__name__)

# Both singletons are reachable from two threads: the API event-loop thread (the
# in-process background task) and the Temporal worker's own loop thread. Guard the
# lazy check-then-set with a lock so a concurrent first-call can't build two
# orchestrators / clients.
_singleton_lock = threading.Lock()
_job_manager: Optional[JobServiceClient] = None
_orchestrator: Optional[AccessibilityAuditOrchestrator] = None


def get_job_manager() -> JobServiceClient:
    """Return the process-wide accessibility-audit ``JobServiceClient`` singleton.

    Preconditions:
        - ``JOB_SERVICE_URL`` is configured (enforced by ``JobServiceClient``).
    Postconditions:
        - Returns the same instance on every call within a process (thread-safe).
    """
    global _job_manager
    if _job_manager is None:
        with _singleton_lock:
            if _job_manager is None:
                _job_manager = JobServiceClient(team="accessibility_audit_team")
    return _job_manager


def get_orchestrator() -> AccessibilityAuditOrchestrator:
    """Return the process-wide orchestrator singleton (LLM-backed).

    Preconditions:
        - An LLM provider is resolvable via ``get_strands_model``.
    Postconditions:
        - Returns the same instance on every call within a process (thread-safe).
    """
    global _orchestrator
    if _orchestrator is None:
        with _singleton_lock:
            if _orchestrator is None:
                from strands import Agent

                from llm_service import get_strands_model

                _orchestrator = AccessibilityAuditOrchestrator(
                    llm_client=Agent(model=get_strands_model("accessibility_audit")),
                )
    return _orchestrator


class CreateAuditRequest(BaseModel):
    """Public request body for creating an accessibility audit.

    Invariants:
        - Every entry in ``web_urls`` is an ``http(s)`` URL (enforced by
          ``validate_urls``); all other fields fall back to sensible defaults.
    """

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

    @field_validator("web_urls")
    @classmethod
    def validate_urls(cls, v: List[str]) -> List[str]:
        """Reject non-``http(s)`` URLs.

        Preconditions:
            - ``v`` is the list of candidate web URLs.
        Postconditions:
            - Returns ``v`` unchanged when every entry starts with ``http://`` or
              ``https://``; raises ``ValueError`` on the first entry that does not.
        """
        for url in v:
            if not url.startswith(("http://", "https://")):
                raise ValueError(f"Invalid URL (must start with http:// or https://): {url}")
        return v


def build_audit_request(request: CreateAuditRequest, audit_id: str) -> AuditRequest:
    """Convert the public ``CreateAuditRequest`` into the internal ``AuditRequest``.

    Preconditions:
        - ``audit_id`` is a non-empty audit identifier.
        - ``request.wcag_levels`` entries are WCAG level strings (invalid ones are dropped).
    Postconditions:
        - Returns an ``AuditRequest`` whose ``audit_id`` equals ``audit_id`` and whose
          ``wcag_levels`` defaults to ``[A, AA]`` when none of the inputs are valid.
    """
    if not audit_id:
        raise ValueError("audit_id must be a non-empty audit identifier")

    mobile_app_targets = [
        MobileAppTarget(
            platform=app.get("platform", "ios"),
            name=app.get("name", ""),
            version=app.get("version", ""),
            build=app.get("build", ""),
        )
        for app in request.mobile_apps
    ]

    wcag_levels = [WCAGLevel(level) for level in request.wcag_levels if level in ["A", "AA", "AAA"]]

    return AuditRequest(
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


async def run_audit_job(job_id: str, audit_id: str, request: CreateAuditRequest) -> None:
    """Run a full audit and persist its lifecycle to the shared job store.

    The shared execution core. It records ``running`` then a terminal state
    (``completed``, or ``failed`` when the audit *ran* but its target was
    unauditable). A genuine *infrastructure* failure (LLM/orchestrator crash,
    job-service error) is NOT swallowed: it propagates so the Temporal activity
    can fail and let Temporal's retry policy recover. Callers with no retry
    mechanism (the FastAPI background task) wrap this in ``execute_audit_job``.

    Preconditions:
        - ``job_id``/``audit_id`` are non-empty and a job row already exists for ``job_id``.
    Postconditions:
        - On success/logical-failure the job ends in ``completed``/``failed``.
        - On an infrastructure exception the exception propagates to the caller
          (the job's last persisted state is ``running``).
    """
    manager = get_job_manager()
    manager.update_job(job_id, status=JOB_STATUS_RUNNING, current_phase="discovery", progress=20)
    audit_request = build_audit_request(request, audit_id)
    result = await get_orchestrator().run_audit(audit_request, request.tech_stack)
    manager.update_job(
        job_id,
        status="completed" if result.success else JOB_STATUS_FAILED,
        progress=100,
        current_phase=result.current_phase.value,
        completed_phases=[p.value for p in result.completed_phases],
        findings_count=result.total_findings,
        result=result.model_dump(),
        error=None if result.success else result.failure_reason,
    )


async def execute_audit_job(job_id: str, audit_id: str, request: CreateAuditRequest) -> None:
    """FastAPI-background-task wrapper around :func:`run_audit_job`.

    The in-process path has no external retry mechanism, so an infrastructure
    exception is captured onto the job record (status ``failed``) rather than
    propagated. The Temporal activity calls :func:`run_audit_job` directly so
    such exceptions can drive Temporal's retry policy.

    Preconditions:
        - ``job_id``/``audit_id`` are non-empty and a job row already exists for ``job_id``.
    Postconditions:
        - The job ends in ``completed`` or ``failed``; no exception propagates.
    """
    try:
        await run_audit_job(job_id, audit_id, request)
    except Exception as e:
        get_job_manager().update_job(job_id, status=JOB_STATUS_FAILED, error=str(e))
