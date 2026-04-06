"""Architecture Auditor — evaluates site navigation and IA for accessibility."""

from __future__ import annotations

from ..models.architecture import BusinessImpact
from ..tools import persist_artifact
from ..tools.architecture_tools import (
    build_architecture_audit_report,
    load_architecture_audit_template,
    score_architecture_section,
)
from .base import ToolContext, tool

# Business-impact checklist item IDs → BusinessImpact boolean fields.
_BIA_FIELD_MAP: dict[str, str] = {
    "bia_01": "keyboard_tasks_completable",
    "bia_02": "screen_reader_tasks_completable",
    "bia_03": "mobile_tasks_completable",
    "bia_04": "legal_compliance_risk",
}


def _flatten_checklist_items(section_def: dict) -> list[dict]:
    """Extract all checklist items from a template section, including subsections."""
    items: list[dict] = []
    for sub in section_def.get("subsections", []):
        for item in sub.get("checklist_items", []):
            items.append(item)
    return items


def _extract_business_impact(
    overrides: dict[str, dict],
) -> BusinessImpact:
    """Build a :class:`BusinessImpact` from the override results.

    Maps the ``bia_*`` checklist items to the typed boolean fields and
    pulls free-text lists from the ``strengths_and_opportunities``
    overrides (keyed ``top_strengths``, ``quick_wins``,
    ``strategic_opportunities``).
    """
    kwargs: dict = {}
    for item_id, field_name in _BIA_FIELD_MAP.items():
        override = overrides.get(item_id, {})
        # Only set the field if the item was actually tested.
        if "passed" in override and override["passed"] is not None:
            kwargs[field_name] = override["passed"]

    # Free-text lists live alongside the checklist overrides keyed by
    # the field id from the YAML's ``strengths_and_opportunities`` subsection.
    for key in ("top_strengths", "quick_wins", "strategic_opportunities"):
        value = overrides.get(key, {})
        if isinstance(value, list):
            kwargs[key] = value
        elif isinstance(value, dict) and "items" in value:
            kwargs[key] = value["items"]

    return BusinessImpact(**kwargs)


@tool(context=True)
def run_architecture_audit(target: str, tool_context: ToolContext) -> dict:
    """Run the site architecture and navigation accessibility audit.

    Loads the structured audit template, evaluates each section's checklist
    items against the provided results in ``tool_context.invocation_state``,
    scores every section, and assembles the full audit report.

    ``tool_context.invocation_state`` may contain:

    * ``artifact_root`` — directory for persisting the output artifact.
    * ``checklist_results`` — dict mapping checklist item IDs to
      ``{"passed": bool | None, "notes": str}`` overrides.  Items not
      present in this mapping are left as ``passed=None`` (not tested).
    * ``recommendations`` — optional list of prioritized recommendation
      strings to include in the report.
    """
    template = load_architecture_audit_template()
    state = tool_context.invocation_state
    overrides: dict = state.get("checklist_results", {})
    recommendations: list[str] = state.get("recommendations", [])

    section_results = []
    for section_def in template.get("sections", []):
        section_id = section_def["id"]
        section_name = section_def["name"]
        template_items = _flatten_checklist_items(section_def)

        evaluated: list[dict] = []
        for item in template_items:
            item_id = item["id"]
            override = overrides.get(item_id, {})
            evaluated.append(
                {
                    "id": item_id,
                    "label": item.get("label", ""),
                    "passed": override.get("passed"),  # None when not tested
                    "notes": override.get("notes", ""),
                    "wcag_ref": item.get("wcag_ref"),
                    "wcag_level": item.get("wcag_level"),
                    "test_method": item.get("test_method", ""),
                }
            )

        scored = score_architecture_section(section_id, section_name, evaluated, template)
        section_results.append(scored)

    report = build_architecture_audit_report(target, section_results, recommendations, template)

    # Populate business impact from the business_impact_assessment results.
    report.business_impact = _extract_business_impact(overrides)

    artifact_path = f"{state['artifact_root']}/architecture.json"
    artifact = persist_artifact(artifact_path, report.model_dump())

    return {
        "phase": "architecture_audit",
        "artifact": artifact,
        "overall_grade": report.overall_grade,
    }
