"""
Adapter: maps PlanningV2WorkflowResult to inputs expected by Tech Lead and Architecture.

Used by the software engineering orchestrator after planning_v2_team.run_workflow()
to produce ProductRequirements, project_overview dict, and optional open_questions/assumptions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from planning_v2_team.models import PLAN_PLANNING_TEAM_DIR
from planning_v2_team.shared_planning_document import SHARED_DOC_FILENAME

from software_engineering_team.shared.models import PlanningHierarchy, ProductRequirements

logger = logging.getLogger(__name__)


@dataclass
class PlanningV2AdapterResult:
    """Result of adapting PlanningV2WorkflowResult for Tech Lead and Architecture."""

    requirements: ProductRequirements
    project_overview: Dict[str, Any]
    open_questions: List[str]
    assumptions: List[str]
    hierarchy: Optional[PlanningHierarchy] = field(default=None)
    final_spec_content: Optional[str] = field(default=None)
    architecture_overview: Optional[str] = field(default=None)
    shared_planning_doc_path: Optional[str] = field(default=None)


def adapt_planning_v2_result(
    result: Any,
    spec_title: str = "Project",
    repo_path: Optional[str] = None,
) -> PlanningV2AdapterResult:
    """
    Map PlanningV2WorkflowResult to ProductRequirements, project_overview, open_questions, assumptions.

    Args:
        result: PlanningV2WorkflowResult from PlanningV2TeamLead.run_workflow().
        spec_title: Optional title for the requirements (e.g. from initial spec).
        repo_path: Optional repo path to derive shared_planning_doc_path.

    Returns:
        PlanningV2AdapterResult with requirements, project_overview, open_questions, assumptions.

    Raises:
        ValueError: If result.success is False or required phase results are missing.
    """
    if not getattr(result, "success", False):
        reason = (
            getattr(result, "failure_reason", None)
            or "Planning-v2 workflow did not complete successfully."
        )
        raise ValueError(reason)

    spec_review = getattr(result, "spec_review_result", None)
    planning = getattr(result, "planning_result", None)

    # Build description and acceptance_criteria from available data
    description_parts: List[str] = []
    acceptance_criteria: List[str] = []

    if spec_review:
        if spec_review.summary:
            description_parts.append(spec_review.summary)
        if spec_review.plan_summary:
            description_parts.append("Plan summary: " + spec_review.plan_summary)
        for issue in spec_review.issues or []:
            acceptance_criteria.append(f"Address issue: {issue}")
        for gap in spec_review.product_gaps or []:
            acceptance_criteria.append(f"Address gap: {gap}")

    if planning:
        if planning.goals_vision:
            description_parts.append("Goals/Vision: " + planning.goals_vision)
        if planning.architecture:
            description_parts.append("Architecture: " + planning.architecture)
        elif planning.summary:
            description_parts.append(planning.summary)
        for feature in planning.key_features or []:
            acceptance_criteria.append(feature)
        if not acceptance_criteria and planning.milestones:
            acceptance_criteria.extend(planning.milestones)

    description = (
        "\n\n".join(description_parts) if description_parts else "See planning-v2 artifacts."
    )
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
        if planning.goals_vision:
            features_doc_parts.append("## Goals / Vision\n" + planning.goals_vision)
        if planning.key_features:
            features_doc_parts.append(
                "\n## Key Features\n" + "\n".join(f"- {f}" for f in planning.key_features)
            )
        if planning.milestones:
            features_doc_parts.append(
                "\n## Milestones\n" + "\n".join(f"- {m}" for m in planning.milestones)
            )
        if planning.architecture:
            features_doc_parts.append("\n## Architecture\n" + planning.architecture)

    features_and_functionality_doc = "\n".join(features_doc_parts) if features_doc_parts else ""

    project_overview: Dict[str, Any] = {
        "features_and_functionality_doc": features_and_functionality_doc,
        "goals": planning.goals_vision if planning else "",
    }

    open_questions: List[str] = (
        list(spec_review.open_questions) if spec_review and spec_review.open_questions else []
    )
    assumptions: List[str] = []
    if spec_review and (spec_review.issues or spec_review.product_gaps):
        assumptions.append(
            "Issues and product gaps identified in spec review will be addressed during implementation."
        )

    # Extract the planning hierarchy from the result
    hierarchy: Optional[PlanningHierarchy] = getattr(result, "hierarchy", None)
    # Also check planning_result.hierarchy as fallback
    if not hierarchy and planning:
        hierarchy = getattr(planning, "hierarchy", None)

    # Extract the final spec content from the result
    final_spec_content: Optional[str] = getattr(result, "final_spec_content", None)

    shared_doc_path: Optional[str] = None
    if repo_path:
        shared_doc_path = str(Path(repo_path) / PLAN_PLANNING_TEAM_DIR / SHARED_DOC_FILENAME)

    return PlanningV2AdapterResult(
        requirements=requirements,
        project_overview=project_overview,
        open_questions=open_questions,
        assumptions=assumptions,
        hierarchy=hierarchy,
        final_spec_content=final_spec_content,
        architecture_overview=None,
        shared_planning_doc_path=shared_doc_path,
    )
