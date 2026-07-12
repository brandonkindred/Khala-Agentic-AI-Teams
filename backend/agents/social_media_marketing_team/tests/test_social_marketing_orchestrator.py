import pytest

from social_media_marketing_team import (
    BrandGoals,
    CampaignStatus,
    HumanReview,
    SocialMediaMarketingOrchestrator,
)
from social_media_marketing_team.models import (
    CampaignPerformanceSnapshot,
    CampaignProposal,
    ContentPlan,
    MetricDefinition,
    Platform,
    PostPerformanceObservation,
)

from .conftest import make_goals


def _goals() -> BrandGoals:
    return make_goals(
        brand_name="Northstar Labs",
        target_audience="startup founders and marketing operators",
        goals=["engagement", "follower growth", "qualified leads"],
    )


def test_requires_human_approval_before_testing() -> None:
    orchestrator = SocialMediaMarketingOrchestrator()

    result = orchestrator.run(
        goals=_goals(),
        human_review=HumanReview(approved=False, feedback="Need stronger CTA details."),
    )

    assert result.status == CampaignStatus.NEEDS_REVISION
    assert result.content_plan is None
    assert not result.platform_execution_plans
    assert "Need stronger CTA details" in (result.human_feedback or "")


def test_approved_campaign_generates_cadence_platform_plans_and_experiment_plan() -> None:
    orchestrator = SocialMediaMarketingOrchestrator()

    goals = _goals()
    result = orchestrator.run(
        goals=goals,
        human_review=HumanReview(approved=True, feedback="Approved."),
    )

    # Required posts = cadence per day x campaign duration (derived, not hard-coded).
    expected_posts = goals.cadence_posts_per_day * goals.duration_days
    assert result.status == CampaignStatus.APPROVED_FOR_TESTING
    assert result.content_plan is not None
    assert result.content_plan.total_required_posts == expected_posts
    assert len(result.content_plan.approved_ideas) == expected_posts
    assert all(
        i.estimated_engagement_probability >= 0.70 for i in result.content_plan.approved_ideas
    )
    assert all(i.risk_level != "high" for i in result.content_plan.approved_ideas)
    assert all(i.linked_goals for i in result.content_plan.approved_ideas)

    platforms = {plan.platform.value for plan in result.platform_execution_plans}
    assert platforms == {"linkedin", "facebook", "instagram", "x"}

    assert result.proposal.consensus_score >= 0.75
    assert any("Consensus reached" in msg for msg in result.proposal.communication_log)
    assert result.experiment_plan is not None
    assert result.experiment_plan.arms


def test_performance_calibration_is_reflected_in_output() -> None:
    orchestrator = SocialMediaMarketingOrchestrator()
    performance = CampaignPerformanceSnapshot(
        campaign_name="Northstar Labs multi-platform growth sprint",
        observations=[
            PostPerformanceObservation(
                campaign_name="Northstar Labs multi-platform growth sprint",
                platform=Platform.LINKEDIN,
                concept_title="Practical education spotlight #1",
                posted_at="2026-01-01T00:00:00Z",
                metrics=[MetricDefinition(name="engagement_rate", value=0.90)],
            )
        ],
    )

    result = orchestrator.run(
        goals=_goals(),
        human_review=HumanReview(approved=True),
        performance=performance,
    )
    assert result.ingested_performance


def _proposal() -> CampaignProposal:
    return CampaignProposal(campaign_name="c", objective="o", audience_hypothesis="a")


def test_assemble_team_output_requires_content_plan_when_approved() -> None:
    """DbC: an approved output must carry a content plan (caller-bug precondition)."""
    orchestrator = SocialMediaMarketingOrchestrator()
    with pytest.raises(ValueError, match="content_plan is required"):
        orchestrator.assemble_team_output(_proposal(), HumanReview(approved=True))


def test_assemble_team_output_requires_experiment_plan_when_approved() -> None:
    """DbC: an approved output must carry an experiment plan."""
    orchestrator = SocialMediaMarketingOrchestrator()
    content = ContentPlan(
        campaign_name="c", cadence_posts_per_day=1, duration_days=1, total_required_posts=1
    )
    with pytest.raises(ValueError, match="experiment_plan is required"):
        orchestrator.assemble_team_output(
            _proposal(), HumanReview(approved=True), content_plan=content
        )


def test_assemble_team_output_not_approved_ignores_missing_artifacts() -> None:
    """The not-approved path produces NEEDS_REVISION without requiring artifacts."""
    orchestrator = SocialMediaMarketingOrchestrator()
    out = orchestrator.assemble_team_output(_proposal(), HumanReview(approved=False))
    assert out.status == CampaignStatus.NEEDS_REVISION
    assert out.content_plan is None


def test_build_platform_plans_rejects_negative_num_ideas() -> None:
    """DbC: num_ideas must be >= 0."""
    orchestrator = SocialMediaMarketingOrchestrator()
    with pytest.raises(ValueError, match="num_ideas must be >= 0"):
        orchestrator.build_platform_plans(_goals(), "campaign", -1)
