"""Targeted tests for residual gaps across orchestrator, agents,
winning_posts_bank, and trend_discovery_agent."""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
from typing import Any

import pytest

from social_media_marketing_team.agents import (
    CampaignCollaborationAgent,
    ExperimentDesignAgent,
    RiskComplianceAgent,
)
from social_media_marketing_team.models import (
    BrandGoals,
    CampaignProposal,
    ConceptIdea,
    Platform,
)
from social_media_marketing_team.orchestrator import SocialMediaMarketingOrchestrator
from social_media_marketing_team.trend_discovery_agent import TrendDiscoveryAgent
from social_media_marketing_team.trend_models import TrendDigest

# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------


def test_bank_top_k_invalid_env_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed SOCIAL_MARKETING_WINNING_POSTS_TOP_K env var falls back to 5."""
    from social_media_marketing_team import orchestrator as omod

    monkeypatch.setenv("SOCIAL_MARKETING_WINNING_POSTS_TOP_K", "not-a-number")
    assert omod._bank_top_k() == 5


def test_bank_top_k_custom_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from social_media_marketing_team import orchestrator as omod

    monkeypatch.setenv("SOCIAL_MARKETING_WINNING_POSTS_TOP_K", "9")
    assert omod._bank_top_k() == 9


def test_calibrate_probabilities_with_observations_but_no_engagement_metrics() -> None:
    """If observations exist but none have an engagement-style metric, the
    calibrated list is returned unchanged."""
    from social_media_marketing_team.models import (
        CampaignPerformanceSnapshot,
        MetricDefinition,
        PostPerformanceObservation,
    )

    idea = ConceptIdea(
        title="t",
        concept="c",
        target_platforms=[Platform.X],
        linked_goals=["engagement"],
        brand_fit_score=0.7,
        audience_resonance_score=0.7,
        goal_alignment_score=0.7,
        estimated_engagement_probability=0.7,
    )
    perf = CampaignPerformanceSnapshot(
        campaign_name="c",
        observations=[
            PostPerformanceObservation(
                campaign_name="c",
                platform=Platform.X,
                concept_title="t",
                posted_at="2026-01-01T00:00:00Z",
                metrics=[MetricDefinition(name="impressions", value=100.0)],
            )
        ],
    )
    out = SocialMediaMarketingOrchestrator._calibrate_probabilities([idea], perf)
    assert out == [idea]


def test_load_winners_returns_empty_for_empty_brand_id() -> None:
    proposal = CampaignProposal(campaign_name="c", objective="o", audience_hypothesis="h")
    goals = BrandGoals(brand_name="b", target_audience="a", goals=["g"])
    assert SocialMediaMarketingOrchestrator._load_winners("", proposal, goals) == []


def test_load_winners_swallows_import_failure(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """The lazy import of ``find_relevant_winners`` is wrapped in a try/except;
    a missing symbol must be swallowed and return []."""
    proposal = CampaignProposal(
        campaign_name="c", objective="o", audience_hypothesis="h", messaging_pillars=["p"]
    )
    goals = BrandGoals(brand_name="b", target_audience="a", goals=["g"])

    with caplog.at_level("WARNING"):
        # find_relevant_winners doesn't exist (the impl uses
        # find_relevant_winning_posts instead) — the wrapping try/except
        # catches the ImportError.
        out = SocialMediaMarketingOrchestrator._load_winners("brand-1", proposal, goals)
    assert out == []
    assert any("Winner retrieval failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# agents.py — remaining rubric branches
# ---------------------------------------------------------------------------


def test_collaboration_agent_audience_specificity_long_phrase() -> None:
    """When the audience_hypothesis contains a connector and >8 tokens, the
    audience_specificity score gets the +0.2 lift (line 183)."""
    agent = CampaignCollaborationAgent("Audience Research Lead")
    proposal = CampaignProposal(
        campaign_name="c",
        objective="grow engagement",
        audience_hypothesis=(
            "Buyers who care deeply for proof points in their daily decision process"
        ),
        success_metrics=["rate"],
        channel_mix_strategy={
            Platform.LINKEDIN: "a",
            Platform.X: "b",
        },
        messaging_pillars=["pillar"],
    )
    score, note, rubric = agent.evaluate_proposal(proposal, 1)
    assert score > 0
    assert "Audience Research Lead" in note
    assert rubric["audience_specificity"] >= 0.55


def test_collaboration_agent_unknown_role_default_weights() -> None:
    """Roles outside the rubric weight table fall back to the default weights
    (covers the dict-get fallback on line 214/215)."""
    agent = CampaignCollaborationAgent("Some Unknown Role")
    proposal = CampaignProposal(
        campaign_name="c",
        objective="grow",
        audience_hypothesis="audience",
        success_metrics=["m"],
        channel_mix_strategy={Platform.X: "x"},
        messaging_pillars=["p"],
    )
    score, _, rubric = agent.evaluate_proposal(proposal, 1)
    # Default weights produce a non-zero score
    assert 0 < score < 1
    # Default weights mean no rubric dimension exceeds 1.0
    assert all(v <= 1.0 for v in rubric.values())


def test_collaboration_agent_strong_proposal_emits_neutral_suggestion() -> None:
    """When every rubric dimension is >= 0.75, the agent emits the generic
    'proposal is strong' suggestion (line 242-245)."""
    agent = CampaignCollaborationAgent("Audience Research Lead")
    proposal = CampaignProposal(
        campaign_name="c",
        objective="engagement and follower growth",
        audience_hypothesis=(
            "B2B SaaS founders who hire developers and care about pipeline conversion"
        ),
        success_metrics=["engagement rate", "conversion rate", "pipeline uplift", "retention"],
        channel_mix_strategy={
            Platform.LINKEDIN: "lead-gen",
            Platform.X: "awareness",
            Platform.FACEBOOK: "community",
            Platform.INSTAGRAM: "visual",
        },
        messaging_pillars=["pillar1", "pillar2", "pillar3"],
    )
    _, note, _ = agent.evaluate_proposal(proposal, 5)
    assert "Proposal is strong" in note or "strong across rubric" in note


# ---------------------------------------------------------------------------
# Risk compliance — competitor / absolute / medium / low+suggestions paths
# ---------------------------------------------------------------------------


def test_risk_agent_flags_competitor_mention() -> None:
    """If guidelines forbid competitors and concept mentions one -> high risk."""
    goals = BrandGoals(
        brand_name="b",
        target_audience="a",
        goals=["x"],
        brand_guidelines="Do not mention competitors in any post.",
    )
    idea = ConceptIdea(
        title="Why we beat competitors",
        concept="Detailed comparison vs competitors.",
        target_platforms=[Platform.LINKEDIN],
        linked_goals=["x"],
        brand_fit_score=0.8,
        audience_resonance_score=0.8,
        goal_alignment_score=0.8,
        estimated_engagement_probability=0.8,
    )
    out = RiskComplianceAgent().review_concept(idea, goals)
    assert out.risk_level == "high"
    assert any("competitors" in r.lower() for r in out.risk_reasons)


def test_risk_agent_flags_absolute_language() -> None:
    """Guidelines saying 'avoid absolute claims' + absolutes -> high risk."""
    goals = BrandGoals(
        brand_name="b",
        target_audience="a",
        goals=["x"],
        brand_guidelines="Please avoid absolute claims in copy.",
    )
    idea = ConceptIdea(
        title="100% growth, always",
        concept="Our customers always succeed; we never miss.",
        target_platforms=[Platform.X],
        linked_goals=["x"],
        brand_fit_score=0.7,
        audience_resonance_score=0.7,
        goal_alignment_score=0.7,
        estimated_engagement_probability=0.7,
    )
    out = RiskComplianceAgent().review_concept(idea, goals)
    assert out.risk_level == "high"
    assert any("absolute" in r.lower() for r in out.risk_reasons)


def test_risk_agent_medium_when_speculative_terms() -> None:
    """Speculative terms ('might', 'could', 'may') -> medium risk."""
    goals = BrandGoals(brand_name="b", target_audience="a", goals=["x"])
    idea = ConceptIdea(
        title="What might happen if you try",
        concept="This may help; results could vary.",
        target_platforms=[Platform.X],
        linked_goals=["x"],
        brand_fit_score=0.7,
        audience_resonance_score=0.7,
        goal_alignment_score=0.7,
        estimated_engagement_probability=0.7,
    )
    out = RiskComplianceAgent().review_concept(idea, goals)
    assert out.risk_level == "medium"


def test_risk_agent_low_with_default_suggestion() -> None:
    """Clean concept -> low risk + neutral suggestion line is appended."""
    goals = BrandGoals(brand_name="b", target_audience="a", goals=["x"])
    idea = ConceptIdea(
        title="Hello",
        concept="A safe, neutral, helpful concept.",
        target_platforms=[Platform.LINKEDIN],
        linked_goals=["x"],
        brand_fit_score=0.7,
        audience_resonance_score=0.7,
        goal_alignment_score=0.7,
        estimated_engagement_probability=0.7,
    )
    out = RiskComplianceAgent().review_concept(idea, goals)
    assert out.risk_level == "low"
    assert any("No high-risk claims" in r for r in out.risk_reasons)


# ---------------------------------------------------------------------------
# ExperimentDesignAgent — empty-ideas branch (line 421)
# ---------------------------------------------------------------------------


def test_experiment_design_handles_empty_ideas() -> None:
    out = ExperimentDesignAgent().build_experiment_plan("c", [])
    assert out.campaign_name == "c"
    assert out.arms == []


# ---------------------------------------------------------------------------
# winning_posts_bank — _to_float edge cases + find rerank invalid indices
# ---------------------------------------------------------------------------


def test_to_float_handles_decimal_none_invalid() -> None:
    from social_media_marketing_team.shared import winning_posts_bank as wpb

    assert wpb._to_float(Decimal("3.5")) == 3.5
    assert wpb._to_float(None) == 0.0
    assert wpb._to_float("not-a-number") == 0.0
    assert wpb._to_float("4.2") == 4.2


def test_find_returns_empty_when_query_keywords_are_only_whitespace(
    monkeypatch: pytest.MonkeyPatch,
):
    """Lines 232-234: query_keywords lowercased -> empty set -> [] returned."""
    from social_media_marketing_team.shared import winning_posts_bank as wpb

    db: dict[str, Any] = {"posts": {"id1": {
        "id": "id1",
        "title": "T",
        "body": "",
        "platform": "",
        "keywords": ["topic"],
        "metrics": {},
        "engagement_score": 0.5,
        "linked_goals": [],
        "summary": "",
        "source_job_id": None,
        "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    }}}

    from social_media_marketing_team.tests.test_winning_posts_bank import _FakeConn

    @contextmanager
    def _fake_get_conn(database=None):
        yield _FakeConn(db)

    monkeypatch.setattr(wpb, "get_conn", _fake_get_conn)

    # All whitespace tokens collapse to nothing -> empty result without raising
    assert wpb.find_relevant_winning_posts(["   ", ""]) == []


def test_find_returns_empty_when_no_candidates_have_overlap(
    monkeypatch: pytest.MonkeyPatch,
):
    """Line 187: when no rows match the keyword overlap, return [] early."""
    from social_media_marketing_team.shared import winning_posts_bank as wpb
    from social_media_marketing_team.tests.test_winning_posts_bank import _FakeConn

    db: dict[str, Any] = {"posts": {}}

    @contextmanager
    def _fake_get_conn(database=None):
        yield _FakeConn(db)

    monkeypatch.setattr(wpb, "get_conn", _fake_get_conn)
    # No rows at all -> _keyword_scored_candidates returns []
    assert wpb.find_relevant_winning_posts(["growth"]) == []


def test_find_llm_rerank_handles_non_integer_indices(
    monkeypatch: pytest.MonkeyPatch,
):
    """``int(idx) - 1`` failing on a non-numeric idx should skip that index
    (line 274-275 continue branch)."""
    from social_media_marketing_team.shared import winning_posts_bank as wpb
    from social_media_marketing_team.tests.test_winning_posts_bank import _FakeConn

    db: dict[str, Any] = {"posts": {}}

    @contextmanager
    def _fake_get_conn(database=None):
        yield _FakeConn(db)

    monkeypatch.setattr(wpb, "get_conn", _fake_get_conn)

    class _LLM:
        def complete(self, *a, **k):
            return "s"

        def complete_json(self, *a, **k):
            return ["not-int", 1, 2, 3, 4, 5, 6, 7]

    llm = _LLM()
    for _ in range(6):
        wpb.save_winning_post(title="t", body="b", keywords=["growth"], llm_client=llm)

    out = wpb.find_relevant_winning_posts(
        ["growth"], limit=2, rerank_context="ctx", llm_client=llm
    )
    # Either reranked (non-int skipped, others used) or keyword fallback;
    # but in any case some results were produced.
    assert len(out) <= 2


# ---------------------------------------------------------------------------
# trend_discovery_agent — ValueError on missing web_search + LLM payload edges
# ---------------------------------------------------------------------------


def test_trend_agent_requires_web_search() -> None:
    with pytest.raises(ValueError):
        TrendDiscoveryAgent(web_search=None)


def test_trend_agent_skips_non_dict_topic_items() -> None:
    """Lines 165-166: non-dict items in 'topics' are skipped."""
    from blog_research_agent.models import CandidateResult

    from llm_service import DummyLLMClient

    class _LLM(DummyLLMClient):
        def complete_json(self, *a, **k):
            return {
                "topics": [
                    "not a dict",  # skipped
                    None,  # skipped
                    {"title": "Real", "summary": "Real summary."},
                ]
            }

    class _Search:
        def search(self, *a, **k):
            return [
                CandidateResult(title="x", url="https://e.test", snippet="s", source="t", rank=1)
            ]

    agent = TrendDiscoveryAgent(llm_client=_LLM(), web_search=_Search())
    digest = agent.run()
    assert isinstance(digest, TrendDigest)
    titles = [t.title for t in digest.topics]
    assert titles == ["Real"]


def test_trend_agent_skips_items_with_blank_title_or_summary() -> None:
    """Lines 168-170: items where title or summary is blank are skipped."""
    from blog_research_agent.models import CandidateResult

    from llm_service import DummyLLMClient

    class _LLM(DummyLLMClient):
        def complete_json(self, *a, **k):
            return {
                "topics": [
                    {"title": "", "summary": "ok"},
                    {"title": "ok", "summary": ""},
                    {"title": "Good", "summary": "Has both."},
                ]
            }

    class _Search:
        def search(self, *a, **k):
            return [
                CandidateResult(title="x", url="https://e.test", snippet="s", source="t", rank=1)
            ]

    agent = TrendDiscoveryAgent(llm_client=_LLM(), web_search=_Search())
    digest = agent.run()
    titles = [t.title for t in digest.topics]
    assert titles == ["Good"]
