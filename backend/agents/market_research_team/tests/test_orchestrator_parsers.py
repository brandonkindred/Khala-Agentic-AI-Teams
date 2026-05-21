"""Unit coverage for the orchestrator's text-parsing helpers and the
signal-padding fallback. These branches are not reached by the
end-to-end ``test_market_research_orchestrator.py`` paths because the
autouse Strands mock always returns well-formed JSON for every node.
"""

from __future__ import annotations

import json

from market_research_team.models import ResearchMission, TeamTopology
from market_research_team.orchestrator import (
    _DEFAULT_SCRIPTS_FALLBACK,
    _parse_insights_from_text,
    _parse_scripts_from_text,
    _parse_signals_from_text,
    _parse_viability_from_text,
)


def test_parse_insights_handles_json_list_of_objects() -> None:
    payload = json.dumps(
        [
            {
                "source": "interview_a",
                "user_jobs": ["job-a"],
                "pain_points": ["pain-a"],
                "desired_outcomes": ["outcome-a"],
                "direct_quotes": ["quote-a"],
            },
            {
                "user_jobs": ["job-b"],
                "pain_points": [],
                "desired_outcomes": [],
                "direct_quotes": [],
            },
            "not-an-object-skipped",
        ]
    )

    insights = _parse_insights_from_text(payload)

    assert [i.source for i in insights] == ["interview_a", "graph_analysis"]
    assert insights[0].user_jobs == ["job-a"]
    assert insights[1].user_jobs == ["job-b"]


def test_parse_insights_returns_empty_for_non_collection() -> None:
    assert _parse_insights_from_text(json.dumps(42)) == []


def test_parse_signals_returns_empty_for_non_collection() -> None:
    assert _parse_signals_from_text(json.dumps("just a string")) == []


def test_parse_viability_recovers_from_non_dict_json() -> None:
    mission = ResearchMission(
        product_concept="Concept",
        target_users="Users",
        business_goal="Goal",
        topology=TeamTopology.UNIFIED,
    )

    rec = _parse_viability_from_text(json.dumps([1, 2, 3]), mission)

    assert rec.verdict == "needs_more_validation"
    assert rec.suggested_next_experiments
    assert "Mission concept: Concept." in rec.rationale[0]


def test_parse_viability_normalises_invalid_verdict() -> None:
    mission = ResearchMission(
        product_concept="Concept",
        target_users="Users",
        business_goal="Goal",
        topology=TeamTopology.UNIFIED,
    )

    payload = json.dumps({"verdict": "totally-bogus", "confidence": 0.9})
    rec = _parse_viability_from_text(payload, mission)

    assert rec.verdict == "needs_more_validation"
    assert rec.confidence == 0.9


def test_parse_scripts_falls_back_when_payload_is_not_string_list() -> None:
    # Dict payload → fallback path.
    assert _parse_scripts_from_text(json.dumps({"oops": True})) == list(
        _DEFAULT_SCRIPTS_FALLBACK
    )

    # List with a non-string element → fallback path.
    assert _parse_scripts_from_text(json.dumps(["ok", 7])) == list(_DEFAULT_SCRIPTS_FALLBACK)


def test_orchestrator_pads_signals_when_graph_returns_none(monkeypatch) -> None:
    """Unified topology, no transcripts, and an empty psychology node forces
    the ``while len(market_signals) < 2`` padding loop to fire twice.
    """

    from market_research_team.models import HumanReview
    from market_research_team.orchestrator import MarketResearchOrchestrator

    def _empty_extract(result, node_id: str) -> str:
        if node_id == "psychology":
            return ""  # _parse_signals_from_text path is skipped, signals start empty
        if node_id == "ux_research":
            return ""
        if node_id == "viability_synthesis":
            return ""
        if node_id == "scripts":
            return ""
        return ""

    monkeypatch.setattr(
        "market_research_team.orchestrator.extract_node_text", _empty_extract
    )

    mission = ResearchMission(
        product_concept="Padding probe",
        target_users="signal-pad users",
        business_goal="hit the padding loop",
        topology=TeamTopology.UNIFIED,
    )

    output = MarketResearchOrchestrator().run(mission, HumanReview(approved=True))

    # Padding loop fills with the two fallback signals.
    assert len(output.market_signals) == 2
    assert output.market_signals[0].signal == "User pain urgency"
    assert output.market_signals[1].signal == "Adoption motivation clarity"
    # Empty scripts node → _parse_scripts_from_text not called, default fallback used.
    assert output.proposed_research_scripts == list(_DEFAULT_SCRIPTS_FALLBACK)
