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

    def create_execution_plan(
        self,
        goals: BrandGoals,
        campaign_name: str,
        ideas_count: int,
    ) -> PlatformExecutionPlan:
        """
        Build a platform execution plan that is aware of brand goals, audience,
        and tone, not just the raw platform choice.
        """
        objective = (goals.brand_objectives or "").lower()
        high_intent_goals = {"demo", "trial", "signup", "lead", "pipeline"}
        is_lead_or_conversion_focused = any(term in objective for term in high_intent_goals)

        if self.platform == Platform.LINKEDIN:
            guidelines = [
                f"Write in a {goals.voice_and_tone} voice tailored to {goals.target_audience}.",
                "Lead with a sharp business pain point or outcome in the first two lines.",
                "Use short paragraphs, scannable formatting, and one clear CTA per post.",
            ]
            kpis = (
                ["qualified inbound messages", "demo or meeting requests", "profile visits"]
                if is_lead_or_conversion_focused
                else ["comments", "reactions", "profile visits"]
            )
        elif self.platform == Platform.FACEBOOK:
            guidelines = [
                f"Use community-oriented framing and relatable storytelling for {goals.target_audience}.",
                "Pair each post with a strong visual and one direct question to spark replies.",
                "Optimize copy for mobile-first scanning with short paragraphs and emojis used sparingly.",
            ]
            kpis = (
                ["link clicks", "outbound site sessions", "lead form starts"]
                if is_lead_or_conversion_focused
                else ["shares", "comments", "time on post"]
            )
        elif self.platform == Platform.INSTAGRAM:
            guidelines = [
                f"Use a strong visual hook aligned with {goals.voice_and_tone}.",
                "Keep captions concise, skimmable, and front-load the value in the first sentence.",
                "Prioritize carousel- and reels-friendly concepts with clear narrative arcs.",
            ]
            kpis = (
                ["profile visits", "link-in-bio taps", "DM replies"]
                if is_lead_or_conversion_focused
                else ["saves", "reel plays", "follows"]
            )
        else:
            guidelines = [
                f"Lead with a concise opinion or insight in < 240 characters, using a {goals.voice_and_tone} tone.",
                "Use threads for nuanced ideas and quote-post interaction.",
                "Tie posts to timely conversations or trends when relevant to the brand.",
            ]
            kpis = (
                ["link clicks", "profile visits", "high-intent replies"]
                if is_lead_or_conversion_focused
                else ["reposts", "replies", "follower growth"]
            )

        schedule: List[str] = []
        for day in range(1, min(8, goals.duration_days + 1)):
            base = f"Day {day}: {goals.cadence_posts_per_day} posts"
            if self.platform == Platform.INSTAGRAM:
                detail = (
                    f"{base} (mix of carousels, reels, and stories) sourced from the approved concept pool, "
                    "including at least one experimental creative angle."
                )
            elif self.platform == Platform.LINKEDIN:
                detail = (
                    f"{base} (thought-leadership post, tactical carousel, and comment strategy) "
                    "mapped to priority messaging pillars."
                )
            elif self.platform == Platform.FACEBOOK:
                detail = f"{base} (story-led post plus at least one discussion prompt) that invites comments and shares."
            else:
                detail = f"{base} (short posts or threads) that test at least one strong hook and one follow-up insight."
            schedule.append(detail)

        schedule.append(
            f"Week-1 coverage target: adapt at least {ideas_count} approved concepts for {self.platform.value}, "
            "ensuring every goal in BrandGoals.goals is represented across the content mix."
        )

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

    def evaluate_proposal(
        self, proposal: CampaignProposal, round_number: int
    ) -> tuple[float, str, Dict[str, float]]:
        """Provide a richer, more actionable rubric for proposal quality."""
        objective_lower = proposal.objective.lower()
        audience_lower = proposal.audience_hypothesis.lower()

        measurability = min(1.0, 0.35 + (len(proposal.success_metrics) * 0.08))
        if any(
            term in " ".join(proposal.success_metrics).lower()
            for term in ("rate", "uplift", "conversion")
        ):
            measurability += 0.1

        audience_specificity = 0.55
        if (
            any(term in audience_lower for term in ("for", "who", "that"))
            and len(audience_lower.split()) > 8
        ):
            audience_specificity += 0.2
        if any(
            term in audience_lower for term in ("job", "role", "founder", "marketer", "developer")
        ):
            audience_specificity += 0.1

        platform_differentiation = min(1.0, 0.4 + (len(proposal.channel_mix_strategy) * 0.12))
        if len({desc for desc in proposal.channel_mix_strategy.values()}) > 1:
            platform_differentiation += 0.1

        traceability = min(1.0, 0.35 + (len(proposal.messaging_pillars) * 0.10))
        if any(
            goal.lower() in objective_lower
            for goal in ("engagement", "follower", "leads", "pipeline")
        ):
            traceability += 0.05

        feasibility = 0.6
        if len(proposal.messaging_pillars) <= 5 and len(proposal.channel_mix_strategy) <= 4:
            feasibility += 0.1

        round_lift = min(0.2, 0.04 * round_number)
        rubric = {
            "measurability": min(1.0, measurability + round_lift),
            "audience_specificity": min(1.0, audience_specificity + round_lift),
            "platform_differentiation": min(1.0, platform_differentiation + round_lift),
            "traceability": min(1.0, traceability + round_lift),
            "feasibility": min(1.0, feasibility + round_lift),
        }
        score = sum(rubric.values()) / len(rubric)

        weak_dimensions = [name for name, value in rubric.items() if value < 0.75]
        suggestions: List[str] = []

        if "measurability" in weak_dimensions:
            suggestions.append(
                "Clarify success_metrics with at least one leading indicator and one conversion or pipeline metric."
            )
        if "audience_specificity" in weak_dimensions:
            suggestions.append(
                "Tighten audience_hypothesis by naming a specific role, segment, and primary pain point."
            )
        if "platform_differentiation" in weak_dimensions:
            suggestions.append(
                "Differentiate channel_mix_strategy so each platform focuses on a distinct content angle or role."
            )
        if "traceability" in weak_dimensions:
            suggestions.append(
                "Ensure each messaging_pillar maps clearly to one or more items in goals and success_metrics."
            )
        if "feasibility" in weak_dimensions:
            suggestions.append(
                "Reduce the number of simultaneous pillars or platforms, or specify phasing to keep execution realistic."
            )

        if not suggestions:
            suggestions.append(
                "Proposal is strong across rubric dimensions; focus next rounds on sharper test hypotheses."
            )

        note = (
            f"{self.role} review round {round_number}: score={score:.2f}. "
            f"Rubric={rubric}. Suggestions: " + " ".join(suggestions)
        )
        return score, note, rubric


