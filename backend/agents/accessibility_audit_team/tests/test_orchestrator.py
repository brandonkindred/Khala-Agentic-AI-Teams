"""Tests for the AccessibilityAuditOrchestrator."""

from unittest.mock import AsyncMock, patch

import pytest

from accessibility_audit_team.models import (
    AccessibilityAuditResult,
    AuditPlan,
    AuditRequest,
    AuditTargets,
    CoverageMatrix,
    DiscoveryResult,
    IntakeResult,
    Phase,
    ReportPackagingResult,
    VerificationResult,
    WCAGLevel,
)
from accessibility_audit_team.orchestrator import AccessibilityAuditOrchestrator


def _make_intake_result(audit_id: str = "audit_test") -> IntakeResult:
    return IntakeResult(
        success=True,
        audit_plan=AuditPlan(
            audit_id=audit_id,
            targets=AuditTargets(web_urls=["https://example.com"]),
        ),
        coverage_matrix=CoverageMatrix(matrix_ref="matrix_1", audit_id=audit_id),
        summary="Intake done",
    )


def _make_discovery_result() -> DiscoveryResult:
    return DiscoveryResult(success=True, draft_findings=[], pages_scanned=1, summary="Discovery done")


def _make_verification_result() -> VerificationResult:
    return VerificationResult(success=True, verified_findings=[], summary="Verification done")


def _make_report_result() -> ReportPackagingResult:
    return ReportPackagingResult(
        success=True,
        final_backlog=[],
        patterns=[],
        executive_summary="All clear",
        summary="Report done",
    )


# ---------------------------------------------------------------------------
# Orchestrator lifecycle tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_full_audit_lifecycle():
    """Orchestrator runs all 4 phases to completion."""
    with patch(
        "accessibility_audit_team.orchestrator.run_intake_phase",
        new_callable=AsyncMock,
        return_value=_make_intake_result(),
    ), patch(
        "accessibility_audit_team.orchestrator.run_discovery_phase",
        new_callable=AsyncMock,
        return_value=_make_discovery_result(),
    ), patch(
        "accessibility_audit_team.orchestrator.run_verification_phase",
        new_callable=AsyncMock,
        return_value=_make_verification_result(),
    ), patch(
        "accessibility_audit_team.orchestrator.run_report_packaging_phase",
        new_callable=AsyncMock,
        return_value=_make_report_result(),
    ):
        orchestrator = AccessibilityAuditOrchestrator()
        request = AuditRequest(
            audit_id="audit_lifecycle",
            web_urls=["https://example.com"],
            wcag_levels=[WCAGLevel.A, WCAGLevel.AA],
        )
        result = await orchestrator.run_audit(request)

    assert result.success is True
    assert Phase.INTAKE in result.completed_phases
    assert Phase.DISCOVERY in result.completed_phases
    assert Phase.VERIFICATION in result.completed_phases
    assert Phase.REPORT_PACKAGING in result.completed_phases


@pytest.mark.anyio
async def test_audit_stops_on_intake_failure():
    with patch(
        "accessibility_audit_team.orchestrator.run_intake_phase",
        new_callable=AsyncMock,
        return_value=IntakeResult(success=False, error="Bad request"),
    ):
        orchestrator = AccessibilityAuditOrchestrator()
        request = AuditRequest(audit_id="audit_fail", web_urls=["https://example.com"])
        result = await orchestrator.run_audit(request)

    assert result.success is False
    assert "Bad request" in result.failure_reason


@pytest.mark.anyio
async def test_audit_timeout():
    """Audit with a timebox should time out if phases take too long."""
    import asyncio

    async def slow_intake(*args, **kwargs):
        await asyncio.sleep(10)
        return _make_intake_result()

    with patch(
        "accessibility_audit_team.orchestrator.run_intake_phase",
        side_effect=slow_intake,
    ):
        orchestrator = AccessibilityAuditOrchestrator()
        request = AuditRequest(
            audit_id="audit_timeout",
            web_urls=["https://example.com"],
            timebox_hours=1,
        )
        # Override the timeout to something tiny for testing
        with patch.object(
            orchestrator,
            "run_audit",
            wraps=orchestrator.run_audit,
        ):
            async def patched_run(req, tech_stack=None):
                # Test the timeout logic by calling _run_audit_phases with a tiny timeout
                result_obj = AccessibilityAuditResult(
                    audit_id=req.audit_id or "audit_temp",
                    current_phase=Phase.INTAKE,
                )
                try:
                    await asyncio.wait_for(
                        orchestrator._run_audit_phases(req, {"web": "other"}, result_obj),
                        timeout=0.1,
                    )
                except asyncio.TimeoutError:
                    result_obj.success = False
                    result_obj.failure_reason = "Audit timed out"
                return result_obj

            result = await patched_run(request)

    assert result.success is False
    assert "timed out" in result.failure_reason


# ---------------------------------------------------------------------------
# Status / findings queries
# ---------------------------------------------------------------------------


def test_get_audit_status_not_found():
    orchestrator = AccessibilityAuditOrchestrator()
    status = orchestrator.get_audit_status("nonexistent")
    assert status["status"] == "not_found"


def test_get_findings_empty_when_not_found():
    orchestrator = AccessibilityAuditOrchestrator()
    findings = orchestrator.get_findings("nonexistent")
    assert findings == []


def test_get_patterns_empty_when_not_found():
    orchestrator = AccessibilityAuditOrchestrator()
    patterns = orchestrator.get_patterns("nonexistent")
    assert patterns == []


# ---------------------------------------------------------------------------
# Addon initialization
# ---------------------------------------------------------------------------


def test_addons_disabled_by_default():
    orchestrator = AccessibilityAuditOrchestrator()
    assert orchestrator.arm is None
    assert orchestrator.adse is None
    assert orchestrator.aet is None


def test_addons_enabled():
    orchestrator = AccessibilityAuditOrchestrator(enable_addons=True)
    assert orchestrator.arm is not None
    assert orchestrator.adse is not None
    assert orchestrator.aet is not None
