"""Agent implementations for branding strategy workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .models import (
    BrandCheckRequest,
    BrandCheckResult,
    BrandCodification,
    BrandingMission,
    CreativeRefinementPlan,
    DesignSystemDefinition,
    MoodBoardConcept,
    WikiEntry,
    WritingGuidelines,
)


@dataclass
class BrandCodificationAgent:
    """Works with the user to codify a unified brand direction."""

    role: str = "Brand Strategist"

    def codify(self, mission: BrandingMission) -> BrandCodification:
        values = mission.values or ["trust", "clarity", "momentum"]
        differentiators = mission.differentiators or ["domain expertise", "execution speed"]
        return BrandCodification(
            positioning_statement=(
                f"{mission.company_name} helps {mission.target_audience} by turning "
                f"{mission.company_description.lower()} into consistent, recognizable experiences."
            ),
            brand_promise=(
                "Every customer touchpoint should feel cohesive, useful, and unmistakably aligned "
                "to one unified brand system."
            ),
            brand_personality_traits=values[:4],
            narrative_pillars=[
                f"Differentiate through {differentiators[0]}",
                "Build trust with transparent communication",
                "Show proof through concrete outcomes",
            ],
        )


@dataclass
class MoodBoardIdeationAgent:
    """Creates candidate brand-image mood boards."""

    role: str = "Brand Visual Ideation Lead"

    def ideate(self, mission: BrandingMission) -> List[MoodBoardConcept]:
        return [
            MoodBoardConcept(
                title="Modern Confidence",
                visual_direction="Clean grids, product-in-context photography, generous whitespace",
                color_story=["midnight blue", "electric cyan", "neutral stone"],
                typography_direction="Geometric sans-serif with high readability",
                image_style=["documentary-style people", "interface closeups", "subtle gradients"],
            ),
            MoodBoardConcept(
                title="Human Craft",
                visual_direction="Editorial layouts with warm contrast and tactile textures",
                color_story=["charcoal", "terracotta", "cream"],
                typography_direction="Humanist sans-serif paired with a restrained serif",
                image_style=["team collaboration scenes", "sketch-to-product narratives", "macro textures"],
            ),
        ]


@dataclass
class CreativeRefinementAgent:
    """Facilitates iterative creative refinement and decision making."""

    role: str = "Creative Director"

    def build_plan(self) -> CreativeRefinementPlan:
        return CreativeRefinementPlan(
            phases=[
                "Diverge: review 2-3 mood boards and map them to audience perception goals",
                "Converge: pick one primary direction and one fallback",
                "Stress-test: apply direction to landing page, sales deck, and social post",
                "Finalize: lock visual and narrative system in v1.0 brand standards",
            ],
            workshop_prompts=[
                "What should prospects feel in the first 5 seconds?",
                "Which direction best communicates credibility and momentum?",
                "What elements are unique enough to own long-term?",
            ],
            decision_criteria=[
                "Audience resonance",
                "Distinctiveness vs competitors",
                "Cross-channel consistency",
                "Execution feasibility in 90 days",
            ],
        )


@dataclass
class BrandGuidelinesAgent:
    """Defines writing, brand, and design-system guidelines."""

    role: str = "Brand Systems Architect"

    def writing_guidelines(self, mission: BrandingMission) -> WritingGuidelines:
        return WritingGuidelines(
            voice_principles=[
                f"Use a {mission.desired_voice} voice across channels",
                "Lead with customer outcomes before product features",
                "Prefer plain language over jargon",
            ],
            style_dos=[
                "Use active voice and direct calls to action",
                "Ground claims in proof points and examples",
                "Keep paragraphs short and scannable",
            ],
            style_donts=[
                "Do not overpromise or use unverifiable superlatives",
                "Avoid inconsistent terminology for core offerings",
                "Do not bury the key value proposition",
            ],
            editorial_quality_bar=[
                "Every artifact must map to one narrative pillar",
                "Every external asset receives tone and terminology QA",
                "Every major launch includes a message hierarchy",
            ],
        )

    def brand_guidelines(self, codification: BrandCodification) -> List[str]:
        return [
            f"Positioning: {codification.positioning_statement}",
            f"Promise: {codification.brand_promise}",
            "Identity system: logo spacing, color usage, and typography rules are mandatory.",
            "Messaging hierarchy: promise -> pillar -> proof -> CTA.",
            "Governance: route major campaign concepts through brand review before launch.",
        ]

    def design_system(self) -> DesignSystemDefinition:
        return DesignSystemDefinition(
            design_principles=[
                "Clarity over decoration",
                "Consistency at scale",
                "Accessible by default",
            ],
            foundation_tokens=[
                "Color tokens: primary/secondary/surface/critical",
                "Type tokens: display/body/caption scales",
                "Spacing tokens: 4-point base scale",
                "Motion tokens: subtle and meaningful",
            ],
            component_standards=[
                "Buttons: size variants, icon rules, and disabled states",
                "Cards: elevation, border, and content density options",
                "Navigation: desktop and mobile behavior patterns",
            ],
        )


@dataclass
class BrandWikiAgent:
    """Builds and maintains an enterprise-ready brand wiki backlog."""

    role: str = "Knowledge Systems Manager"

    def build_wiki_backlog(self, mission: BrandingMission) -> List[WikiEntry]:
        return [
            WikiEntry(
                title="Brand North Star",
                summary="Single source of truth for positioning, promise, and narrative pillars.",
                owners=["Brand Strategy", "Executive Sponsor"],
                update_cadence="quarterly",
            ),
            WikiEntry(
                title="Voice & Writing Playbook",
                summary="Examples, approved terminology, and do/don't patterns for all writers.",
                owners=["Content Design", "Comms"],
                update_cadence="monthly",
            ),
            WikiEntry(
                title="Design System & UI Guidance",
                summary="Token catalog, component rules, and accessibility requirements.",
                owners=["Design Systems", "Frontend Platform"],
                update_cadence="monthly",
            ),
            WikiEntry(
                title="Brand Review Intake",
                summary=(
                    "Request template and SLA for checking whether campaigns, pages, and artifacts "
                    "are on brand."
                ),
                owners=["Brand Operations"],
                update_cadence="bi-weekly",
            ),
        ]


@dataclass
class BrandComplianceAgent:
    """Fields requests to determine whether assets are on brand."""

    role: str = "Brand Compliance Reviewer"

    def evaluate(self, checks: List[BrandCheckRequest], mission: BrandingMission) -> List[BrandCheckResult]:
        keywords = [*mission.values, *mission.differentiators, mission.company_name, mission.target_audience]
        lowered_keywords = [k.lower() for k in keywords if k]
        results: List[BrandCheckResult] = []

        for check in checks:
            text = f"{check.asset_name} {check.asset_description}".lower()
            matched = [k for k in lowered_keywords if k in text]
            is_on_brand = len(matched) >= 2
            confidence = min(0.95, 0.45 + (0.1 * len(matched)))

            rationale = [
                "Asset aligns with declared audience and brand language." if is_on_brand else "Asset is missing core brand signals.",
                f"Detected brand cues: {', '.join(matched[:4]) or 'none'}.",
            ]
            revision_suggestions = []
            if not is_on_brand:
                revision_suggestions = [
                    "Add clearer reference to target audience and expected outcome.",
                    "Use approved voice-and-tone language from the writing playbook.",
                    "Map copy to one narrative pillar and include proof.",
                ]

            results.append(
                BrandCheckResult(
                    asset_name=check.asset_name,
                    is_on_brand=is_on_brand,
                    confidence=round(confidence, 2),
                    rationale=rationale,
                    revision_suggestions=revision_suggestions,
                )
            )

        return results
