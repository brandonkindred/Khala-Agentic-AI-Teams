"""Agent implementations for social media marketing workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from .models import (
    BrandGoals,
    CampaignProposal,
    ConceptIdea,
    ExperimentArm,
    ExperimentPlan,
    Platform,
    PlatformExecutionPlan,
)


@dataclass
class PlatformSpecialistAgent:
    """Specialist for one social network platform."""

    platform: Platform

    def create_execution_plan(self, goals: BrandGoals, campaign_name: str, ideas_count: int) -> PlatformExecutionPlan:
        if self.platform == Platform.LINKEDIN:
            guidelines = [
                "Lead with a business pain point in the first two lines.",
                "Use short paragraphs and one clear CTA.",
                "Anchor claims with practical examples or mini case studies.",
            ]
            kpis = ["comments", "profile visits", "qualified inbound messages"]
        elif self.platform == Platform.FACEBOOK:
            guidelines = [
                "Use community-oriented framing and relatable storytelling.",
                "Pair each post with a visual and one direct question.",
                "Optimize copy for mobile-first scanning.",
            ]
            kpis = ["shares", "comments", "link clicks"]
        elif self.platform == Platform.INSTAGRAM:
            guidelines = [
                "Use a strong visual hook and concise, skimmable captions.",
                "Prioritize carousel- and reels-friendly concepts.",
                "Close captions with lightweight engagement prompts.",
            ]
            kpis = ["saves", "reel plays", "follows"]
        else:
            guidelines = [
                "Lead with a concise opinion or insight in < 240 characters.",
                "Use threads for nuanced ideas and quote-post interaction.",
                "Tie posts to timely conversations when relevant.",
            ]
            kpis = ["reposts", "replies", "follower growth"]

        schedule = [
            f"Day {day}: {goals.cadence_posts_per_day} posts derived from approved concept pool"
            for day in range(1, min(8, goals.duration_days + 1))
        ]
        schedule.append(f"Week-1 coverage target: {ideas_count} approved concepts adapted for {self.platform.value}.")

        return PlatformExecutionPlan(
            platform=self.platform,
            posting_guidelines=guidelines,
            first_week_schedule=schedule,
            kpi_focus=kpis,
        )


@dataclass
class CampaignCollaborationAgent:
    """A planning specialist who contributes to campaign proposal quality."""

    role: str

    def evaluate_proposal(self, proposal: CampaignProposal, round_number: int) -> tuple[float, str, Dict[str, float]]:
        measurability = min(1.0, 0.45 + (len(proposal.success_metrics) * 0.09))
        audience_specificity = 0.85 if "will engage" in proposal.audience_hypothesis.lower() else 0.65
        platform_differentiation = min(1.0, 0.5 + (len(proposal.channel_mix_strategy) * 0.125))
        traceability = min(1.0, 0.45 + (len(proposal.messaging_pillars) * 0.11))

        round_lift = min(0.2, 0.05 * round_number)
        rubric = {
            "measurability": min(1.0, measurability + round_lift),
            "audience_specificity": min(1.0, audience_specificity + round_lift),
            "platform_differentiation": min(1.0, platform_differentiation + round_lift),
            "traceability": min(1.0, traceability + round_lift),
        }
        score = sum(rubric.values()) / len(rubric)
        note = (
            f"{self.role} review round {round_number}: score={score:.2f}. "
            f"Rubric={rubric}. Recommendation: strengthen weak rubric dimensions before execution."
        )
        return score, note, rubric


@dataclass
class ContentConceptAgent:
    """Generates candidate post concepts before final filtering."""

    role: str

    def generate_candidates(self, proposal: CampaignProposal, goals: BrandGoals) -> List[ConceptIdea]:
        base_topics = proposal.messaging_pillars or ["Educational insight", "Proof point", "Actionable tip"]
        linked_goals = goals.goals or ["engagement"]
        ideas: List[ConceptIdea] = []
        for idx, topic in enumerate(base_topics, start=1):
            ideas.append(
                ConceptIdea(
                    title=f"{topic} spotlight #{idx}",
                    concept=f"Explain {topic.lower()} with a practical example and CTA linked to {proposal.objective}.",
                    target_platforms=[Platform.LINKEDIN, Platform.FACEBOOK, Platform.INSTAGRAM, Platform.X],
                    linked_goals=[linked_goals[(idx - 1) % len(linked_goals)]],
                    brand_fit_score=min(0.95, 0.72 + (idx * 0.03)),
                    audience_resonance_score=min(0.93, 0.70 + (idx * 0.025)),
                    goal_alignment_score=min(0.95, 0.71 + (idx * 0.03)),
                    estimated_engagement_probability=min(0.92, 0.69 + (idx * 0.03)),
                )
            )
        return ideas


@dataclass
class ExperimentDesignAgent:
    """Designs experiment arms for campaign testing."""

    role: str = "Experiment Design Lead"

    def build_experiment_plan(self, campaign_name: str, ideas: List[ConceptIdea]) -> ExperimentPlan:
        if not ideas:
            return ExperimentPlan(campaign_name=campaign_name, arms=[])

        control = ideas[0]
        arms = [
            ExperimentArm(
                name=control.title,
                arm_type="control",
                hypothesis="Baseline framing establishes benchmark engagement.",
                success_criteria=["Engagement rate >= baseline", "Comments quality >= baseline"],
            )
        ]

        for idea in ideas[1:4]:
            arms.append(
                ExperimentArm(
                    name=idea.title,
                    arm_type="variant",
                    hypothesis=f"Variant improves engagement for goal(s): {', '.join(idea.linked_goals)}",
                    success_criteria=["Engagement uplift >= 10% vs control", "Follow rate uplift >= 5%"],
                )
            )

        return ExperimentPlan(campaign_name=campaign_name, arms=arms)


@dataclass
class RiskComplianceAgent:
    """Reviews concepts for risk and compliance issues."""

    role: str = "Brand & Compliance Reviewer"

    def review_concept(self, idea: ConceptIdea, goals: BrandGoals) -> ConceptIdea:
        lowered = f"{idea.title} {idea.concept}".lower()
        risk_reasons: List[str] = []
        risk_level = "low"

        banned_terms = ["guarantee", "guaranteed", "instant", "overnight", "no risk"]
        for term in banned_terms:
            if term in lowered:
                risk_reasons.append(f"Contains risky claim term: {term}")

        if goals.brand_guidelines and "do not mention" in goals.brand_guidelines.lower() and "mention" in lowered:
            risk_reasons.append("Potential guideline violation phrase detected")

        if risk_reasons:
            risk_level = "high"
        elif "might" in lowered:
            risk_level = "medium"

        return idea.model_copy(update={"risk_level": risk_level, "risk_reasons": risk_reasons})
