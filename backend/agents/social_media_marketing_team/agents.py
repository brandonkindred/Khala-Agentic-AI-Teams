"""Agent implementations for social media marketing workflows."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

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
class _PlatformConfig:
    """Per-platform guidelines, KPIs, and schedule templates."""

    guideline_templates: List[str]
    kpis_lead: List[str]
    kpis_engagement: List[str]
    schedule_suffix: str


_PLATFORM_CONFIGS: Dict[Platform, _PlatformConfig] = {
    Platform.LINKEDIN: _PlatformConfig(
        guideline_templates=[
            "Write in a {voice} voice tailored to {audience}.",
            "Lead with a sharp business pain point or outcome in the first two lines.",
            "Use short paragraphs, scannable formatting, and one clear CTA per post.",
        ],
        kpis_lead=["qualified inbound messages", "demo or meeting requests", "profile visits"],
        kpis_engagement=["comments", "reactions", "profile visits"],
        schedule_suffix=(
            "(thought-leadership post, tactical carousel, and comment strategy) "
            "mapped to priority messaging pillars."
        ),
    ),
    Platform.FACEBOOK: _PlatformConfig(
        guideline_templates=[
            "Use community-oriented framing and relatable storytelling for {audience}.",
            "Pair each post with a strong visual and one direct question to spark replies.",
            "Optimize copy for mobile-first scanning with short paragraphs and emojis used sparingly.",
        ],
        kpis_lead=["link clicks", "outbound site sessions", "lead form starts"],
        kpis_engagement=["shares", "comments", "time on post"],
        schedule_suffix="(story-led post plus at least one discussion prompt) that invites comments and shares.",
    ),
    Platform.INSTAGRAM: _PlatformConfig(
        guideline_templates=[
            "Use a strong visual hook aligned with {voice}.",
            "Keep captions concise, skimmable, and front-load the value in the first sentence.",
            "Prioritize carousel- and reels-friendly concepts with clear narrative arcs.",
        ],
        kpis_lead=["profile visits", "link-in-bio taps", "DM replies"],
        kpis_engagement=["saves", "reel plays", "follows"],
        schedule_suffix=(
            "(mix of carousels, reels, and stories) sourced from the approved concept pool, "
            "including at least one experimental creative angle."
        ),
    ),
    Platform.X: _PlatformConfig(
        guideline_templates=[
            "Lead with a concise opinion or insight in < 240 characters, using a {voice} tone.",
            "Use threads for nuanced ideas and quote-post interaction.",
            "Tie posts to timely conversations or trends when relevant to the brand.",
        ],
        kpis_lead=["link clicks", "profile visits", "high-intent replies"],
        kpis_engagement=["reposts", "replies", "follower growth"],
        schedule_suffix="(short posts or threads) that test at least one strong hook and one follow-up insight.",
    ),
}


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
        """Build a platform execution plan aware of brand goals, audience, and tone."""
        cfg = _PLATFORM_CONFIGS[self.platform]

        objective = (goals.brand_objectives or "").lower()
        high_intent_goals = {"demo", "trial", "signup", "lead", "pipeline"}
        is_lead_focused = any(term in objective for term in high_intent_goals)

        fmt = {"voice": goals.voice_and_tone, "audience": goals.target_audience}
        guidelines = [t.format(**fmt) for t in cfg.guideline_templates]
        kpis = cfg.kpis_lead if is_lead_focused else cfg.kpis_engagement

        schedule: List[str] = []
        for day in range(1, min(8, goals.duration_days + 1)):
            schedule.append(f"Day {day}: {goals.cadence_posts_per_day} posts {cfg.schedule_suffix}")
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


_ROLE_RUBRIC_WEIGHTS: Dict[str, Dict[str, float]] = {
    "Campaign Strategist": {
        "measurability": 1.10,
        "audience_specificity": 0.95,
        "platform_differentiation": 1.08,
        "traceability": 1.0,
        "feasibility": 0.92,
    },
    "Audience Research Lead": {
        "measurability": 0.92,
        "audience_specificity": 1.15,
        "platform_differentiation": 0.95,
        "traceability": 1.0,
        "feasibility": 1.0,
    },
    "Performance Marketing Analyst": {
        "measurability": 1.15,
        "audience_specificity": 0.92,
        "platform_differentiation": 0.95,
        "traceability": 1.08,
        "feasibility": 0.95,
    },
}

_DEFAULT_RUBRIC_WEIGHTS: Dict[str, float] = {
    "measurability": 1.0,
    "audience_specificity": 1.0,
    "platform_differentiation": 1.0,
    "traceability": 1.0,
    "feasibility": 1.0,
}


@dataclass
class CampaignCollaborationAgent:
    """A planning specialist who contributes to campaign proposal quality.

    Each role applies different rubric weights reflecting their area of expertise:
    - Campaign Strategist: emphasises measurability and platform differentiation
    - Audience Research Lead: emphasises audience specificity
    - Performance Marketing Analyst: emphasises measurability and traceability
    """

    role: str

    def evaluate_proposal(
        self, proposal: CampaignProposal, round_number: int
    ) -> tuple[float, str, Dict[str, float]]:
        """Provide a role-weighted rubric for proposal quality."""
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
        raw_rubric = {
            "measurability": min(1.0, measurability + round_lift),
            "audience_specificity": min(1.0, audience_specificity + round_lift),
            "platform_differentiation": min(1.0, platform_differentiation + round_lift),
            "traceability": min(1.0, traceability + round_lift),
            "feasibility": min(1.0, feasibility + round_lift),
        }

        # Apply role-specific weights so each agent emphasises their expertise.
        weights = _ROLE_RUBRIC_WEIGHTS.get(self.role, _DEFAULT_RUBRIC_WEIGHTS)
        rubric = {dim: min(1.0, raw_rubric[dim] * weights.get(dim, 1.0)) for dim in raw_rubric}
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


_STORYTELLING_ARCHETYPES = [
    ("Customer story", "Tell a short story showing how someone overcame"),
    ("Behind-the-scenes", "Reveal behind-the-scenes context that demystifies"),
    ("Brand origin", "Share the founding insight or pivotal moment behind"),
    ("Community spotlight", "Highlight a community member or partner experience with"),
]

_CREATIVE_TESTING_ARCHETYPES = [
    ("Educational framework", "Share a simple framework or checklist related to"),
    ("Contrarian take", "Offer a surprising or counter-intuitive perspective on"),
    ("Data snapshot", "Visualise a compelling data point or trend around"),
    ("Rapid-fire tips", "Deliver 3 actionable tips in under 60 seconds about"),
]


def _format_exemplar_context(exemplars: Optional[List[Dict[str, Any]]]) -> str:
    """Format Winning Posts Bank exemplars for the ``prior_winners_context`` field.

    Returns an empty string when no exemplars are supplied. Output is
    stored on its own ``ConceptIdea`` field — never concatenated into
    ``concept`` — so risk and routing scanners cannot misread terms in
    exemplar text as belonging to the new concept.
    """
    if not exemplars:
        return ""
    blocks: List[str] = []
    for e in exemplars:
        platform = str(e.get("platform") or "unknown")
        score = float(e.get("engagement_score") or 0.0)
        title = str(e.get("title") or "")
        body = str(e.get("body") or "")
        blocks.append(
            f"[Winning post on {platform} — engagement {score:.2f}]\n{title}\n{body}".rstrip()
        )
    return "Prior winners (reference, do not copy):\n" + "\n\n".join(blocks)


@dataclass
class ContentConceptAgent:
    """Generates candidate post concepts before final filtering.

    The archetype set and scoring biases differ by role:
    - Brand Storytelling Lead: narrative archetypes, higher brand-fit scores for stories
    - Creative Testing Lead: experimental archetypes, higher resonance for data-driven content
    """

    role: str

    def _archetypes(self) -> List[tuple[str, str]]:
        if self.role == "Brand Storytelling Lead":
            return _STORYTELLING_ARCHETYPES
        if self.role == "Creative Testing Lead":
            return _CREATIVE_TESTING_ARCHETYPES
        # Fallback: combined set (deduped by name)
        return _STORYTELLING_ARCHETYPES + _CREATIVE_TESTING_ARCHETYPES

    def generate_candidates(
        self,
        proposal: CampaignProposal,
        goals: BrandGoals,
        exemplars: Optional[List[Dict[str, Any]]] = None,
    ) -> List[ConceptIdea]:
        """Generate a diverse, platform-aware set of candidate concepts.

        When *exemplars* are provided (from the Winning Posts Bank),
        the formatted reference block is stored on the
        ``prior_winners_context`` field. Crucially, it is NOT mixed
        into ``concept`` so downstream scanners (risk/compliance,
        routing) only see the agent's own copy.
        """
        base_topics = proposal.messaging_pillars or [
            "Educational insight",
            "Proof point",
            "Actionable tip",
        ]
        linked_goals = goals.goals or ["engagement"]
        archetypes = self._archetypes()

        exemplar_context = _format_exemplar_context(exemplars)

        ideas: List[ConceptIdea] = []
        idx = 0
        for topic in base_topics:
            for archetype_name, archetype_prompt in archetypes:
                idx += 1

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
                if self.role == "Brand Storytelling Lead" and "story" in archetype_name.lower():
                    brand_fit_score += 0.04

                audience_resonance_score = 0.7
                if any(
                    word in proposal.audience_hypothesis.lower()
                    for word in ("pain", "challenge", "struggle")
                ):
                    audience_resonance_score += 0.05
                if "story" in archetype_name.lower():
                    audience_resonance_score += 0.03
                if self.role == "Creative Testing Lead" and archetype_name in (
                    "Data snapshot",
                    "Contrarian take",
                ):
                    audience_resonance_score += 0.04

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
                        prior_winners_context=exemplar_context,
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


def _word_boundary_pattern(term: str) -> str:
    """Build a regex that matches *term* as a whole token.

    ``\\b`` only works between word (``\\w``) and non-word characters.  For
    terms that end with non-word characters (e.g. ``100%``) the trailing
    ``\\b`` silently fails.  This helper uses lookarounds that work
    regardless of character class at the edges.
    """
    escaped = re.escape(term)
    return rf"(?<!\w){escaped}(?!\w)"


@dataclass
class RiskComplianceAgent:
    """Reviews concepts for risk and compliance issues."""

    role: str = "Brand & Compliance Reviewer"

    def review_concept(self, idea: ConceptIdea, goals: BrandGoals) -> ConceptIdea:
        """
        Review a concept for risk and compliance, returning an updated idea with
        risk level and structured reasons.
        """
        text = f"{idea.title} {idea.concept}"
        lowered = text.lower()
        risk_reasons: List[str] = []
        risk_level = "low"

        banned_terms = ["guarantee", "guaranteed", "instant", "overnight", "no risk"]
        for term in banned_terms:
            if re.search(_word_boundary_pattern(term), lowered):
                risk_reasons.append(f"Contains risky claim term: {term} (overclaim)")

        if goals.brand_guidelines:
            guidelines_lower = goals.brand_guidelines.lower()
            if "do not mention competitors" in guidelines_lower and re.search(
                r"(?<!\w)competitors?(?!\w)", lowered
            ):
                risk_reasons.append(
                    "Mentions competitors despite guidelines (brand_guideline_violation)"
                )
            if "avoid absolute claims" in guidelines_lower and any(
                re.search(_word_boundary_pattern(t), lowered) for t in ("never", "always", "100%")
            ):
                risk_reasons.append("Uses absolute language discouraged by guidelines (regulatory)")

        if risk_reasons:
            risk_level = "high"
        elif any(re.search(_word_boundary_pattern(t), lowered) for t in ("might", "could", "may")):
            risk_level = "medium"

        if risk_level == "low" and not risk_reasons:
            risk_reasons.append(
                "No high-risk claims detected; concept aligns with cautious, compliant positioning."
            )

        return idea.model_copy(update={"risk_level": risk_level, "risk_reasons": risk_reasons})
