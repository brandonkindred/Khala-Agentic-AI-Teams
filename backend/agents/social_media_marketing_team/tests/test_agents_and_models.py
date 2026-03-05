import pytest
from pydantic import ValidationError

from social_media_marketing_team.agents import (
    CampaignCollaborationAgent,
    ContentConceptAgent,
    ExperimentDesignAgent,
    PlatformSpecialistAgent,
    RiskComplianceAgent,
)
from social_media_marketing_team.models import (
    BrandGoals,
    CampaignProposal,
    ConceptIdea,
    Platform,
)


def _goals() -> BrandGoals:
    return BrandGoals(
        brand_name="Acme",
        target_audience="B2B marketers",
        goals=["engagement", "follower growth"],
        cadence_posts_per_day=3,
        duration_days=5,
    )


def _proposal() -> CampaignProposal:
    return CampaignProposal(
        campaign_name="Acme test",
        objective="Grow engagement",
        audience_hypothesis="Audience will engage with practical content",
        messaging_pillars=["Practical education", "Proof"],
        channel_mix_strategy={
            Platform.LINKEDIN: "leadership",
            Platform.FACEBOOK: "community",
            Platform.INSTAGRAM: "visual",
            Platform.X: "timely",
        },
        success_metrics=["engagement", "followers"],
    )


def test_platform_specialist_branches() -> None:
    goals = _goals()
    for platform in (Platform.LINKEDIN, Platform.FACEBOOK, Platform.INSTAGRAM, Platform.X):
        plan = PlatformSpecialistAgent(platform).create_execution_plan(goals, "Campaign", ideas_count=10)
        assert plan.platform == platform
        assert len(plan.posting_guidelines) == 3
        assert len(plan.kpi_focus) == 3
        assert plan.first_week_schedule[-1].startswith("Week-1 coverage target")


def test_collaboration_scoring_returns_rubric() -> None:
    proposal = _proposal()
    agent = CampaignCollaborationAgent(role="Strategist")

    early_score, early_note, early_rubric = agent.evaluate_proposal(proposal, round_number=1)
    late_score, late_note, late_rubric = agent.evaluate_proposal(proposal, round_number=4)

    assert "Strategist review round 1" in early_note
    assert "Rubric" in late_note
    assert late_score >= early_score
    assert all(0 <= v <= 1 for v in early_rubric.values())
    assert all(0 <= v <= 1 for v in late_rubric.values())


def test_content_concept_generation_has_goal_links_and_platforms() -> None:
    goals = _goals()
    proposal = _proposal()

    ideas = ContentConceptAgent(role="Creative").generate_candidates(proposal, goals)
    assert ideas
    assert all(isinstance(idea, ConceptIdea) for idea in ideas)
    assert all(idea.linked_goals for idea in ideas)
    assert all(idea.estimated_engagement_probability >= 0.70 for idea in ideas)
    assert all(set(idea.target_platforms) == {Platform.LINKEDIN, Platform.FACEBOOK, Platform.INSTAGRAM, Platform.X} for idea in ideas)


def test_experiment_design_agent_creates_control_and_variants() -> None:
    ideas = ContentConceptAgent(role="Creative").generate_candidates(_proposal(), _goals())
    plan = ExperimentDesignAgent().build_experiment_plan("Acme test", ideas)

    assert plan.arms
    assert plan.arms[0].arm_type == "control"
    assert any(arm.arm_type == "variant" for arm in plan.arms)


def test_risk_compliance_agent_flags_risky_claims() -> None:
    goals = _goals()
    risky = ConceptIdea(
        title="Guaranteed overnight growth",
        concept="This will guarantee results overnight",
        target_platforms=[Platform.X],
        linked_goals=["engagement"],
        brand_fit_score=0.8,
        audience_resonance_score=0.8,
        goal_alignment_score=0.8,
        estimated_engagement_probability=0.8,
    )
    reviewed = RiskComplianceAgent().review_concept(risky, goals)
    assert reviewed.risk_level == "high"
    assert reviewed.risk_reasons


def test_concept_validation_rejects_out_of_range_probability() -> None:
    with pytest.raises(ValidationError):
        ConceptIdea(
            title="Invalid",
            concept="Should fail",
            target_platforms=[Platform.LINKEDIN],
            linked_goals=["engagement"],
            brand_fit_score=0.8,
            audience_resonance_score=0.8,
            goal_alignment_score=0.8,
            estimated_engagement_probability=1.1,
        )
