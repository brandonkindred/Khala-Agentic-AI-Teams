import json
from pathlib import Path

from market_research_team.agents import (
    MarketViabilityAgent,
    ResearchScriptAgent,
    TranscriptIngestionAgent,
    UserPsychologyAgent,
    UXResearchAgent,
    _parse_json,
)
from market_research_team.models import InterviewInsight, MarketSignal, ResearchMission

# ---------------------------------------------------------------------------
# TranscriptIngestionAgent (unchanged — pure I/O, no LLM)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# UXResearchAgent (Strands-powered)
# ---------------------------------------------------------------------------


def test_ux_research_extracts_fields_from_llm_response() -> None:
    agent = UXResearchAgent()
    insight = agent.analyze("source.txt", "Some transcript about user pain and jobs.")

    assert insight.source == "source.txt"
    assert isinstance(insight.user_jobs, list)
    assert len(insight.user_jobs) >= 1
    assert isinstance(insight.pain_points, list)
    assert len(insight.pain_points) >= 1
    assert isinstance(insight.desired_outcomes, list)
    assert isinstance(insight.direct_quotes, list)


def test_ux_research_fallback_on_bad_llm_response(monkeypatch) -> None:
    monkeypatch.setattr(
        "market_research_team.agents._call_agent",
        lambda agent, prompt: "not valid json at all",
    )
    agent = UXResearchAgent()
    insight = agent.analyze("source.txt", "neutral text")

    assert insight.source == "source.txt"
    assert insight.user_jobs[0].startswith("Identify the core user job")
    assert insight.pain_points[0].startswith("Validate top workflow frictions")
    assert insight.desired_outcomes[0].startswith("Confirm measurable success")


# ---------------------------------------------------------------------------
# UserPsychologyAgent (Strands-powered)
# ---------------------------------------------------------------------------


def test_user_psychology_returns_signals() -> None:
    insights = [
        InterviewInsight(
            source="a",
            pain_points=["pain1", "pain2"],
            desired_outcomes=["outcome1"],
        )
    ]
    signals = UserPsychologyAgent().derive_signals(insights)
    assert len(signals) >= 2
    assert all(isinstance(s, MarketSignal) for s in signals)
    assert all(0.0 <= s.confidence <= 1.0 for s in signals)


def test_user_psychology_handles_empty_insights() -> None:
    signals = UserPsychologyAgent().derive_signals([])
    assert len(signals) >= 2
    assert all(isinstance(s, MarketSignal) for s in signals)


def test_user_psychology_pads_to_minimum_two_signals(monkeypatch) -> None:
    monkeypatch.setattr(
        "market_research_team.agents._call_agent",
        lambda agent, prompt: json.dumps(
            [{"signal": "Only one", "confidence": 0.6, "evidence": ["e1"]}]
        ),
    )
    signals = UserPsychologyAgent().derive_signals([])
    assert len(signals) >= 2


# ---------------------------------------------------------------------------
# MarketViabilityAgent (Strands-powered)
# ---------------------------------------------------------------------------


def test_market_viability_insufficient_evidence_no_llm() -> None:
    mission = ResearchMission(
        product_concept="Concept",
        target_users="Users",
        business_goal="Goal",
    )
    result = MarketViabilityAgent().recommend(mission, [], 0)
    assert result.verdict == "insufficient_evidence"
    assert result.confidence == 0.3


def test_market_viability_with_signals() -> None:
    mission = ResearchMission(
        product_concept="Concept",
        target_users="Users",
        business_goal="Goal",
    )
    signals = [
        MarketSignal(signal="s1", confidence=0.7, evidence=["e1"]),
        MarketSignal(signal="s2", confidence=0.8, evidence=["e2"]),
    ]
    result = MarketViabilityAgent().recommend(mission, signals, 2)
    assert result.verdict in {
        "insufficient_evidence",
        "needs_more_validation",
        "promising_with_risks",
    }
    assert 0.0 <= result.confidence <= 1.0
    assert isinstance(result.rationale, list)
    assert isinstance(result.suggested_next_experiments, list)


def test_market_viability_fallback_on_bad_response(monkeypatch) -> None:
    monkeypatch.setattr(
        "market_research_team.agents._call_agent",
        lambda agent, prompt: "broken json",
    )
    mission = ResearchMission(
        product_concept="Concept",
        target_users="Users",
        business_goal="Goal",
    )
    result = MarketViabilityAgent().recommend(
        mission, [MarketSignal(signal="s1", confidence=0.5)], 1
    )
    assert result.verdict == "needs_more_validation"


def test_market_viability_invalid_verdict_defaults(monkeypatch) -> None:
    monkeypatch.setattr(
        "market_research_team.agents._call_agent",
        lambda agent, prompt: json.dumps(
            {
                "verdict": "INVALID_VALUE",
                "confidence": 0.5,
                "rationale": [],
                "suggested_next_experiments": [],
            }
        ),
    )
    mission = ResearchMission(
        product_concept="Concept",
        target_users="Users",
        business_goal="Goal",
    )
    result = MarketViabilityAgent().recommend(
        mission, [MarketSignal(signal="s1", confidence=0.5)], 1
    )
    assert result.verdict == "needs_more_validation"


# ---------------------------------------------------------------------------
# ResearchScriptAgent (Strands-powered)
# ---------------------------------------------------------------------------


def test_research_script_builder_returns_scripts() -> None:
    mission = ResearchMission(
        product_concept="Concept",
        target_users="Users",
        business_goal="prioritizing roadmap items",
    )
    scripts = ResearchScriptAgent().build_scripts(mission)
    assert isinstance(scripts, list)
    assert len(scripts) >= 1
    assert all(isinstance(s, str) for s in scripts)


def test_research_script_builder_fallback_on_bad_response(monkeypatch) -> None:
    monkeypatch.setattr(
        "market_research_team.agents._call_agent",
        lambda agent, prompt: "not json",
    )
    mission = ResearchMission(
        product_concept="Concept",
        target_users="Users",
        business_goal="Goal",
    )
    scripts = ResearchScriptAgent().build_scripts(mission)
    assert isinstance(scripts, list)
    assert len(scripts) == 3


# ---------------------------------------------------------------------------
# _parse_json helper
# ---------------------------------------------------------------------------


def test_parse_json_strips_markdown_fences() -> None:
    raw = '```json\n{"key": "value"}\n```'
    result = _parse_json(raw, {})
    assert result == {"key": "value"}


def test_parse_json_returns_fallback_on_invalid_input() -> None:
    assert _parse_json("", {"default": True}) == {"default": True}
    assert _parse_json("not json", []) == []
    assert _parse_json(None, "fallback") == "fallback"
