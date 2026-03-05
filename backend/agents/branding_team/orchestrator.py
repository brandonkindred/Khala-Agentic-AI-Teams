"""Orchestrator for the branding strategy team."""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

from .agents import (
    BrandCodificationAgent,
    BrandComplianceAgent,
    BrandGuidelinesAgent,
    BrandWikiAgent,
    CreativeRefinementAgent,
    MoodBoardIdeationAgent,
)
from .models import (
    BrandBook,
    BrandCheckRequest,
    BrandingMission,
    HumanReview,
    TeamOutput,
    WorkflowStatus,
)

if TYPE_CHECKING:
    from .store import BrandingStore


class BrandingTeamOrchestrator:
    """Coordinates codification, ideation, systems, and brand compliance workflows."""

    def __init__(self) -> None:
        self.codifier = BrandCodificationAgent()
        self.moodboard = MoodBoardIdeationAgent()
        self.refinement = CreativeRefinementAgent()
        self.guidelines = BrandGuidelinesAgent()
        self.wiki = BrandWikiAgent()
        self.compliance = BrandComplianceAgent()

    def run(
        self,
        mission: BrandingMission,
        human_review: HumanReview,
        brand_checks: List[BrandCheckRequest] | None = None,
        store: Optional[BrandingStore] = None,
        client_id: Optional[str] = None,
        brand_id: Optional[str] = None,
        include_market_research: bool = False,
        include_design_assets: bool = False,
    ) -> TeamOutput:
        resolved_client_id: Optional[str] = client_id
        if store and brand_id:
            if client_id:
                brand = store.get_brand(client_id, brand_id)
            else:
                brand = None
                for c in store.list_clients():
                    brand = store.get_brand(c.id, brand_id)
                    if brand is not None:
                        resolved_client_id = c.id
                        break
            if brand is not None:
                mission = brand.mission
                if resolved_client_id is None:
                    resolved_client_id = brand.client_id
        else:
            resolved_client_id = client_id

        codification = self.codifier.codify(mission)
        mood_boards = self.moodboard.ideate(mission)
        refinement_plan = self.refinement.build_plan()
        writing_guidelines = self.guidelines.writing_guidelines(mission)
        brand_guidelines = self.guidelines.brand_guidelines(codification)
        design_system = self.guidelines.design_system()
        wiki_backlog = self.wiki.build_wiki_backlog(mission)
        checks = self.compliance.evaluate(brand_checks or [], mission)

        competitive_snapshot = None
        if include_market_research:
            try:
                from .adapters.market_research import request_market_research
                competitive_snapshot = request_market_research(mission)
            except Exception:
                competitive_snapshot = None

        design_asset_result = None
        if include_design_assets:
            from .adapters.design_assets import request_design_assets
            design_asset_result = request_design_assets(codification, mission.company_name)

        brand_book = _build_brand_book(codification, writing_guidelines, brand_guidelines, design_system)

        if not human_review.approved:
            output = TeamOutput(
                status=WorkflowStatus.NEEDS_HUMAN_DECISION,
                mission_summary=(
                    "Brand codification, mood boards, and governance artifacts are ready for stakeholder "
                    "review before enterprise rollout."
                ),
                codification=codification,
                mood_boards=mood_boards,
                creative_refinement=refinement_plan,
                writing_guidelines=writing_guidelines,
                brand_guidelines=brand_guidelines,
                design_system=design_system,
                wiki_backlog=wiki_backlog,
                brand_checks=checks,
                human_feedback=human_review.feedback or "Awaiting approval from brand leadership.",
                competitive_snapshot=competitive_snapshot,
                design_asset_result=design_asset_result,
                brand_book=brand_book,
            )
        else:
            output = TeamOutput(
                status=WorkflowStatus.READY_FOR_ROLLOUT,
                mission_summary=(
                    "Branding team finalized codification, systems, and wiki operations and is ready to support "
                    "enterprise-wide on-brand delivery."
                ),
                codification=codification,
                mood_boards=mood_boards,
                creative_refinement=refinement_plan,
                writing_guidelines=writing_guidelines,
                brand_guidelines=brand_guidelines,
                design_system=design_system,
                wiki_backlog=wiki_backlog,
                brand_checks=checks,
                human_feedback=human_review.feedback or "Approved for rollout.",
                competitive_snapshot=competitive_snapshot,
                design_asset_result=design_asset_result,
                brand_book=brand_book,
            )

        if store and brand_id and resolved_client_id:
            store.append_brand_version(resolved_client_id, brand_id, output)

        return output


def _build_brand_book(
    codification,
    writing_guidelines,
    brand_guidelines: List[str],
    design_system,
) -> BrandBook:
    """Build consolidated brand document from codification, guidelines, and design system."""
    sections = [
        f"# Positioning\n{codification.positioning_statement}",
        f"# Brand Promise\n{codification.brand_promise}",
        "# Narrative Pillars\n" + "\n".join(f"- {p}" for p in codification.narrative_pillars),
        "# Voice Principles\n" + "\n".join(f"- {v}" for v in writing_guidelines.voice_principles),
        "# Brand Guidelines\n" + "\n".join(f"- {g}" for g in brand_guidelines),
        "# Design System Principles\n" + "\n".join(f"- {p}" for p in design_system.design_principles),
    ]
    content = "\n\n".join(sections)
    return BrandBook(
        content=content,
        sections={
            "positioning": codification.positioning_statement,
            "brand_promise": codification.brand_promise,
            "narrative_pillars": codification.narrative_pillars,
            "voice_principles": writing_guidelines.voice_principles,
            "design_principles": design_system.design_principles,
        },
    )
