"""Tests for the shared gate-report base and the models migrated onto it."""

from __future__ import annotations

import pytest
from blog_compliance_agent.models import ComplianceReport, Violation
from blog_copy_editor_agent.models import CopyEditorOutput, FeedbackItem
from blog_fact_check_agent.models import FactCheckReport
from blog_plan_critic_agent.models import PlanCriticReport, PlanViolation
from pydantic import ValidationError
from shared import GateReport, GateViolation
from shared.gate_report import GateSeverity, GateStatus
from validators.models import CheckResult, ValidatorReport


def test_status_and_severity_literals_are_shared():
    """The canonical literals expose exactly the expected members."""
    assert set(GateStatus.__args__) == {"PASS", "FAIL"}
    assert set(GateSeverity.__args__) == {"must_fix", "should_fix", "consider"}


def test_to_dict_omits_none_and_returns_plain_dict():
    """GateReport.to_dict() drops None-valued fields and yields a plain dict."""

    class _Report(GateReport):
        status: GateStatus = "PASS"
        notes: str | None = None
        extra: str | None = "kept"

    data = _Report().to_dict()
    assert isinstance(data, dict)
    assert data == {"status": "PASS", "extra": "kept"}
    assert "notes" not in data


def test_gate_violation_requires_rule_id_and_description():
    """rule_id and description are mandatory on the shared violation base."""
    v = GateViolation(rule_id="r", description="d")
    assert (v.rule_id, v.description) == ("r", "d")
    with pytest.raises(ValidationError):
        GateViolation(rule_id="r")  # type: ignore[call-arg]


def test_all_reports_extend_gate_report():
    """Every migrated report is a GateReport and inherits one to_dict()."""
    for cls in (
        ValidatorReport,
        ComplianceReport,
        FactCheckReport,
        PlanCriticReport,
        CopyEditorOutput,
    ):
        assert issubclass(cls, GateReport)


def test_violation_models_extend_gate_violation_but_feedback_item_does_not():
    """Violation/PlanViolation share the base; FeedbackItem keeps its own shape."""
    assert issubclass(Violation, GateViolation)
    assert issubclass(PlanViolation, GateViolation)
    assert not issubclass(FeedbackItem, GateViolation)


def test_validator_report_to_dict_drops_null_check_details():
    """Null CheckResult.details drops out of the serialized validator report."""
    report = ValidatorReport(
        status="PASS", checks=[CheckResult(name="banned_phrases", status="PASS")]
    )
    data = report.to_dict()
    assert data["status"] == "PASS"
    assert data["checks"] == [{"name": "banned_phrases", "status": "PASS"}]
    assert "details" not in data["checks"][0]


def test_compliance_report_to_dict_keys_and_null_drop():
    """Compliance report keeps its keys; None notes/location_hint are omitted."""
    report = ComplianceReport(
        status="FAIL",
        violations=[Violation(rule_id="fmt.x", description="bad")],
        required_fixes=["fix it"],
    )
    data = report.to_dict()
    assert data["status"] == "FAIL"
    assert data["required_fixes"] == ["fix it"]
    assert data["violations"][0] == {
        "rule_id": "fmt.x",
        "description": "bad",
        "evidence_quotes": [],
    }
    assert "notes" not in data
    assert "location_hint" not in data["violations"][0]


def test_fact_check_report_preserves_dual_status_and_drops_null_notes():
    """FactCheckReport keeps both status axes; None notes drops from the dict."""
    report = FactCheckReport(claims_status="PASS", risk_status="FAIL", risk_flags=["legal"])
    data = report.to_dict()
    assert data["claims_status"] == "PASS"
    assert data["risk_status"] == "FAIL"
    assert data["risk_flags"] == ["legal"]
    assert "notes" not in data


def test_plan_critic_report_must_fix_count_and_to_dict():
    """must_fix_count() counts must_fix violations; to_dict keeps top-level keys."""
    report = PlanCriticReport(
        status="FAIL",
        approved=False,
        violations=[
            {"rule_id": "a", "description": "d1", "suggested_fix": "f1", "severity": "must_fix"},
            {"rule_id": "b", "description": "d2", "suggested_fix": "f2", "severity": "consider"},
        ],
    )
    assert report.must_fix_count() == 1
    data = report.to_dict()
    assert data["status"] == "FAIL"
    assert data["approved"] is False
    assert data["rubric_version"] == "v1"
    assert {v["rule_id"] for v in data["violations"]} == {"a", "b"}


def test_feedback_item_accepts_free_form_severity():
    """FeedbackItem.severity stays a plain str (values outside GateSeverity allowed)."""
    item = FeedbackItem(category="voice", severity="minor", issue="off tone")
    assert item.severity == "minor"


def test_copy_editor_output_to_dict_drops_null_feedback_fields():
    """CopyEditorOutput inherits to_dict(); null feedback location/suggestion drop out."""
    output = CopyEditorOutput(
        summary="focus on tone",
        feedback_items=[FeedbackItem(category="voice", severity="must_fix", issue="off tone")],
    )
    data = output.to_dict()
    assert data["approved"] is False
    assert data["summary"] == "focus on tone"
    assert data["feedback_items"][0] == {
        "category": "voice",
        "severity": "must_fix",
        "issue": "off tone",
    }
