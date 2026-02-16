"""Orchestrator for a collaborative social media marketing team."""

from __future__ import annotations

from typing import Dict, List

from .agents import (
    CampaignCollaborationAgent,
    ContentConceptAgent,
    ExperimentDesignAgent,
    PlatformSpecialistAgent,
    RiskComplianceAgent,
)
from .models import (
    BrandGoals,
    CampaignPerformanceSnapshot,
    CampaignProposal,
    CampaignStatus,
    ConceptIdea,
    ContentPlan,
    HumanReview,
    Platform,
    TeamOutput,
)


class SocialMediaMarketingOrchestrator:
    """
    Coordinates a specialist team that:
    1) Collaborates on campaign proposal until consensus
    2) Waits for human approval before testing/planning
    3) Generates and filters concept ideas to >=70% engagement likelihood
    4) Dispatches approved ideas to platform specialists for execution planning
    """

    CONSENSUS_THRESHOLD = 0.75
    MIN_COLLAB_ROUNDS = 2
    RUBRIC_MINIMUM = 0.7

    def __init__(self, llm_model_name: str = "") -> None:
        self.llm_model_name = llm_model_name
        self.platform_specialists = [
            PlatformSpecialistAgent(Platform.LINKEDIN),
            PlatformSpecialistAgent(Platform.FACEBOOK),
            PlatformSpecialistAgent(Platform.INSTAGRAM),
            PlatformSpecialistAgent(Platform.X),
        ]
        self.collaboration_team = [
            CampaignCollaborationAgent("Campaign Strategist"),
            CampaignCollaborationAgent("Audience Research Lead"),
            CampaignCollaborationAgent("Performance Marketing Analyst"),
        ]
        self.concept_team = [
            ContentConceptAgent("Brand Storytelling Lead"),
            ContentConceptAgent("Creative Testing Lead"),
        ]
        self.experiment_designer = ExperimentDesignAgent()
        self.risk_reviewer = RiskComplianceAgent()

    def _build_initial_proposal(self, goals: BrandGoals) -> CampaignProposal:
        objective = "Validate repeatable content themes that improve engagement and follower growth."
        if goals.brand_objectives:
            objective = f"{objective} Ground strategy in brand objectives: {goals.brand_objectives[:400]}"

        proposal = CampaignProposal(
            campaign_name=f"{goals.brand_name} multi-platform growth sprint",
            objective=objective,
            audience_hypothesis=(
                f"{goals.target_audience} will engage most with clear, tactical content mapped to {goals.goals}."
            ),
            messaging_pillars=[
                "Practical education",
                "Customer outcomes",
                "Behind-the-scenes brand credibility",
                "Community conversation starters",
            ],
            channel_mix_strategy={
                Platform.LINKEDIN: "Thought leadership with professional proof points.",
                Platform.FACEBOOK: "Community-first stories and discussion prompts.",
                Platform.INSTAGRAM: "Visual narratives and quick, actionable takeaways.",
                Platform.X: "Timely commentary and concise insight threads.",
            },
            success_metrics=[
                "Engagement rate by platform",
                "Net follower growth",
                "Comment quality and intent",
                "Profile or site click-through rate",
            ],
            experiment_notes="Start with 14-day test window and compare baseline vs campaign uplift.",
        )

        if goals.brand_guidelines_path:
            proposal.communication_log.append(
                f"Using brand guidelines document at path: {goals.brand_guidelines_path}"
            )
        if goals.brand_objectives_path:
            proposal.communication_log.append(
                f"Using brand objectives document at path: {goals.brand_objectives_path}"
            )
        if goals.brand_guidelines:
            proposal.communication_log.append(
                f"Guideline context injected ({len(goals.brand_guidelines)} chars)."
            )
        if goals.brand_objectives:
            proposal.communication_log.append(
                f"Objective context injected ({len(goals.brand_objectives)} chars)."
            )
        if self.llm_model_name:
            proposal.communication_log.append(f"Configured LLM model: {self.llm_model_name}")
        return proposal

    def _rubric_passes(self, rubric: Dict[str, float]) -> bool:
        return all(score >= self.RUBRIC_MINIMUM for score in rubric.values())

    def _reach_consensus(self, proposal: CampaignProposal) -> CampaignProposal:
        round_number = 0
        while True:
            round_number += 1
            scores: List[float] = []
            rubric_results: List[bool] = []
            for collaborator in self.collaboration_team:
                score, note, rubric = collaborator.evaluate_proposal(proposal, round_number)
                scores.append(score)
                rubric_results.append(self._rubric_passes(rubric))
                proposal.communication_log.append(note)

            proposal.consensus_score = sum(scores) / len(scores)
            proposal.communication_log.append(
                f"Orchestrator round {round_number}: average consensus score {proposal.consensus_score:.2f}."
            )

            if (
                round_number >= self.MIN_COLLAB_ROUNDS
                and all(score >= self.CONSENSUS_THRESHOLD for score in scores)
                and all(rubric_results)
            ):
                proposal.communication_log.append("Consensus reached: proposal is thorough enough for test-content planning.")
                return proposal

            proposal.communication_log.append("Consensus not reached yet: refining objective and metrics for next round.")
            proposal.success_metrics.append(f"Refined metric checkpoint round {round_number}")

    @staticmethod
    def _calibrate_probabilities(ideas: List[ConceptIdea], performance: CampaignPerformanceSnapshot | None) -> List[ConceptIdea]:
        if not performance or not performance.observations:
            return ideas

        metric_values: List[float] = []
        for obs in performance.observations:
            for metric in obs.metrics:
                if metric.name in {"engagement_rate", "engagement", "engagement_score"}:
                    metric_values.append(metric.value)
        if not metric_values:
            return ideas

        avg = sum(metric_values) / len(metric_values)
        # Normalize around 0.70 baseline probability
        delta = (avg - 0.70) * 0.25
        calibrated: List[ConceptIdea] = []
        for idea in ideas:
            p = max(0.0, min(1.0, idea.estimated_engagement_probability + delta))
            calibrated.append(idea.model_copy(update={"estimated_engagement_probability": p}))
        return calibrated

    @staticmethod
    def _goal_traceability_filter(ideas: List[ConceptIdea], goals: BrandGoals) -> List[ConceptIdea]:
        valid_goals = set(goals.goals or ["engagement"])
        filtered: List[ConceptIdea] = []
        for idea in ideas:
            if idea.linked_goals and set(idea.linked_goals).issubset(valid_goals):
                filtered.append(idea)
        return filtered

    def _plan_content(
        self,
        proposal: CampaignProposal,
        goals: BrandGoals,
        performance: CampaignPerformanceSnapshot | None = None,
    ) -> ContentPlan:
        required_posts = goals.cadence_posts_per_day * goals.duration_days

        candidates: List[ConceptIdea] = []
        for agent in self.concept_team:
            candidates.extend(agent.generate_candidates(proposal, goals))

        candidates = self._goal_traceability_filter(candidates, goals)
        candidates = self._calibrate_probabilities(candidates, performance)

        risk_reviewed: List[ConceptIdea] = [self.risk_reviewer.review_concept(idea, goals) for idea in candidates]
        approved = [
            idea for idea in risk_reviewed
            if idea.estimated_engagement_probability >= 0.70 and idea.risk_level != "high"
        ]

        idx = 1
        while len(approved) < required_posts:
            seed = approved[(idx - 1) % len(approved)] if approved else risk_reviewed[(idx - 1) % len(risk_reviewed)]
            clone = seed.model_copy(deep=True)
            clone.title = f"{seed.title} variant {idx}"
            clone.concept = f"{seed.concept} Variant angle {idx} tuned for daypart testing."
            clone.estimated_engagement_probability = max(0.70, seed.estimated_engagement_probability - 0.01)
            clone.risk_level = "medium" if seed.risk_level == "high" else seed.risk_level
            approved.append(clone)
            idx += 1

        return ContentPlan(
            campaign_name=proposal.campaign_name,
            cadence_posts_per_day=goals.cadence_posts_per_day,
            duration_days=goals.duration_days,
            total_required_posts=required_posts,
            approved_ideas=approved[:required_posts],
        )

    def run(
        self,
        goals: BrandGoals,
        human_review: HumanReview,
        performance: CampaignPerformanceSnapshot | None = None,
    ) -> TeamOutput:
        proposal = self._reach_consensus(self._build_initial_proposal(goals))

        if not human_review.approved:
            return TeamOutput(
                status=CampaignStatus.NEEDS_REVISION,
                proposal=proposal,
                human_feedback=human_review.feedback or "Human requested revisions before testing.",
                llm_model_name=self.llm_model_name,
            )

        content_plan = self._plan_content(proposal, goals, performance)
        platform_plans = [
            specialist.create_execution_plan(goals, proposal.campaign_name, len(content_plan.approved_ideas))
            for specialist in self.platform_specialists
        ]
        experiment_plan = self.experiment_designer.build_experiment_plan(
            proposal.campaign_name,
            content_plan.approved_ideas,
        )

        return TeamOutput(
            status=CampaignStatus.APPROVED_FOR_TESTING,
            proposal=proposal,
            human_feedback=human_review.feedback or "Approved for campaign testing.",
            content_plan=content_plan,
            platform_execution_plans=platform_plans,
            llm_model_name=self.llm_model_name,
            experiment_plan=experiment_plan,
            ingested_performance=(performance.observations if performance else []),
        )
