import json

from market_research_team.models import HumanReview, ResearchMission, TeamTopology, WorkflowStatus
from market_research_team.orchestrator import MarketResearchOrchestrator


def test_orchestrator_needs_human_decision_without_approval() -> None:
    orchestrator = MarketResearchOrchestrator()
    mission = ResearchMission(
        product_concept="AI note summarizer",
        target_users="research operations leads",
        business_goal="faster synthesis",
        topology=TeamTopology.UNIFIED,
        transcripts=[
            '"Our job is to reduce synthesis time."\nBig pain: recruiting takes too long.\nWe need better tagging.'
        ],
    )

    output = orchestrator.run(
        mission, HumanReview(approved=False, feedback="Need stronger pricing proof")
    )

    assert output.status == WorkflowStatus.NEEDS_HUMAN_DECISION
    assert output.topology == TeamTopology.UNIFIED
    assert output.insights
    assert output.market_signals
    assert output.proposed_research_scripts


def test_orchestrator_ready_for_execution_with_approval() -> None:
    orchestrator = MarketResearchOrchestrator()
    mission = ResearchMission(
        product_concept="AI onboarding copilot",
        target_users="customer success managers",
        business_goal="shorten time to first value",
        topology=TeamTopology.SPLIT,
        transcripts=[
            "Users are trying to reduce setup time. The main issue is fragmented documentation."
        ],
    )

    output = orchestrator.run(mission, HumanReview(approved=True))

    assert output.status == WorkflowStatus.READY_FOR_EXECUTION
    assert output.topology == TeamTopology.SPLIT
    assert output.recommendation.verdict in {
        "promising_with_risks",
        "needs_more_validation",
        "insufficient_evidence",
    }
    assert any(
        signal.signal == "Cross-interview theme consistency" for signal in output.market_signals
    )


def test_orchestrator_split_mode_adds_consistency_signal_for_empty_inputs() -> None:
    orchestrator = MarketResearchOrchestrator()
    mission = ResearchMission(
        product_concept="AI onboarding copilot",
        target_users="customer success managers",
        business_goal="shorten time to first value",
        topology=TeamTopology.SPLIT,
    )

    output = orchestrator.run(mission, HumanReview(approved=False))

    consistency = [
        signal
        for signal in output.market_signals
        if signal.signal == "Cross-interview theme consistency"
    ]
    assert len(consistency) == 1
    assert "Insufficient transcript volume" in consistency[0].evidence[0]


def test_orchestrator_consistency_signal_survives_null_signal_name(monkeypatch) -> None:
    """LLM returns {"signal": null, ...} — should fall back to the default signal name."""

    null_consistency_json = json.dumps({"signal": None, "confidence": 0.6, "evidence": ["theme A"]})

    # Override extract_node_text to return null-signal consistency for the consistency node
    from market_research_team.tests.conftest import (
        SAMPLE_INSIGHT_JSON,
        SAMPLE_SCRIPTS_JSON,
        SAMPLE_SIGNALS_JSON,
        SAMPLE_VIABILITY_JSON,
    )

    def _custom_extract(result, node_id):
        if node_id == "consistency":
            return null_consistency_json
        if node_id == "ux_research":
            return SAMPLE_INSIGHT_JSON
        if node_id == "psychology":
            return SAMPLE_SIGNALS_JSON
        if node_id == "viability_synthesis":
            return SAMPLE_VIABILITY_JSON
        if node_id == "scripts":
            return SAMPLE_SCRIPTS_JSON
        return ""

    monkeypatch.setattr("market_research_team.orchestrator.extract_node_text", _custom_extract)

    orchestrator = MarketResearchOrchestrator()
    mission = ResearchMission(
        product_concept="Test product",
        target_users="Test users",
        business_goal="Test goal",
        topology=TeamTopology.SPLIT,
        transcripts=["Some transcript content about user pain."],
    )

    output = orchestrator.run(mission, HumanReview(approved=False))
    consistency = [
        s for s in output.market_signals if s.signal == "Cross-interview theme consistency"
    ]
    assert len(consistency) == 1
