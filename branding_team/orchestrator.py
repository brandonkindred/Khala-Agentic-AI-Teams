"""Orchestrator for the branding strategy team."""

from __future__ import annotations

from typing import List

from .agents import (
    BrandCodificationAgent,
    BrandComplianceAgent,
    BrandGuidelinesAgent,
    BrandWikiAgent,
    CreativeRefinementAgent,
    MoodBoardIdeationAgent,
)
from .models import BrandCheckRequest, BrandingMission, HumanReview, TeamOutput, WorkflowStatus


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
    ) -> TeamOutput:
        codification = self.codifier.codify(mission)
        mood_boards = self.moodboard.ideate(mission)
        refinement_plan = self.refinement.build_plan()
        writing_guidelines = self.guidelines.writing_guidelines(mission)
        brand_guidelines = self.guidelines.brand_guidelines(codification)
        design_system = self.guidelines.design_system()
        wiki_backlog = self.wiki.build_wiki_backlog(mission)
        checks = self.compliance.evaluate(brand_checks or [], mission)

        if not human_review.approved:
            return TeamOutput(
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
            )

        return TeamOutput(
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
        )
