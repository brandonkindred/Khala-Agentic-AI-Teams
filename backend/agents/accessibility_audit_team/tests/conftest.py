import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from accessibility_audit_team.models import (  # noqa: E402
    AuditPlan,
    AuditRequest,
    AuditTargets,
    Finding,
    FindingState,
    IssueType,
    PatternCluster,
    Scope,
    Severity,
    Surface,
    WCAGLevel,
    WCAGMapping,
)


@pytest.fixture
def mock_llm_client():
    """A mock LLM client that does nothing."""
    return AsyncMock()


@pytest.fixture
def sample_audit_request():
    return AuditRequest(
        audit_id="audit_test01",
        name="Test Audit",
        web_urls=["https://example.com", "https://example.com/about"],
        critical_journeys=["Login", "Checkout"],
        wcag_levels=[WCAGLevel.A, WCAGLevel.AA],
    )


@pytest.fixture
def sample_audit_plan():
    return AuditPlan(
        audit_id="audit_test01",
        name="Test Audit",
        targets=AuditTargets(web_urls=["https://example.com"]),
    )


@pytest.fixture
def sample_findings():
    return [
        Finding(
            id="finding_001",
            state=FindingState.DRAFT,
            surface=Surface.WEB,
            target="https://example.com",
            issue_type=IssueType.CONTRAST,
            severity=Severity.HIGH,
            scope=Scope.LOCALIZED,
            confidence=0.8,
            title="Low contrast text",
            summary="Text has insufficient contrast ratio",
            expected="4.5:1 contrast ratio",
            actual="2.1:1 contrast ratio",
            user_impact="Users with low vision cannot read text",
            wcag_mappings=[WCAGMapping(sc="1.4.3", name="Contrast", confidence=0.9, rationale="")],
            created_by="WAS",
        ),
        Finding(
            id="finding_002",
            state=FindingState.DRAFT,
            surface=Surface.WEB,
            target="https://example.com",
            issue_type=IssueType.KEYBOARD,
            severity=Severity.CRITICAL,
            scope=Scope.SYSTEMIC,
            confidence=0.9,
            title="Keyboard trap in modal",
            summary="Focus gets trapped in modal dialog",
            expected="Focus should escape modal on Esc",
            actual="Focus is trapped indefinitely",
            user_impact="Keyboard users cannot navigate past modal",
            wcag_mappings=[WCAGMapping(sc="2.1.2", name="No Keyboard Trap", confidence=0.95, rationale="")],
            created_by="WAS",
        ),
        Finding(
            id="finding_003",
            state=FindingState.DRAFT,
            surface=Surface.WEB,
            target="https://example.com/about",
            issue_type=IssueType.STRUCTURE,
            severity=Severity.MEDIUM,
            scope=Scope.LOCALIZED,
            confidence=0.7,
            title="Missing heading hierarchy",
            summary="Page jumps from h1 to h4",
            expected="Sequential heading levels",
            actual="h1 followed by h4",
            user_impact="Screen reader users lose context of page structure",
            wcag_mappings=[WCAGMapping(sc="1.3.1", name="Info and Relationships", confidence=0.8, rationale="")],
            created_by="WAS",
        ),
    ]


@pytest.fixture
def sample_patterns():
    return [
        PatternCluster(
            pattern_id="pattern_001",
            name="Missing focus indicators",
            description="Multiple elements lack visible focus",
            linked_finding_ids=["finding_001", "finding_002"],
            severity=Severity.HIGH,
            scope=Scope.SYSTEMIC,
            issue_types=[IssueType.FOCUS],
            wcag_scs=["2.4.7"],
        ),
    ]
