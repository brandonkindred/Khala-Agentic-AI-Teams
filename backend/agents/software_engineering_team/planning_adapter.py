"""
Adapter: maps Planning handoff package to inputs expected by Tech Lead and Architecture.

Used by the software engineering orchestrator after planning_team.orchestrator.run_workflow()
to produce ProductRequirements, project_overview dict, and optional open_questions/assumptions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from shared.dev_models.models import PlanningHierarchy, ProductRequirements
from software_engineering_team.shared.project_overview_builder import build_project_overview

logger = logging.getLogger(__name__)


@dataclass
class PlanningAdapterResult:
    """Result of adapting a planning workflow for Tech Lead and Architecture."""

    requirements: ProductRequirements
    project_overview: Dict[str, Any]
    open_questions: List[str]
    assumptions: List[str]
    hierarchy: Optional[PlanningHierarchy] = field(default=None)
    final_spec_content: Optional[str] = field(default=None)
    architecture_overview: Optional[str] = field(default=None)
    shared_planning_doc_path: Optional[str] = field(default=None)
    resolved_questions: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe payload for cross-process handoff (e.g. Temporal activity results).

        This is a dataclass, not a Pydantic model — generic ``model_dump`` probing
        silently yields nothing for it, which is how the Temporal planning→coding
        handoff shipped serializing ``{}``.

        Postconditions: round-trips through :meth:`from_dict` losslessly; nested
        Pydantic models are dumped to plain dicts.
        """
        return {
            "requirements": self.requirements.model_dump(),
            "project_overview": self.project_overview,
            "open_questions": list(self.open_questions),
            "assumptions": list(self.assumptions),
            "hierarchy": self.hierarchy.model_dump() if self.hierarchy else None,
            "final_spec_content": self.final_spec_content,
            "architecture_overview": self.architecture_overview,
            "shared_planning_doc_path": self.shared_planning_doc_path,
            "resolved_questions": list(self.resolved_questions),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlanningAdapterResult":
        """Rebuild from a :meth:`to_dict` payload.

        Preconditions: ``data`` carries a valid ``requirements`` mapping (the one
        field with no usable default — its absence is a handoff bug, not a state
        to paper over).
        """
        return cls(
            requirements=ProductRequirements.model_validate(data["requirements"]),
            project_overview=data.get("project_overview") or {},
            open_questions=list(data.get("open_questions") or []),
            assumptions=list(data.get("assumptions") or []),
            hierarchy=PlanningHierarchy.model_validate(data["hierarchy"])
            if data.get("hierarchy")
            else None,
            final_spec_content=data.get("final_spec_content"),
            architecture_overview=data.get("architecture_overview"),
            shared_planning_doc_path=data.get("shared_planning_doc_path"),
            resolved_questions=list(data.get("resolved_questions") or []),
        )


__all__ = ["adapt_planning_result", "PlanningAdapterResult"]

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


def adapt_planning_result(
    result: Dict[str, Any],
    spec_title: str = "Project",
    repo_path: Optional[str] = None,
) -> PlanningAdapterResult:
    """
    Map Planning workflow result (handoff package) to PlanningAdapterResult.

    Args:
        result: Return value of planning_team.orchestrator.run_workflow()
            (success, handoff_package, failure_reason).
        spec_title: Title for the requirements (e.g. from initial spec).
        repo_path: Optional repo path; used to read PRD from disk when handoff
            has no prd_content (e.g. when use_product_analysis=False).

    Returns:
        PlanningAdapterResult with requirements, project_overview, open_questions,
        assumptions, hierarchy=None, final_spec_content.

    Raises:
        ValueError: If result.success is False or handoff is missing.
    """
    if not result.get("success", False):
        reason = result.get("failure_reason") or "Planning workflow did not complete successfully."
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
    description = (
        "\n\n".join(description_parts) if description_parts else "See Planning handoff artifacts."
    )

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

    project_overview: Dict[str, Any] = build_project_overview(
        prd_content=prd_content, client_context=client_context
    )

    # Adapter-specific: append target_users to features doc (not in shared helper).
    features_and_functionality_doc = project_overview["features_and_functionality_doc"]
    if client_context and client_context.get("target_users"):
        target_section = "## Target users\n" + "\n".join(
            f"- {u}" for u in client_context["target_users"]
        )
        if features_and_functionality_doc:
            features_and_functionality_doc += "\n\n" + target_section
        else:
            features_and_functionality_doc = target_section
        project_overview["features_and_functionality_doc"] = features_and_functionality_doc

    # Adapter-specific: fall back to handoff summary when goals are empty.
    if not project_overview["goals"] and handoff.get("summary"):
        project_overview["goals"] = handoff["summary"]

    # Open/resolved questions are carried across the planning handoff so the SE gate can escalate
    # unanswered product questions to the user instead of letting them be auto-decided downstream.
    open_questions: List[str] = []
    for q in handoff.get("open_questions") or []:
        if isinstance(q, dict):
            text = q.get("question_text") or q.get("text") or q.get("question") or ""
        else:
            text = str(q)
        if text:
            open_questions.append(text)
    resolved_questions: List[Dict[str, Any]] = list(handoff.get("resolved_questions") or [])

    assumptions: List[str] = []
    if client_context and client_context.get("assumptions"):
        assumptions = list(client_context["assumptions"])

    hierarchy = None
    final_spec_content = validated_spec or None
    architecture_overview = handoff.get("architecture_overview") or None

    return PlanningAdapterResult(
        requirements=requirements,
        project_overview=project_overview,
        open_questions=open_questions,
        assumptions=assumptions,
        hierarchy=hierarchy,
        final_spec_content=final_spec_content,
        architecture_overview=architecture_overview,
        resolved_questions=resolved_questions,
    )
