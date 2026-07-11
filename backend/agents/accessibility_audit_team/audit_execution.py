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

from job_service_client import (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_RUNNING,
    JobServiceClient,
)

from .agents.base import MessageBus
from .models import (
    AccessibilityAuditResult,
    AuditRequest,
    MobileAppTarget,
    Phase,
    ReportPackagingResult,
    Severity,
    WCAGLevel,
)
from .orchestrator import AccessibilityAuditOrchestrator
from .phases import (
    run_discovery_phase,
    run_intake_phase,
    run_report_packaging_phase,
    run_verification_phase,
)

logger = logging.getLogger(__name__)

#: Fallback tech stack applied when a request/verification step supplies none. Kept
#: in one place so the API default (``CreateAuditRequest.tech_stack``) and the
#: verification step's fallback can't drift apart.
DEFAULT_TECH_STACK: Dict[str, str] = {"web": "other", "mobile": "other"}

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
    timebox_hours: Optional[int] = Field(
        default=None, ge=1, description="Maximum hours for the audit (>= 1 when set)"
    )
    auth_required: bool = Field(default=False)
    max_pages: Optional[int] = Field(default=None)
    sampling_strategy: str = Field(default="journey_based")
    wcag_levels: List[str] = Field(default_factory=lambda: ["A", "AA"])
    tech_stack: Dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_TECH_STACK))

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
        - If the job is already in a terminal state (``completed``/``failed``), the
          audit is NOT re-run — this makes a Temporal retry that fires *after* the
          terminal ``update_job`` below already landed (e.g. the retry was triggered
          by a job-store network blip on that same call) a no-op instead of
          re-executing the full (up to 2h) audit.
        - Otherwise, on success/logical-failure the job ends in ``completed``/``failed``.
        - On an infrastructure exception the exception propagates to the caller
          (the job's last persisted state is ``running``).
    """
    if not job_id:
        raise ValueError("job_id must be a non-empty job id")
    if not audit_id:
        raise ValueError("audit_id must be a non-empty audit id")
    manager = get_job_manager()
    existing = manager.get_job(job_id)
    if existing is not None and existing.get("status") in (JOB_STATUS_COMPLETED, JOB_STATUS_FAILED):
        return
    manager.update_job(job_id, status=JOB_STATUS_RUNNING, current_phase="discovery", progress=20)
    audit_request = build_audit_request(request, audit_id)
    result = await get_orchestrator().run_audit(audit_request, request.tech_stack)
    manager.update_job(
        job_id,
        status=JOB_STATUS_COMPLETED if result.success else JOB_STATUS_FAILED,
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
        logger.exception("Audit job %s failed", job_id)
        get_job_manager().update_job(job_id, status=JOB_STATUS_FAILED, error=str(e))


# ---------------------------------------------------------------------------
# Per-phase step helpers
#
# These are the single source of truth for one phase's work + partial-state
# persistence, shared by BOTH thread mode (the orchestrator) and the Temporal
# per-phase activities (``temporal.activities``). They deliberately do NOT touch
# the job store — each caller owns its own job lifecycle (thread mode writes one
# running→terminal transition in ``run_audit_job``; each activity writes its own
# per-phase progress) — and they raise on infrastructure failure so a Temporal
# activity can surface it to the retry policy instead of masking it.
#
# State crosses the phase boundary via the artifact store (``audit_state_{id}``),
# not by threading large findings lists through Temporal payloads: each step
# loads the accumulated ``AccessibilityAuditResult``, runs its phase, and persists
# the updated result. The load happens inside these helpers (activity code — I/O
# is allowed), never inside a deterministic workflow body.
# ---------------------------------------------------------------------------


def _build_llm_client() -> object:
    """Build a fresh Strands ``Agent`` LLM client for one phase's agents.

    A per-call client (rather than the shared ``get_orchestrator().llm_client``)
    keeps a phase step process-agnostic and free of the singleton orchestrator's
    shared agents/message-bus, so it runs identically in the API process (thread
    mode) and a Temporal worker process.

    Preconditions:
        - An LLM provider is resolvable via ``get_strands_model``.
    Postconditions:
        - Returns a new ``strands.Agent`` bound to the accessibility-audit model.
    """
    from strands import Agent

    from llm_service import get_strands_model

    return Agent(model=get_strands_model("accessibility_audit"))


async def persist_audit_state(result: AccessibilityAuditResult) -> None:
    """Persist an audit's accumulated state to the artifact store for crash recovery.

    Shared by the orchestrator (thread mode) and the per-phase steps so both write
    state the same way, keyed by ``audit_state_{result.audit_id}``.

    Preconditions:
        - ``result.audit_id`` is the non-empty API-supplied audit id (the stable
          store key threaded by the workflow — never the plan's derived id).
    Postconditions:
        - The JSON dump of ``result`` is stored under ``audit_state_{audit_id}``.
          A store failure is logged and swallowed (persistence is best-effort
          crash-recovery, not the durable job record).
    """
    try:
        from .artifact_store import (
            ArtifactMetadata,
            ArtifactType,
            RetentionPolicy,
            get_artifact_store,
        )

        store = get_artifact_store()
        ref = f"audit_state_{result.audit_id}"
        content = result.model_dump_json().encode()
        metadata = ArtifactMetadata(
            artifact_ref=ref,
            artifact_type=ArtifactType.AUDIT_STATE,
            audit_id=result.audit_id,
            mime_type="application/json",
            retention_policy=RetentionPolicy.STANDARD,
        )
        await store.backend.store(ref, content, metadata)
    except Exception as e:
        logger.warning("Failed to persist audit state: %s", e)


async def load_audit_state(audit_id: str) -> Optional[AccessibilityAuditResult]:
    """Load an audit's accumulated state from the artifact store.

    Preconditions:
        - ``audit_id`` is the API-supplied audit id used as the persistence key.
    Postconditions:
        - Returns the persisted ``AccessibilityAuditResult`` or ``None`` when no
          state exists (or it could not be loaded).
    """
    try:
        from .artifact_store import get_artifact_store

        store = get_artifact_store()
        ref = f"audit_state_{audit_id}"
        content = await store.retrieve(ref)
        if content:
            return AccessibilityAuditResult.model_validate_json(content)
    except Exception as e:
        logger.warning("Failed to load audit state: %s", e)
    return None


def finalize_audit_result(
    result: AccessibilityAuditResult, report_result: ReportPackagingResult
) -> AccessibilityAuditResult:
    """Fold the report-packaging output into ``result`` and mark the audit successful.

    The single copy of the audit's finalize logic (severity counts + summary),
    shared by the orchestrator's thread-mode path and the Temporal finalize step.

    Preconditions:
        - ``report_result.success`` is True (the caller short-circuits otherwise).
    Postconditions:
        - ``result`` carries the report result, has ``REPORT_PACKAGING`` recorded
          exactly once in ``completed_phases``, ``success=True``, the final
          findings/patterns/coverage matrix, the four severity counts, and a
          human-readable ``summary``. Returns the same (mutated) ``result``.
    """
    result.report_packaging_result = report_result
    if Phase.REPORT_PACKAGING not in result.completed_phases:
        result.completed_phases.append(Phase.REPORT_PACKAGING)

    result.success = True
    result.final_findings = report_result.final_backlog
    result.final_patterns = report_result.patterns
    result.coverage_matrix = report_result.coverage_matrix

    # Count all severities in a single pass rather than four separate scans.
    counts: Dict[Severity, int] = {sev: 0 for sev in Severity}
    for finding in result.final_findings:
        if finding.severity in counts:
            counts[finding.severity] += 1
    result.total_findings = len(result.final_findings)
    result.critical_count = counts[Severity.CRITICAL]
    result.high_count = counts[Severity.HIGH]
    result.medium_count = counts[Severity.MEDIUM]
    result.low_count = counts[Severity.LOW]

    result.summary = (
        f"Audit complete. {result.total_findings} findings "
        f"({result.critical_count} critical, {result.high_count} high, "
        f"{result.medium_count} medium, {result.low_count} low). "
        f"{len(result.final_patterns)} patterns identified."
    )
    return result


async def run_intake_step(
    job_id: str, audit_id: str, request: CreateAuditRequest
) -> AccessibilityAuditResult:
    """Run the intake phase and persist the seeded audit state.

    Preconditions:
        - ``audit_id`` is the non-empty API-supplied audit id (also the store key).
    Postconditions:
        - Returns a fresh ``AccessibilityAuditResult`` keyed by ``audit_id`` with
          the intake result attached (``INTAKE`` recorded) on success, or with
          ``success=False`` and ``failure_reason`` set when intake fails. Either
          way the state is persisted. An infrastructure failure inside the phase
          propagates.
    """
    logger.info("Intake step for job %s audit %s", job_id, audit_id)
    audit_request = build_audit_request(request, audit_id)
    result = AccessibilityAuditResult(audit_id=audit_id, current_phase=Phase.INTAKE)

    intake_result = await run_intake_phase(
        audit_request=audit_request,
        llm_client=_build_llm_client(),
        message_bus=MessageBus(),
    )

    if not intake_result.success:
        result.success = False
        result.failure_reason = intake_result.error or "Intake failed"
        await persist_audit_state(result)
        return result

    # Keep the API-supplied ``audit_id`` as the canonical store key. In practice the
    # plan carries the same id (``build_audit_request`` sets it), so this is also the
    # id downstream phases see — but we never overwrite it from the plan so the
    # workflow's threaded key can always reload this state.
    result.intake_result = intake_result
    result.completed_phases.append(Phase.INTAKE)
    await persist_audit_state(result)
    return result


async def run_discovery_step(job_id: str, audit_id: str) -> AccessibilityAuditResult:
    """Load prior state, run the discovery phase, and persist the updated state.

    Preconditions:
        - Intake has already persisted state under ``audit_id`` with an audit plan.
    Postconditions:
        - Returns the audit result with the discovery result attached (``DISCOVERY``
          recorded) on success, or ``success=False`` on a logical discovery failure.
        - Raises ``RuntimeError`` if the persisted intake state / audit plan is
          missing (an infrastructure/plumbing failure, surfaced for Temporal retry).
    """
    logger.info("Discovery step for job %s audit %s", job_id, audit_id)
    result = await load_audit_state(audit_id)
    if result is None or result.intake_result is None or result.intake_result.audit_plan is None:
        raise RuntimeError(f"discovery step: intake state/audit plan missing for {audit_id}")

    result.current_phase = Phase.DISCOVERY
    discovery_result = await run_discovery_phase(
        audit_plan=result.intake_result.audit_plan,
        llm_client=_build_llm_client(),
        message_bus=MessageBus(),
    )

    if not discovery_result.success:
        result.success = False
        result.failure_reason = discovery_result.error or "Discovery failed"
        await persist_audit_state(result)
        return result

    result.discovery_result = discovery_result
    result.completed_phases.append(Phase.DISCOVERY)
    await persist_audit_state(result)
    return result


async def run_verification_step(
    job_id: str, audit_id: str, tech_stack: Optional[Dict[str, str]] = None
) -> AccessibilityAuditResult:
    """Load prior state, run the verification phase, and persist the updated state.

    Preconditions:
        - Discovery has already persisted state under ``audit_id`` with draft findings.
    Postconditions:
        - Returns the audit result with the verification result attached
          (``VERIFICATION`` recorded) on success, or ``success=False`` on a logical
          verification failure.
        - Raises ``RuntimeError`` if the persisted discovery state is missing.
    """
    logger.info("Verification step for job %s audit %s", job_id, audit_id)
    result = await load_audit_state(audit_id)
    if result is None or result.discovery_result is None:
        raise RuntimeError(f"verification step: discovery state missing for {audit_id}")

    result.current_phase = Phase.VERIFICATION
    verification_result = await run_verification_phase(
        audit_id=audit_id,
        draft_findings=result.discovery_result.draft_findings,
        stack=tech_stack or dict(DEFAULT_TECH_STACK),
        llm_client=_build_llm_client(),
        message_bus=MessageBus(),
    )

    if not verification_result.success:
        result.success = False
        result.failure_reason = verification_result.error or "Verification failed"
        await persist_audit_state(result)
        return result

    result.verification_result = verification_result
    result.completed_phases.append(Phase.VERIFICATION)
    await persist_audit_state(result)
    return result


async def run_report_packaging_step(job_id: str, audit_id: str) -> AccessibilityAuditResult:
    """Load prior state, run the report-packaging phase, and persist the updated state.

    Preconditions:
        - Verification has already persisted state under ``audit_id``.
    Postconditions:
        - Returns the audit result with the report-packaging result attached
          (``REPORT_PACKAGING`` recorded) on success, or ``success=False`` on a
          logical report-packaging failure. Final assembly (severity counts /
          summary / ``success``) is deferred to :func:`finalize_audit_step`.
        - Raises ``RuntimeError`` if the persisted verification state is missing.
    """
    logger.info("Report-packaging step for job %s audit %s", job_id, audit_id)
    result = await load_audit_state(audit_id)
    if result is None or result.verification_result is None:
        raise RuntimeError(f"report packaging step: verification state missing for {audit_id}")

    coverage_matrix = result.intake_result.coverage_matrix if result.intake_result else None
    result.current_phase = Phase.REPORT_PACKAGING
    report_result = await run_report_packaging_phase(
        audit_id=audit_id,
        verified_findings=result.verification_result.verified_findings,
        coverage_matrix=coverage_matrix,
        llm_client=_build_llm_client(),
        message_bus=MessageBus(),
    )

    if not report_result.success:
        result.success = False
        result.failure_reason = report_result.error or "Report packaging failed"
        await persist_audit_state(result)
        return result

    result.report_packaging_result = report_result
    if Phase.REPORT_PACKAGING not in result.completed_phases:
        result.completed_phases.append(Phase.REPORT_PACKAGING)
    await persist_audit_state(result)
    return result


async def finalize_audit_step(job_id: str, audit_id: str) -> AccessibilityAuditResult:
    """Load prior state, assemble the final audit result, and persist it.

    Preconditions:
        - Report packaging has already persisted state under ``audit_id`` with a
          successful ``report_packaging_result``.
    Postconditions:
        - Returns the finalized ``AccessibilityAuditResult`` (``success=True`` with
          severity counts + summary via :func:`finalize_audit_result`) and persists
          it. Raises ``RuntimeError`` if the persisted report state is missing.
    """
    logger.info("Finalize step for job %s audit %s", job_id, audit_id)
    result = await load_audit_state(audit_id)
    # Enforce finalize_audit_result's precondition (report packaging succeeded). The
    # workflow only schedules finalize after report_packaging returns PASS, so a
    # missing/unsuccessful report here is a plumbing defect that must fail loudly.
    if result is None or result.report_packaging_result is None:
        raise RuntimeError(f"finalize step: report-packaging state missing for {audit_id}")
    if not result.report_packaging_result.success:
        raise RuntimeError(f"finalize step: report-packaging did not succeed for {audit_id}")

    finalize_audit_result(result, result.report_packaging_result)
    await persist_audit_state(result)
    return result


# ---------------------------------------------------------------------------
# Retest execution core (symmetric with run_audit_job / execute_audit_job)
# ---------------------------------------------------------------------------


async def mark_audit_timed_out(job_id: str, audit_id: str, timebox_hours: int) -> None:
    """Mark an audit job failed because it exceeded its ``timebox_hours`` budget.

    The Temporal counterpart of thread mode's ``asyncio.wait_for`` timeout branch:
    it records the same failure reason (including the phases that did complete) on
    both the persisted audit state and the durable job record.

    Preconditions:
        - ``job_id``/``audit_id`` are non-empty and a job row exists for ``job_id``.
    Postconditions:
        - The job is marked ``failed`` with a timeout reason; when persisted audit
          state exists it is also flipped to ``success=False`` with that reason.
    """
    result = await load_audit_state(audit_id)
    completed = [p.value for p in result.completed_phases] if result is not None else []
    reason = f"Audit timed out after {timebox_hours} hour(s). Completed phases: {completed}"
    if result is not None:
        result.success = False
        result.failure_reason = reason
        await persist_audit_state(result)
    get_job_manager().update_job(job_id, status=JOB_STATUS_FAILED, error=reason)


async def run_retest_job(job_id: str, audit_id: str, finding_ids: List[str]) -> None:
    """Run a retest and persist its lifecycle to the shared job store.

    The propagating core (mirrors :func:`run_audit_job`): records ``running`` then a
    terminal state, but does NOT swallow an infrastructure failure so a Temporal
    activity can surface it to its retry policy. ``run_retest`` loads the audit
    state from the artifact store, so this works in a worker-only process.

    Preconditions:
        - ``job_id``/``audit_id`` are non-empty and a job row already exists for ``job_id``.
    Postconditions:
        - If the job is already terminal, this is a no-op (idempotent Temporal retry).
        - Otherwise the job ends in ``completed``/``failed``; an infrastructure
          exception propagates (last persisted state is ``running``).
    """
    manager = get_job_manager()
    existing = manager.get_job(job_id)
    if existing is not None and existing.get("status") in (JOB_STATUS_COMPLETED, JOB_STATUS_FAILED):
        return
    manager.update_job(job_id, status=JOB_STATUS_RUNNING, current_phase="retest", progress=30)
    result = await get_orchestrator().run_retest(audit_id, finding_ids)
    manager.update_job(
        job_id,
        status=JOB_STATUS_COMPLETED if result.success else JOB_STATUS_FAILED,
        progress=100,
        result=result.model_dump(),
        error=None if result.success else result.failure_reason,
    )


async def execute_retest_job(job_id: str, audit_id: str, finding_ids: List[str]) -> None:
    """FastAPI-background-task wrapper around :func:`run_retest_job`.

    The in-process path has no external retry mechanism, so an infrastructure
    exception is captured onto the job record (status ``failed``) rather than
    propagated.

    Preconditions:
        - ``job_id``/``audit_id`` are non-empty and a job row already exists for ``job_id``.
    Postconditions:
        - The job ends in ``completed`` or ``failed``; no exception propagates.
    """
    try:
        await run_retest_job(job_id, audit_id, finding_ids)
    except Exception as e:
        logger.exception("Retest job %s failed", job_id)
        get_job_manager().update_job(job_id, status=JOB_STATUS_FAILED, error=str(e))
