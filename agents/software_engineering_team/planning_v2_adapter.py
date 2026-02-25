"""
Adapter: maps PlanningV2WorkflowResult to inputs expected by Tech Lead and Architecture.

Used by the software engineering orchestrator after planning_v2_team.run_workflow()
to produce ProductRequirements, project_overview dict, and optional open_questions/assumptions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from shared.models import PlanningHierarchy, ProductRequirements

logger = logging.getLogger(__name__)


@dataclass
class PlanningV2AdapterResult:
    """Result of adapting PlanningV2WorkflowResult for Tech Lead and Architecture."""

    requirements: ProductRequirements
    project_overview: Dict[str, Any]
    open_questions: List[str]
    assumptions: List[str]
    hierarchy: Optional[PlanningHierarchy] = field(default=None)


def adapt_planning_v2_result(
    result: Any,
    spec_title: str = "Project",
) -> PlanningV2AdapterResult:
    """
    Map PlanningV2WorkflowResult to ProductRequirements, project_overview, open_questions, assumptions.

    Args:
        result: PlanningV2WorkflowResult from PlanningV2TeamLead.run_workflow().
        spec_title: Optional title for the requirements (e.g. from initial spec).

    Returns:
        PlanningV2AdapterResult with requirements, project_overview, open_questions, assumptions.

    Raises:
        ValueError: If result.success is False or required phase results are missing.
    """
    if not getattr(result, "success", False):
        reason = getattr(result, "failure_reason", None) or "Planning-v2 workflow did not complete successfully."
        raise ValueError(reason)

    spec_review = getattr(result, "spec_review_result", None)
    planning = getattr(result, "planning_result", None)

    # Build description and acceptance_criteria from available data
    description_parts: List[str] = []
    acceptance_criteria: List[str] = []

    if spec_review:
        if spec_review.summary:
            description_parts.append(spec_review.summary)
        if spec_review.system_design_notes:
            description_parts.append("System design: " + spec_review.system_design_notes)
        if spec_review.architecture_notes:
            description_parts.append("Architecture: " + spec_review.architecture_notes)
        # Use gaps as constraints or acceptance criteria
        for g in spec_review.gaps or []:
            acceptance_criteria.append(f"Address gap: {g}")

    if planning:
        if planning.high_level_plan:
            description_parts.append(planning.high_level_plan)
        elif planning.summary:
            description_parts.append(planning.summary)
        for u in planning.user_stories or []:
            acceptance_criteria.append(u)
        # If we still have no acceptance criteria, use milestones as high-level criteria
        if not acceptance_criteria and planning.milestones:
            acceptance_criteria.extend(planning.milestones)

    description = "\n\n".join(description_parts) if description_parts else "See planning-v2 artifacts."
    if not acceptance_criteria:
        acceptance_criteria = ["Deliver according to spec and planning artifacts."]

    requirements = ProductRequirements(
        title=spec_title or "Project",
        description=description,
        acceptance_criteria=acceptance_criteria,
        constraints=[],
        priority="medium",
        metadata={},
    )

    # project_overview dict for TechLeadInput and ArchitectureInput
    features_doc_parts: List[str] = []
    if planning:
        if planning.high_level_plan:
            features_doc_parts.append(planning.high_level_plan)
        if planning.milestones:
            features_doc_parts.append("\n## Milestones\n" + "\n".join(f"- {m}" for m in planning.milestones))
        if planning.user_stories:
            features_doc_parts.append("\n## User stories\n" + "\n".join(f"- {u}" for u in planning.user_stories))

    features_and_functionality_doc = "\n".join(features_doc_parts) if features_doc_parts else ""

    project_overview: Dict[str, Any] = {
        "features_and_functionality_doc": features_and_functionality_doc,
        "goals": getattr(planning, "summary", "") if planning else "",
    }

    open_questions: List[str] = list(spec_review.open_questions) if spec_review and spec_review.open_questions else []
    # Derive minimal assumptions if we have spec review gaps (assumptions we're making)
    assumptions: List[str] = []
    if spec_review and spec_review.gaps:
        assumptions.append("Gaps identified in spec review will be addressed during implementation.")

    # Extract the planning hierarchy from the result
    hierarchy: Optional[PlanningHierarchy] = getattr(result, "hierarchy", None)
    # Also check planning_result.hierarchy as fallback
    if not hierarchy and planning:
        hierarchy = getattr(planning, "hierarchy", None)

    return PlanningV2AdapterResult(
        requirements=requirements,
        project_overview=project_overview,
        open_questions=open_questions,
        assumptions=assumptions,
        hierarchy=hierarchy,
    )
