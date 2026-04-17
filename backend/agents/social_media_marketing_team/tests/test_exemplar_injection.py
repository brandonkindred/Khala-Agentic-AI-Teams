"""Tests for Winning Posts Bank exemplar injection into ContentConceptAgent."""

from __future__ import annotations

from social_media_marketing_team.agents import (
    ContentConceptAgent,
    RiskComplianceAgent,
    _format_exemplar_context,
)
from social_media_marketing_team.models import CampaignProposal, Platform
from social_media_marketing_team.tests.conftest import make_goals


def _proposal() -> CampaignProposal:
    return CampaignProposal(
        campaign_name="Q2 growth sprint",
        objective="Drive qualified inbound",
        audience_hypothesis="Founders struggle with seed-stage GTM",
        messaging_pillars=["Pricing", "Traction"],
        channel_mix_strategy={Platform.LINKEDIN: "lead-gen"},
    )


def test_format_context_empty_returns_empty_string():
    assert _format_exemplar_context(None) == ""
    assert _format_exemplar_context([]) == ""


def test_format_context_renders_blocks():
    ctx = _format_exemplar_context(
        [
            {
                "platform": "linkedin",
                "engagement_score": 0.84,
                "title": "Why seed rounds fail",
                "body": "Founders skip...",
            }
        ]
    )
    assert "Prior winners (reference, do not copy):" in ctx
    assert "[Winning post on linkedin — engagement 0.84]" in ctx
    assert "Why seed rounds fail" in ctx


def test_generate_candidates_writes_exemplars_to_separate_field():
    agent = ContentConceptAgent("Brand Storytelling Lead")
    goals = make_goals()
    proposal = _proposal()
    exemplars = [
        {
            "platform": "linkedin",
            "engagement_score": 0.91,
            "title": "Our $100k pricing mistake",
            "body": "We almost lost 40% of customers...",
        }
    ]

    ideas = agent.generate_candidates(proposal, goals, exemplars=exemplars)
    assert ideas, "Expected at least one generated idea"
    for idea in ideas:
        # Exemplar text lives on its own field …
        assert "[Winning post on linkedin — engagement 0.91]" in idea.prior_winners_context
        assert "Our $100k pricing mistake" in idea.prior_winners_context
        # … and never leaks into the concept the agent actually wrote.
        assert "[Winning post on" not in idea.concept
        assert "Prior winners" not in idea.concept
        assert "Our $100k pricing mistake" not in idea.concept


def test_generate_candidates_without_exemplars_leaves_field_empty():
    """Regression guard: no exemplars → empty context, concept unchanged."""
    agent = ContentConceptAgent("Brand Storytelling Lead")
    goals = make_goals()
    proposal = _proposal()

    ideas_none = agent.generate_candidates(proposal, goals)
    ideas_empty = agent.generate_candidates(proposal, goals, exemplars=[])

    for idea in ideas_none + ideas_empty:
        assert idea.prior_winners_context == ""
        assert "[Winning post on" not in idea.concept
        assert "Prior winners" not in idea.concept

    assert [i.concept for i in ideas_none] == [i.concept for i in ideas_empty]


def test_generate_candidates_preserves_scoring_with_exemplars():
    agent = ContentConceptAgent("Brand Storytelling Lead")
    goals = make_goals()
    proposal = _proposal()

    without = agent.generate_candidates(proposal, goals)
    with_ex = agent.generate_candidates(
        proposal,
        goals,
        exemplars=[{"platform": "x", "engagement_score": 0.5, "title": "t", "body": "b"}],
    )

    assert len(without) == len(with_ex)
    for a, b in zip(without, with_ex):
        assert a.concept == b.concept  # exemplar must not change the agent's own copy
        assert a.brand_fit_score == b.brand_fit_score
        assert a.audience_resonance_score == b.audience_resonance_score
        assert a.goal_alignment_score == b.goal_alignment_score


def test_risk_scorer_ignores_banned_terms_inside_exemplar():
    """Critical: a winning post containing 'guarantee'/'overnight' must NOT
    cause clean new concepts to be flagged high-risk."""
    agent = ContentConceptAgent("Brand Storytelling Lead")
    goals = make_goals()
    proposal = _proposal()

    poisoned_exemplars = [
        {
            "platform": "linkedin",
            "engagement_score": 0.95,
            "title": "We guarantee overnight results",
            "body": "No risk, instant ROI for every customer.",
        }
    ]

    ideas = agent.generate_candidates(proposal, goals, exemplars=poisoned_exemplars)
    reviewer = RiskComplianceAgent()
    reviewed = [reviewer.review_concept(i, goals) for i in ideas]

    assert reviewed, "Expected at least one reviewed idea"
    for idea in reviewed:
        # The exemplar carries banned terms, but the new concept is clean.
        assert idea.risk_level == "low", (
            f"Exemplar text leaked into risk scoring: "
            f"risk_level={idea.risk_level}, reasons={idea.risk_reasons}, "
            f"concept={idea.concept!r}"
        )
        assert not any("risky claim" in r for r in idea.risk_reasons)
