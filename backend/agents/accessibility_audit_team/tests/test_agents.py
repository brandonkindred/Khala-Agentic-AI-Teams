"""Tests for specialist agent base class and key agent logic."""

import pytest

from accessibility_audit_team.agents.base import AgentMessage, BaseSpecialistAgent, MessageBus
from accessibility_audit_team.models import (
    FindingState,
    IssueType,
    Scope,
    Severity,
    Surface,
)

# ---------------------------------------------------------------------------
# Concrete test agent (BaseSpecialistAgent is abstract)
# ---------------------------------------------------------------------------


class StubAgent(BaseSpecialistAgent):
    agent_code = "STUB"
    agent_name = "Stub Agent"

    async def process(self, context):
        return {"success": True}


class CrashingAgent(BaseSpecialistAgent):
    agent_code = "CRASH"
    agent_name = "Crashing Agent"

    async def process(self, context):
        raise RuntimeError("intentional crash")


# ---------------------------------------------------------------------------
# Finding factory tests
# ---------------------------------------------------------------------------


def test_create_finding_defaults():
    agent = StubAgent()
    finding = agent.create_finding(
        audit_id="audit_1",
        target="https://example.com",
        surface=Surface.WEB,
        issue_type=IssueType.CONTRAST,
        severity=Severity.HIGH,
        title="Low contrast",
        summary="Text contrast is too low",
        expected="4.5:1",
        actual="2.1:1",
        user_impact="Users cannot read text",
        wcag_scs=["1.4.3"],
    )

    assert finding.id.startswith("finding_")
    assert finding.state == FindingState.DRAFT
    assert finding.surface == Surface.WEB
    assert finding.severity == Severity.HIGH
    assert finding.scope == Scope.LOCALIZED
    assert finding.confidence == 0.7
    assert finding.created_by == "STUB"
    assert len(finding.wcag_mappings) == 1
    assert finding.wcag_mappings[0].sc == "1.4.3"


def test_create_finding_custom_scope_and_confidence():
    agent = StubAgent()
    finding = agent.create_finding(
        audit_id="audit_1",
        target="https://example.com",
        surface=Surface.IOS,
        issue_type=IssueType.TARGET_SIZE,
        severity=Severity.MEDIUM,
        title="Small touch target",
        summary="Button is too small",
        expected="44x44pt minimum",
        actual="20x20pt",
        user_impact="Motor impaired users cannot tap",
        wcag_scs=["2.5.8"],
        scope=Scope.SYSTEMIC,
        confidence=0.95,
    )

    assert finding.scope == Scope.SYSTEMIC
    assert finding.confidence == 0.95
    assert finding.surface == Surface.IOS


# ---------------------------------------------------------------------------
# Message bus integration tests
# ---------------------------------------------------------------------------


def test_agent_send_via_bus():
    bus = MessageBus()
    sender = StubAgent(message_bus=bus)
    sender.agent_code = "WAS"

    sender.send_message(
        AgentMessage(from_agent="WAS", to_agent="REE", message_type="capture")
    )

    # Receiver should get the message
    receiver = StubAgent(message_bus=bus)
    receiver.agent_code = "REE"
    msgs = receiver.receive_messages()
    assert len(msgs) == 1
    assert msgs[0].message_type == "capture"


def test_agent_send_without_bus_uses_local_queue():
    agent = StubAgent()
    agent.send_message(
        AgentMessage(from_agent="X", to_agent="Y", message_type="test")
    )
    # Local queue — only the sender can receive
    msgs = agent.receive_messages()
    assert len(msgs) == 1


# ---------------------------------------------------------------------------
# safe_process tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_safe_process_success():
    agent = StubAgent()
    result = await agent.safe_process({})
    assert result["success"] is True


@pytest.mark.anyio
async def test_safe_process_catches_exception():
    agent = CrashingAgent()
    result = await agent.safe_process({})
    assert result["success"] is False
    assert "intentional crash" in result["error"]
    assert result["agent"] == "CRASH"


# ---------------------------------------------------------------------------
# QCR severity normalization tests
# ---------------------------------------------------------------------------


def test_severity_normalization_normalizes_down():
    """CRITICAL should be normalized DOWN to MEDIUM (most common), LOW stays LOW."""
    from accessibility_audit_team.agents.qa_consistency_reviewer import QAConsistencyReviewer
    from accessibility_audit_team.models import Finding

    qcr = QAConsistencyReviewer()

    findings = []
    for i, sev in enumerate([Severity.CRITICAL, Severity.MEDIUM, Severity.MEDIUM, Severity.MEDIUM]):
        findings.append(
            Finding(
                id=f"f_{i}",
                surface=Surface.WEB,
                target="https://example.com",
                issue_type=IssueType.CONTRAST,
                severity=sev,
                scope=Scope.LOCALIZED,
                confidence=0.8,
                title="Test",
                summary="Test",
                expected="Expected",
                actual="Actual",
                user_impact="Impact",
                pattern_id="pattern_1",
            )
        )

    result = qcr.normalize_severity(findings)

    severities = [f.severity for f in result]
    # CRITICAL should be normalized DOWN to MEDIUM (most common)
    assert severities[0] == Severity.MEDIUM
    # MEDIUM findings stay MEDIUM
    assert severities[1] == Severity.MEDIUM
    assert severities[2] == Severity.MEDIUM
    assert severities[3] == Severity.MEDIUM


def test_severity_normalization_does_not_normalize_up():
    """LOW should NOT be normalized UP to HIGH."""
    from accessibility_audit_team.agents.qa_consistency_reviewer import QAConsistencyReviewer
    from accessibility_audit_team.models import Finding

    qcr = QAConsistencyReviewer()

    findings = []
    for i, sev in enumerate([Severity.HIGH, Severity.HIGH, Severity.HIGH, Severity.LOW]):
        findings.append(
            Finding(
                id=f"f_{i}",
                surface=Surface.WEB,
                target="https://example.com",
                issue_type=IssueType.KEYBOARD,
                severity=sev,
                scope=Scope.LOCALIZED,
                confidence=0.8,
                title="Test",
                summary="Test",
                expected="Expected",
                actual="Actual",
                user_impact="Impact",
                pattern_id="pattern_2",
            )
        )

    result = qcr.normalize_severity(findings)

    # LOW should stay LOW (not normalized up to HIGH)
    assert result[3].severity == Severity.LOW
    # HIGH findings stay HIGH
    assert result[0].severity == Severity.HIGH
