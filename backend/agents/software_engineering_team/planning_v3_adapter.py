"""
Adapter: maps Planning V3 handoff package to inputs expected by Tech Lead and Architecture.

Used by the software engineering orchestrator after planning_v3_team.orchestrator.run_workflow()
to produce ProductRequirements, project_overview dict, and optional open_questions/assumptions.
Output type is PlanningV2AdapterResult so the rest of the pipeline (Tech Lead, Architecture)
remains unchanged.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from planning_v2_adapter import PlanningV2AdapterResult

from software_engineering_team.shared.models import ProductRequirements

logger = logging.getLogger(__name__)

__all__ = ["adapt_planning_v3_result", "PlanningV2AdapterResult"]

PRD_FALLBACK_PATH = "plan/product_analysis/product_requirements_document.md"


def _handoff_to_dict(handoff: Any) -> Dict[str, Any]:
    """Normalize handoff to dict; support Pydantic model or dict."""
    if handoff is None:
        return {}
    if hasattr(handoff, "model_dump"):
        return handoff.model_dump()
    if isinstance(handoff, dict):
        return handoff
    return {}


def _get_client_context(handoff: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract client_context dict from handoff; may be nested model."""
    ctx = handoff.get("client_context")
    if ctx is None:
        return None
    if hasattr(ctx, "model_dump"):
        return ctx.model_dump()
    if isinstance(ctx, dict):
        return ctx
    return None


def adapt_planning_v3_result(
    result: Dict[str, Any],
    spec_title: str = "Project",
    repo_path: Optional[str] = None,
) -> PlanningV2AdapterResult:
    """
    Map Planning V3 workflow result (handoff package) to PlanningV2AdapterResult.

    Args:
        result: Return value of planning_v3_team.orchestrator.run_workflow()
            (success, handoff_package, failure_reason).
        spec_title: Title for the requirements (e.g. from initial spec).
        repo_path: Optional repo path; used to read PRD from disk when handoff
            has no prd_content (e.g. when use_product_analysis=False in V3).

    Returns:
        PlanningV2AdapterResult with requirements, project_overview, open_questions,
        assumptions, hierarchy=None, final_spec_content.

    Raises:
        ValueError: If result.success is False or handoff is missing.
    """
    if not result.get("success", False):
        reason = result.get("failure_reason") or "Planning V3 workflow did not complete successfully."
        raise ValueError(reason)

    handoff_raw = result.get("handoff_package")
    handoff = _handoff_to_dict(handoff_raw)
    if not handoff and handoff_raw is not None:
        handoff = _handoff_to_dict(handoff_raw)

    validated_spec = handoff.get("validated_spec_content") or ""
    prd_content = handoff.get("prd_content")
    if not prd_content and repo_path:
        prd_path = Path(repo_path) / PRD_FALLBACK_PATH
        if prd_path.exists():
            try:
                prd_content = prd_path.read_text(encoding="utf-8")
            except Exception as e:
                logger.warning("Could not read PRD fallback at %s: %s", prd_path, e)

    client_context = _get_client_context(handoff)

    description_parts: List[str] = []
    if validated_spec:
        description_parts.append(validated_spec)
    if prd_content:
        description_parts.append(prd_content)
    description = "\n\n".join(description_parts) if description_parts else "See Planning V3 handoff artifacts."

    acceptance_criteria: List[str] = []
    if client_context and client_context.get("success_criteria"):
        acceptance_criteria = list(client_context["success_criteria"])
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

    features_doc_parts: List[str] = []
    if prd_content:
        features_doc_parts.append(prd_content)
    if client_context:
        if client_context.get("problem_summary"):
            features_doc_parts.append("## Problem summary\n" + (client_context["problem_summary"] or ""))
        if client_context.get("opportunity_statement"):
            features_doc_parts.append("## Opportunity\n" + (client_context["opportunity_statement"] or ""))
        if client_context.get("target_users"):
            features_doc_parts.append(
                "## Target users\n" + "\n".join(f"- {u}" for u in client_context["target_users"])
            )
    features_and_functionality_doc = "\n\n".join(features_doc_parts) if features_doc_parts else ""
    goals = ""
    if client_context and (client_context.get("problem_summary") or client_context.get("opportunity_statement")):
        goals = (client_context.get("problem_summary") or "") + "\n" + (client_context.get("opportunity_statement") or "")
    if not goals and handoff.get("summary"):
        goals = handoff["summary"]

    project_overview: Dict[str, Any] = {
        "features_and_functionality_doc": features_and_functionality_doc,
        "goals": goals.strip(),
    }

    open_questions: List[str] = []
    assumptions: List[str] = []
    if client_context and client_context.get("assumptions"):
        assumptions = list(client_context["assumptions"])

    hierarchy = None
    final_spec_content = validated_spec or None
    architecture_overview = handoff.get("architecture_overview") or None

    return PlanningV2AdapterResult(
        requirements=requirements,
        project_overview=project_overview,
        open_questions=open_questions,
        assumptions=assumptions,
        hierarchy=hierarchy,
        final_spec_content=final_spec_content,
        architecture_overview=architecture_overview,
    )
