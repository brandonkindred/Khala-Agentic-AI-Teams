from social_media_marketing_team.agents import ContentConceptAgent
from social_media_marketing_team.models import (
    BrandGoals,
    CampaignPerformanceSnapshot,
    CampaignProposal,
    CampaignStatus,
    ConceptIdea,
    HumanReview,
    MetricDefinition,
    Platform,
    PostPerformanceObservation,
)
from social_media_marketing_team.orchestrator import SocialMediaMarketingOrchestrator

from .conftest import make_goals


def _goals() -> BrandGoals:
    return make_goals(cadence_posts_per_day=1, duration_days=3)


def test_default_feedback_messages() -> None:
    orchestrator = SocialMediaMarketingOrchestrator()
    goals = _goals()

    rejected = orchestrator.run(goals=goals, human_review=HumanReview(approved=False))
    assert rejected.status == CampaignStatus.NEEDS_REVISION
    assert rejected.human_feedback == "Human requested revisions before testing."

    approved = orchestrator.run(goals=goals, human_review=HumanReview(approved=True))
    assert approved.status == CampaignStatus.APPROVED_FOR_TESTING
    assert approved.human_feedback == "Approved for campaign testing."


def test_reach_consensus_runs_multiple_rounds_and_logs_refinement() -> None:
    orchestrator = SocialMediaMarketingOrchestrator()
    proposal = orchestrator._build_initial_proposal(_goals())

    result = orchestrator._reach_consensus(proposal)

    assert result.consensus_score >= orchestrator.CONSENSUS_THRESHOLD
    assert any("Consensus not reached yet" in msg for msg in result.communication_log)
    # Consensus may be reached naturally or after hitting the max-rounds guard.
    assert any(
        "Consensus reached" in msg or "Max collaboration rounds" in msg
        for msg in result.communication_log
    )


def test_plan_content_handles_no_initially_approved_ideas() -> None:
    orchestrator = SocialMediaMarketingOrchestrator()
    goals = _goals()

    class LowConfidenceAgent(ContentConceptAgent):
        def generate_candidates(
            self, proposal: CampaignProposal, goals: BrandGoals, exemplars=None
        ):
            return [
                ConceptIdea(
                    title="Low confidence",
                    concept="Needs more proof",
                    target_platforms=[Platform.X],
                    linked_goals=["engagement"],
                    brand_fit_score=0.6,
                    audience_resonance_score=0.6,
                    goal_alignment_score=0.6,
                    estimated_engagement_probability=0.5,
                )
            ]

    orchestrator.concept_team = [LowConfidenceAgent("LowConfidence")]
    proposal = orchestrator._build_initial_proposal(goals)

    content_plan = orchestrator._plan_content(proposal, goals)

    assert content_plan.total_required_posts == 3
    assert len(content_plan.approved_ideas) == 3
    assert all(
        idea.estimated_engagement_probability >= 0.70 for idea in content_plan.approved_ideas
    )


def test_goal_traceability_filter_excludes_invalid_goal_links() -> None:
    orchestrator = SocialMediaMarketingOrchestrator()
    goals = _goals()
    ideas = [
        ConceptIdea(
            title="Good",
            concept="Good",
            target_platforms=[Platform.LINKEDIN],
            linked_goals=["engagement"],
            brand_fit_score=0.8,
            audience_resonance_score=0.8,
            goal_alignment_score=0.8,
            estimated_engagement_probability=0.8,
        ),
        ConceptIdea(
            title="Bad",
            concept="Bad",
            target_platforms=[Platform.LINKEDIN],
            linked_goals=["unknown"],
            brand_fit_score=0.8,
            audience_resonance_score=0.8,
            goal_alignment_score=0.8,
            estimated_engagement_probability=0.8,
        ),
    ]

    filtered = orchestrator._goal_traceability_filter(ideas, goals)
    assert len(filtered) == 1
    assert filtered[0].title == "Good"


def test_calibration_changes_probabilities_when_engagement_observations_exist() -> None:
    idea = ConceptIdea(
        title="A",
        concept="B",
        target_platforms=[Platform.X],
        linked_goals=["engagement"],
        brand_fit_score=0.8,
        audience_resonance_score=0.8,
        goal_alignment_score=0.8,
        estimated_engagement_probability=0.7,
    )
    perf = CampaignPerformanceSnapshot(
        campaign_name="x",
        observations=[
            PostPerformanceObservation(
                campaign_name="x",
                platform=Platform.X,
                concept_title="A",
                posted_at="2026-01-01T00:00:00Z",
                metrics=[MetricDefinition(name="engagement_rate", value=0.9)],
            )
        ],
    )
    calibrated = SocialMediaMarketingOrchestrator._calibrate_probabilities([idea], perf)
    assert calibrated[0].estimated_engagement_probability > idea.estimated_engagement_probability


def test_build_initial_proposal_includes_brand_context_and_model() -> None:
    orchestrator = SocialMediaMarketingOrchestrator(llm_model_name="llama3.1")
    goals = BrandGoals(
        brand_name="DocBrand",
        target_audience="professionals",
        goals=["engagement"],
        brand_guidelines="Always include clear CTA",
        brand_objectives="Increase comments by 20%",
        messaging_pillars=["Developer empowerment", "Simplicity"],
        brand_story="DocBrand was founded to simplify documentation.",
        tagline="Docs done right",
    )

    proposal = orchestrator._build_initial_proposal(goals)

    assert "Ground strategy in brand objectives" in proposal.objective
    assert proposal.messaging_pillars == ["Developer empowerment", "Simplicity"]
    assert any("Brand tagline" in msg for msg in proposal.communication_log)
    assert any("Brand story context injected" in msg for msg in proposal.communication_log)
    assert any("Guideline context injected" in msg for msg in proposal.communication_log)
    assert any("Objective context injected" in msg for msg in proposal.communication_log)
    assert any("Configured LLM model" in msg for msg in proposal.communication_log)