@dataclass
class ContentConceptAgent:
    """Generates candidate post concepts before final filtering."""

    role: str

    def generate_candidates(
        self, proposal: CampaignProposal, goals: BrandGoals
    ) -> List[ConceptIdea]:
        """
        Generate a diverse, platform-aware set of candidate concepts.
        """
        base_topics = proposal.messaging_pillars or [
            "Educational insight",
            "Proof point",
            "Actionable tip",
        ]
        linked_goals = goals.goals or ["engagement"]

        archetypes = [
            ("Educational framework", "Share a simple framework or checklist related to"),
            ("Customer story", "Tell a short story showing how someone overcame"),
            ("Contrarian take", "Offer a surprising or counter-intuitive perspective on"),
            ("Behind-the-scenes", "Reveal behind-the-scenes context that demystifies"),
        ]

        ideas: List[ConceptIdea] = []
        idx = 0
        for topic in base_topics:
            for archetype_name, archetype_prompt in archetypes:
                idx += 1

                # Decide primary platform mix based on archetype
                target_platforms = [
                    Platform.LINKEDIN,
                    Platform.FACEBOOK,
                    Platform.INSTAGRAM,
                    Platform.X,
                ]
                content_format = "thread" if archetype_name == "Contrarian take" else "carousel"

                goal = linked_goals[(idx - 1) % len(linked_goals)]
                primary_hook = f"{archetype_name}: {topic}"
                suggested_visual = (
                    "Carousel of key steps"
                    if content_format == "carousel"
                    else "Short reel with overlay text"
                )
                cta_variant = (
                    f"Invite {goals.target_audience} to take one concrete step tied to {goal}."
                )

                brand_fit_score = 0.7
                if goals.voice_and_tone:
                    brand_fit_score += 0.05
                if "behind-the-scenes" in topic.lower():
                    brand_fit_score += 0.03

                audience_resonance_score = 0.7
                if any(
                    word in proposal.audience_hypothesis.lower()
                    for word in ("pain", "challenge", "struggle")
                ):
                    audience_resonance_score += 0.05
                if "story" in archetype_name.lower():
                    audience_resonance_score += 0.03

                goal_alignment_score = 0.7
                if goal.lower() in proposal.objective.lower():
                    goal_alignment_score += 0.08

                estimated_engagement_probability = min(
                    0.92,
                    (brand_fit_score + audience_resonance_score + goal_alignment_score) / 3.0,
                )

                ideas.append(
                    ConceptIdea(
                        title=f"{topic} – {archetype_name}",
                        concept=(
                            f"{archetype_prompt} {topic.lower()} for {goals.target_audience}. "
                            f"Close with a CTA tied to {goal} and the campaign objective: {proposal.objective}."
                        ),
                        target_platforms=target_platforms,
                        linked_goals=[goal],
                        primary_hook=primary_hook,
                        suggested_visual=suggested_visual,
                        content_format=content_format,
                        cta_variant=cta_variant,
                        brand_fit_score=min(1.0, brand_fit_score),
                        audience_resonance_score=min(1.0, audience_resonance_score),
                        goal_alignment_score=min(1.0, goal_alignment_score),
                        estimated_engagement_probability=min(1.0, estimated_engagement_probability),
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

        # Choose control as a strong, on-brand baseline
        control = max(
            ideas,
            key=lambda i: (
                i.brand_fit_score + i.goal_alignment_score,
                i.estimated_engagement_probability,
            ),
        )
        arms = [
            ExperimentArm(
                name=control.title,
                arm_type="control",
                hypothesis="Baseline framing establishes benchmark engagement and follower growth.",
                success_criteria=[
                    "Engagement rate >= historical baseline for this audience",
                    "Comment quality and intent remain on-brand",
                ],
            )
        ]

        # Select up to three variants that differ meaningfully from control
        variants: List[ConceptIdea] = []
        for idea in ideas:
            if idea.title == control.title:
                continue
            if len(variants) >= 3:
                break
            variants.append(idea)

        for idea in variants:
            goals_str = ", ".join(idea.linked_goals) if idea.linked_goals else "engagement"
            angle = idea.content_format or "creative framing"
            arms.append(
                ExperimentArm(
                    name=idea.title,
                    arm_type="variant",
                    hypothesis=(
                        f"Changing the {angle} increases outcomes for goal(s): {goals_str} "
                        f"relative to the control concept '{control.title}'."
                    ),
                    success_criteria=[
                        "Engagement uplift >= 10% vs control on primary engagement metric",
                        "Follow rate or qualified click-through uplift >= 5% vs control",
                    ],
                )
            )

        return ExperimentPlan(campaign_name=campaign_name, arms=arms)


@dataclass
class RiskComplianceAgent:
    """Reviews concepts for risk and compliance issues."""

    role: str = "Brand & Compliance Reviewer"

    def review_concept(self, idea: ConceptIdea, goals: BrandGoals) -> ConceptIdea:
        """
        Review a concept for risk and compliance, returning an updated idea with
        risk level and structured reasons.
        """
        lowered = f"{idea.title} {idea.concept}".lower()
        risk_reasons: List[str] = []
        risk_level = "low"

        banned_terms = ["guarantee", "guaranteed", "instant", "overnight", "no risk"]
        for term in banned_terms:
            if term in lowered:
                risk_reasons.append(f"Contains risky claim term: {term} (overclaim)")

        if goals.brand_guidelines:
            guidelines_lower = goals.brand_guidelines.lower()
            if "do not mention competitors" in guidelines_lower and "competitor" in lowered:
                risk_reasons.append(
                    "Mentions competitors despite guidelines (brand_guideline_violation)"
                )
            if "avoid absolute claims" in guidelines_lower and any(
                t in lowered for t in ("never", "always", "100%")
            ):
                risk_reasons.append("Uses absolute language discouraged by guidelines (regulatory)")

        if risk_reasons:
            risk_level = "high"
        elif any(term in lowered for term in ("might", "could", "may")):
            risk_level = "medium"

        if risk_level == "low" and not risk_reasons:
            risk_reasons.append(
                "No high-risk claims detected; concept aligns with cautious, compliant positioning."
            )

        return idea.model_copy(update={"risk_level": risk_level, "risk_reasons": risk_reasons})
