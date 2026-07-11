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
    # The viability stage is fed the mock's viability JSON (routed correctly even
    # though its prompt also mentions "market signals"), so the verdict is the
    # canned one — not a silently-defaulted "needs_more_validation".
    assert output.recommendation.verdict == "promising_with_risks"
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
    """LLM returns {"signal": null, ...} for the consistency stage — the
    ConsistencyAgent must fall back to the default signal name."""

    null_consistency_json = json.dumps({"signal": None, "confidence": 0.6, "evidence": ["theme A"]})

    # Route the consistency stage's LLM call to null-signal JSON; every other
    # stage keeps its schema-valid canned output.
    from market_research_team.tests.conftest import (
        SAMPLE_INSIGHT_JSON,
        SAMPLE_SCRIPTS_JSON,
        SAMPLE_SIGNALS_JSON,
        SAMPLE_VIABILITY_JSON,
    )

    def _custom_call_agent(agent, prompt):
        p = prompt.lower()
        # Same unique-instruction-phrase routing as the conftest mock (see there
        # for why substring keys on shared/embedded words mis-route stages).
        if "cross-interview consistency" in p:
            return null_consistency_json
        if "viability" in p or "verdict" in p:
            return SAMPLE_VIABILITY_JSON
        if "user psychology" in p:
            return SAMPLE_SIGNALS_JSON
        if "user interview transcript" in p:
            return SAMPLE_INSIGHT_JSON
        if "research artifacts" in p or "interview script" in p:
            return SAMPLE_SCRIPTS_JSON
        return SAMPLE_INSIGHT_JSON

    monkeypatch.setattr("market_research_team.agents._call_agent", _custom_call_agent)

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
