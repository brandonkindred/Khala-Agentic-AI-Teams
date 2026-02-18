"""Project Planning agent: produces high-level project overview from spec."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from shared.llm import LLMClient
from shared.models import ProductRequirements

from .models import (
    EpicStoryItem,
    Milestone,
    ProjectOverview,
    ProjectPlanningInput,
    ProjectPlanningOutput,
    RiskItem,
)
from .prompts import PROJECT_PLANNING_PROMPT

logger = logging.getLogger(__name__)


class ProjectPlanningAgent:
    """
    Produces a ProjectOverview from product requirements and spec.
    Frames the engagement for fast delivery; consumed by Architecture and Tech Lead.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: ProjectPlanningInput) -> ProjectPlanningOutput:
        """Generate project overview from requirements and spec."""
        logger.info("Project Planning: starting for %s", input_data.requirements.title)
        reqs = input_data.requirements

        context_parts = [
            f"**Product Title:** {reqs.title}",
            f"**Description:** {reqs.description}",
            "**Acceptance Criteria:**",
            *[f"- {c}" for c in reqs.acceptance_criteria],
            "**Constraints:**",
            *[f"- {c}" for c in reqs.constraints],
            f"**Priority:** {reqs.priority}",
            "",
            "**Full Specification:**",
            "---",
            (input_data.spec_content or "")[:15000] + ("..." if len(input_data.spec_content or "") > 15000 else ""),
            "---",
        ]
        if input_data.repo_state_summary:
            context_parts.extend([
                "",
                "**Existing Codebase Summary:**",
                input_data.repo_state_summary[:3000] + ("..." if len(input_data.repo_state_summary) > 3000 else ""),
            ])

        prompt = PROJECT_PLANNING_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)
        data = self.llm.complete_json(prompt, temperature=0.2)

        milestones: List[Milestone] = []
        for m in data.get("milestones") or []:
            if isinstance(m, dict) and m.get("id"):
                milestones.append(Milestone(
                    id=m["id"],
                    name=m.get("name", ""),
                    description=m.get("description", ""),
                    target_order=m.get("target_order", len(milestones)),
                    scope_summary=m.get("scope_summary", ""),
                    definition_of_done=m.get("definition_of_done", ""),
                ))

        epic_story_breakdown: List[EpicStoryItem] = []
        for e in data.get("epic_story_breakdown") or []:
            if isinstance(e, dict) and e.get("id"):
                epic_story_breakdown.append(EpicStoryItem(
                    id=e["id"],
                    name=e.get("name", ""),
                    description=e.get("description", ""),
                    scope=e.get("scope", "MVP"),
                    dependencies=e.get("dependencies", []),
                ))

        nfr_list = data.get("non_functional_requirements") or []
        if not isinstance(nfr_list, list):
            nfr_list = []

        risk_items: List[RiskItem] = []
        for r in data.get("risk_items") or []:
            if isinstance(r, dict) and r.get("description"):
                risk_items.append(RiskItem(
                    description=r["description"],
                    severity=r.get("severity", "medium"),
                    mitigation=r.get("mitigation", ""),
                ))

        features_doc = (data.get("features_and_functionality") or "").strip()
        if isinstance(features_doc, list):
            features_doc = "\n".join(str(x) for x in features_doc)

        overview = ProjectOverview(
            features_and_functionality_doc=features_doc,
            primary_goal=data.get("primary_goal", ""),
            secondary_goals=data.get("secondary_goals", []),
            milestones=milestones,
            risk_items=risk_items,
            delivery_strategy=data.get("delivery_strategy", ""),
            epic_story_breakdown=epic_story_breakdown,
            scope_cut=data.get("scope_cut", ""),
            non_functional_requirements=nfr_list,
        )

        logger.info("Project Planning: done, features_doc=%s chars, %s milestones, %s risks", len(features_doc), len(milestones), len(risk_items))
        return ProjectPlanningOutput(
            overview=overview,
            summary=data.get("summary", ""),
            features_and_functionality_doc=features_doc or overview.features_and_functionality_doc,
        )
