"""Edge-case coverage for the specialist agents' JSON parsing/fallbacks and the
orchestrator's ``assemble`` padding.

These branches are not reached by the end-to-end
``test_market_research_orchestrator.py`` paths (the autouse Strands mock returns
well-formed JSON for every stage). Since the Strands graph was retired, the
parsing that used to live in the orchestrator's ``_parse_*_from_text`` helpers
now lives inside the ``agents.py`` dataclass agents, so these tests drive those
agents directly with malformed ``_call_agent`` output.
"""

from __future__ import annotations

import json

from market_research_team.agents import (
    _DEFAULT_SCRIPTS_FALLBACK,
    ConsistencyAgent,
    MarketViabilityAgent,
    ResearchScriptAgent,
    UserPsychologyAgent,
)
from market_research_team.models import (
    HumanReview,
    InterviewInsight,
    MarketSignal,
    ResearchMission,
    TeamTopology,
    ViabilityRecommendation,
    WorkflowStatus,
)
from market_research_team.orchestrator import MarketResearchOrchestrator

_MISSION = ResearchMission(
    product_concept="Concept",
    target_users="Users",
    business_goal="Goal",
    topology=TeamTopology.UNIFIED,
)


def _patch_call(monkeypatch, payload: str) -> None:
    monkeypatch.setattr("market_research_team.agents._call_agent", lambda agent, prompt: payload)


def test_psychology_falls_back_for_non_collection(monkeypatch) -> None:
    """A bare string (schema drift) → the two default signals, padded to two."""
    _patch_call(monkeypatch, json.dumps("just a string"))

    signals = UserPsychologyAgent().derive_signals([])

    assert [s.signal for s in signals] == ["User pain urgency", "Adoption motivation clarity"]


def test_consistency_recovers_from_non_dict_json(monkeypatch) -> None:
    """A non-dict consistency payload → a single default-named signal."""
    _patch_call(monkeypatch, json.dumps([1, 2, 3]))

    signals = ConsistencyAgent().analyze([InterviewInsight(source="a")])

    assert len(signals) == 1
    assert signals[0].signal == "Cross-interview theme consistency"
    assert signals[0].confidence == 0.5


def test_viability_recovers_from_non_dict_json(monkeypatch) -> None:
    _patch_call(monkeypatch, json.dumps([1, 2, 3]))

    rec = MarketViabilityAgent().recommend(_MISSION, [], insight_count=1)

    assert rec.verdict == "needs_more_validation"
    assert rec.suggested_next_experiments
    assert "Mission concept: Concept." in rec.rationale[0]


def test_viability_normalises_invalid_verdict(monkeypatch) -> None:
    _patch_call(monkeypatch, json.dumps({"verdict": "totally-bogus", "confidence": 0.9}))

    rec = MarketViabilityAgent().recommend(_MISSION, [], insight_count=1)

    assert rec.verdict == "needs_more_validation"
    assert rec.confidence == 0.9


def test_viability_zero_evidence_short_circuits() -> None:
    """insight_count == 0 → deterministic insufficient-evidence verdict (no LLM)."""
    rec = MarketViabilityAgent().recommend(_MISSION, [], insight_count=0)

    assert rec.verdict == "insufficient_evidence"
    assert rec.confidence == 0.3


def test_scripts_falls_back_when_payload_is_not_string_list(monkeypatch) -> None:
    # Dict payload → fallback path.
    _patch_call(monkeypatch, json.dumps({"oops": True}))
    assert ResearchScriptAgent().build_scripts(_MISSION) == list(_DEFAULT_SCRIPTS_FALLBACK)

    # List with a non-string element → fallback path.
    _patch_call(monkeypatch, json.dumps(["ok", 7]))
    assert ResearchScriptAgent().build_scripts(_MISSION) == list(_DEFAULT_SCRIPTS_FALLBACK)


def test_assemble_pads_signals_to_minimum_two() -> None:
    """``assemble`` pads an under-two signal set with the two default signals."""
    recommendation = ViabilityRecommendation(verdict="needs_more_validation", confidence=0.5)

    output = MarketResearchOrchestrator().assemble(
        _MISSION,
        HumanReview(approved=True),
        insights=[],
        signals=[],
        recommendation=recommendation,
        scripts=["a script"],
    )

    assert output.status == WorkflowStatus.READY_FOR_EXECUTION
    assert [s.signal for s in output.market_signals] == [
        "User pain urgency",
        "Adoption motivation clarity",
    ]
    assert output.proposed_research_scripts == ["a script"]


def test_assemble_preserves_supplied_signals_when_already_two() -> None:
    """No padding when the caller already supplies two-plus signals."""
    signals = [
        MarketSignal(signal="s1", confidence=0.6, evidence=["e1"]),
        MarketSignal(signal="s2", confidence=0.7, evidence=["e2"]),
        MarketSignal(signal="s3", confidence=0.8, evidence=["e3"]),
    ]
    recommendation = ViabilityRecommendation(verdict="promising_with_risks", confidence=0.8)

    output = MarketResearchOrchestrator().assemble(
        _MISSION,
        HumanReview(approved=False, feedback="hold"),
        insights=[],
        signals=signals,
        recommendation=recommendation,
        scripts=[],
    )

    assert output.status == WorkflowStatus.NEEDS_HUMAN_DECISION
    assert [s.signal for s in output.market_signals] == ["s1", "s2", "s3"]
    assert output.human_feedback == "hold"
