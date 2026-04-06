"""Architecture Auditor — evaluates site navigation and IA for accessibility."""

from __future__ import annotations

from ..models.architecture import BusinessImpact
from ..models.phase_result import ArchitecturePhaseResult
from ..tools.template_audit_engine import TemplateAuditEngine
from .base import ToolContext, tool

_TEMPLATE_NAME = "site_architecture_audit_template.yaml"

# Business-impact checklist item IDs → BusinessImpact boolean fields.
_BIA_FIELD_MAP: dict[str, str] = {
    "bia_01": "keyboard_tasks_completable",
    "bia_02": "screen_reader_tasks_completable",
    "bia_03": "mobile_tasks_completable",
    "bia_04": "legal_compliance_risk",
}


def _extract_business_impact(overrides: dict[str, dict]) -> BusinessImpact:
    """Build a :class:`BusinessImpact` from the override results."""
    kwargs: dict = {}
    for item_id, field_name in _BIA_FIELD_MAP.items():
        override = overrides.get(item_id, {})
        if "passed" in override and override["passed"] is not None:
            kwargs[field_name] = override["passed"]

    for key in ("top_strengths", "quick_wins", "strategic_opportunities"):
        value = overrides.get(key, {})
        if isinstance(value, list):
            kwargs[key] = value
        elif isinstance(value, dict) and "items" in value:
            kwargs[key] = value["items"]

    return BusinessImpact(**kwargs)


@tool(context=True)
def run_architecture_audit(target: str, tool_context: ToolContext) -> dict:
    """Run the site architecture and navigation accessibility audit."""
    state = tool_context.invocation_state
    overrides: dict = state.get("checklist_results", {})
    recommendations: list[str] = state.get("recommendations", [])

    engine = TemplateAuditEngine(_TEMPLATE_NAME)
    report = engine.evaluate(target, overrides, recommendations)
    report.business_impact = _extract_business_impact(overrides)
    artifact = engine.persist(report, state["artifact_root"])

    return ArchitecturePhaseResult(
        artifact=artifact,
        overall_grade=report.overall_grade,
    ).model_dump()
