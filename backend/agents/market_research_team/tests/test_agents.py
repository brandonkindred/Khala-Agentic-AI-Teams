from pathlib import Path

from market_research_team.agents import (
    MarketViabilityAgent,
    ResearchScriptAgent,
    TranscriptIngestionAgent,
    UserPsychologyAgent,
    UXResearchAgent,
)
from market_research_team.models import InterviewInsight, MarketSignal, ResearchMission


def test_transcript_ingestion_loads_inline_and_folder(tmp_path: Path) -> None:
    folder = tmp_path / "transcripts"
    folder.mkdir()
    (folder / "a.txt").write_text("First line\nNeed better reporting", encoding="utf-8")
    (folder / "b.txt").write_text("", encoding="utf-8")
    (folder / "c.md").write_text("ignore me", encoding="utf-8")

    mission = ResearchMission(
        product_concept="Concept",
        target_users="Users",
        business_goal="Goal",
        transcript_folder_path=str(folder),
        transcripts=[" inline text ", "   "],
    )

    loaded = TranscriptIngestionAgent().load_transcripts(mission)
    assert loaded[0] == ("inline_transcript_1", "inline text")
    assert any(name == "a.txt" for name, _ in loaded)
    assert all(name != "b.txt" for name, _ in loaded)
    assert all(name != "c.md" for name, _ in loaded)


def test_transcript_ingestion_handles_missing_folder() -> None:
    mission = ResearchMission(
        product_concept="Concept",
        target_users="Users",
        business_goal="Goal",
        transcript_folder_path="/tmp/does-not-exist-xyz",
        transcripts=[],
    )
    assert TranscriptIngestionAgent().load_transcripts(mission) == []


def test_ux_research_extracts_expected_fields_and_fallbacks() -> None:
    transcript = (
        '"I need this fixed"\n'
        "I am trying to speed up onboarding.\n"
        "Main pain is handoffs across teams.\n"
        "Desired outcome is fewer escalations."
    )
    insight = UXResearchAgent().analyze("source.txt", transcript)
    assert insight.source == "source.txt"
    assert insight.user_jobs
    assert insight.pain_points
    assert insight.desired_outcomes
    assert insight.direct_quotes

    fallback = UXResearchAgent().analyze("source2.txt", "just neutral text")
    assert fallback.user_jobs[0].startswith("Identify the core user job")
    assert fallback.pain_points[0].startswith("Validate top workflow frictions")
    assert fallback.desired_outcomes[0].startswith("Confirm measurable success")


def test_user_psychology_signals_with_and_without_insights() -> None:
    insights = [
        InterviewInsight(
            source="a",
            pain_points=["pain1", "pain2"],
            desired_outcomes=["outcome1"],
        )
    ]
    signals = UserPsychologyAgent().derive_signals(insights)
    assert len(signals) == 2
    assert signals[0].signal == "User pain urgency"
    assert signals[1].signal == "Adoption motivation clarity"

    empty_signals = UserPsychologyAgent().derive_signals([])
    assert empty_signals[0].evidence[0].startswith("No direct pain")
    assert empty_signals[1].evidence[0].startswith("No clear desired")


def test_market_viability_recommendation_branches() -> None:
    mission = ResearchMission(
        product_concept="Concept",
        target_users="Users",
        business_goal="Goal",
    )
    agent = MarketViabilityAgent()

    insufficient = agent.recommend(mission, [], 0)
    assert insufficient.verdict == "insufficient_evidence"
    assert insufficient.confidence == 0.3

    low = agent.recommend(
        mission,
        [MarketSignal(signal="s1", confidence=0.5), MarketSignal(signal="s2", confidence=0.7)],
        1,
    )
    assert low.verdict == "needs_more_validation"

    high = agent.recommend(
        mission,
        [MarketSignal(signal="s1", confidence=0.9), MarketSignal(signal="s2", confidence=0.8)],
        2,
    )
    assert high.verdict == "promising_with_risks"


def test_research_script_builder_includes_business_goal() -> None:
    mission = ResearchMission(
        product_concept="Concept",
        target_users="Users",
        business_goal="prioritizing roadmap items",
    )
    scripts = ResearchScriptAgent().build_scripts(mission)
    assert len(scripts) == 3
    assert "prioritizing roadmap items" in scripts[0]
