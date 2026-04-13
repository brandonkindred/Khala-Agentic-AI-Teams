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
