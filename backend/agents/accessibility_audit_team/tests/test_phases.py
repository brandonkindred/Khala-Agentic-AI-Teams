"""Tests for phase runners."""

from unittest.mock import AsyncMock, patch

import pytest

from accessibility_audit_team.models import (
    AuditPlan,
    AuditTargets,
    Finding,
    FindingState,
    IssueType,
    Scope,
    Severity,
    Surface,
    WCAGMapping,
)
from accessibility_audit_team.phases.discovery import run_discovery_phase
from accessibility_audit_team.phases.verification import run_verification_phase


def _make_finding(finding_id: str, severity: Severity = Severity.HIGH) -> Finding:
    return Finding(
        id=finding_id,
        surface=Surface.WEB,
        target="https://example.com",
        issue_type=IssueType.CONTRAST,
        severity=severity,
        scope=Scope.LOCALIZED,
        confidence=0.8,
        title="Test finding",
        summary="Test",
        expected="Expected",
        actual="Actual",
        user_impact="Impact",
        wcag_mappings=[WCAGMapping(sc="1.4.3", name="Contrast", confidence=0.8, rationale="")],
        created_by="WAS",
    )


# ---------------------------------------------------------------------------
# Discovery Phase
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_discovery_phase_with_no_targets():
    plan = AuditPlan(audit_id="audit_empty", targets=AuditTargets())
    result = await run_discovery_phase(plan)
    assert result.success is True
    assert result.draft_findings == []
    assert result.pages_scanned == 0


@pytest.mark.anyio
async def test_discovery_phase_runs_was():
    plan = AuditPlan(
        audit_id="audit_web",
        targets=AuditTargets(web_urls=["https://example.com"]),
    )

    mock_finding = _make_finding("f_web")

    with patch(
        "accessibility_audit_team.agents.web_audit_specialist.WebAuditSpecialist.process",
        new_callable=AsyncMock,
        return_value={"success": True, "findings": [mock_finding], "scan_results": []},
    ), patch(
        "accessibility_audit_team.agents.evidence_engineer.EvidenceEngineer.process",
        new_callable=AsyncMock,
        return_value={"success": True},
    ), patch(
        "accessibility_audit_team.agents.qa_consistency_reviewer.QAConsistencyReviewer.process",
        new_callable=AsyncMock,
        return_value={"success": True, "patterns": [], "deduped_findings": [mock_finding]},
    ):
        result = await run_discovery_phase(plan)

    assert result.success is True
    assert len(result.draft_findings) == 1
    assert result.draft_findings[0].id == "f_web"


# ---------------------------------------------------------------------------
# Verification Phase
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_verification_phase_with_no_findings():
    result = await run_verification_phase(audit_id="audit_1", draft_findings=[])
    assert result.success is True
    assert result.verified_findings == []


@pytest.mark.anyio
async def test_verification_phase_high_impact_gets_at_verified():
    critical_finding = _make_finding("f_critical", Severity.CRITICAL)
    verified_copy = critical_finding.model_copy()
    verified_copy.state = FindingState.VERIFIED

    with patch(
        "accessibility_audit_team.agents.assistive_tech_specialist.AssistiveTechSpecialist.process",
        new_callable=AsyncMock,
        return_value={"success": True, "verified_findings": [verified_copy], "rejected_finding_ids": []},
    ), patch(
        "accessibility_audit_team.agents.standards_mapping_specialist.StandardsMappingSpecialist.process",
        new_callable=AsyncMock,
        return_value={"success": True, "mapped_findings": [verified_copy]},
    ), patch(
        "accessibility_audit_team.agents.remediation_advisor.RemediationAdvisor.process",
        new_callable=AsyncMock,
        return_value={"success": True, "remediated_findings": [verified_copy]},
    ), patch(
        "accessibility_audit_team.agents.evidence_engineer.EvidenceEngineer.process",
        new_callable=AsyncMock,
        return_value={"success": True},
    ):
        result = await run_verification_phase(
            audit_id="audit_1",
            draft_findings=[critical_finding],
        )

    assert result.success is True
    assert len(result.verified_findings) >= 1


@pytest.mark.anyio
async def test_verification_medium_finding_without_evidence():
    """Medium findings without evidence should get NEEDS_VERIFICATION state."""
    medium_finding = _make_finding("f_medium", Severity.MEDIUM)

    with patch(
        "accessibility_audit_team.agents.standards_mapping_specialist.StandardsMappingSpecialist.process",
        new_callable=AsyncMock,
        return_value={"success": True, "mapped_findings": [medium_finding]},
    ), patch(
        "accessibility_audit_team.agents.remediation_advisor.RemediationAdvisor.process",
        new_callable=AsyncMock,
        return_value={"success": True, "remediated_findings": [medium_finding]},
    ), patch(
        "accessibility_audit_team.agents.evidence_engineer.EvidenceEngineer.process",
        new_callable=AsyncMock,
        return_value={"success": True},
    ):
        result = await run_verification_phase(
            audit_id="audit_1",
            draft_findings=[medium_finding],
        )

    assert result.success is True
    # The medium finding should still be in verified_findings (not rejected)
    assert len(result.verified_findings) == 1
    assert result.verified_findings[0].state == FindingState.NEEDS_VERIFICATION
