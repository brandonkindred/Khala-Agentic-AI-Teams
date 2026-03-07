import pytest

from agents.accessibility_audit_team.a11y_agency_strands.app.agents import EngagementOrchestrator
from agents.accessibility_audit_team.a11y_agency_strands.app.runner import run_engagement


def test_engagement_workflow_runs_with_deterministic_order(tmp_path):
    result = run_engagement(
        "eng-123",
        raw_answers={"organization": "Kindred", "goals": ["Reduce legal risk"]},
    )
    assert list(result.keys()) == [
        "run_discovery",
        "run_inventory_setup",
        "run_component_audit",
        "run_journey_assessment",
        "run_page_audit",
        "run_architecture_audit",
        "run_infrastructure_audit",
        "run_wcag_coverage",
        "run_508_mapping",
        "run_scoring_and_prioritization",
        "run_reporting",
        "request_human_approval",
        "run_remediation_planning",
        "run_delivery",
        "run_retest_cycle",
    ]


def test_reporting_gate_blocks_if_required_phase_missing():
    orchestrator = EngagementOrchestrator({"artifact_root": ".tmp", "questionnaire": {}})
    orchestrator.run_component_audit("header-nav")
    with pytest.raises(ValueError, match="Reporting blocked"):
        orchestrator.run_reporting("eng-1")


def test_delivery_gate_blocks_without_approval():
    orchestrator = EngagementOrchestrator({"artifact_root": ".tmp", "questionnaire": {}})
    orchestrator.state.completed_tasks.extend(["reporting", "remediation", "sec508_mapping"])
    with pytest.raises(ValueError, match="Delivery blocked"):
        orchestrator.run_delivery()
