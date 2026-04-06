"""
Tool: web.evaluate_site_architecture

Evaluate site architecture and navigation for accessibility using the
structured audit template.

Delegates scoring and grading to the shared architecture tools in the
strands implementation — no logic is duplicated here.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from ...a11y_agency_strands.app.models.architecture import (
    ArchitectureSectionResult,
    WCAGCriterionStatus,
)
from ...a11y_agency_strands.app.tools.architecture_tools import (
    build_architecture_audit_report,
    load_architecture_audit_template,
    score_architecture_section,
)

# ---------------------------------------------------------------------------
# I/O models (thin wrappers specific to the async web-tool interface)
# ---------------------------------------------------------------------------


class EvaluateSiteArchitectureInput(BaseModel):
    """Input for evaluating site architecture accessibility."""

    audit_id: str = Field(..., description="Audit identifier")
    url: str = Field(..., description="Root URL of the site being audited")
    checklist_overrides: Dict[str, Dict[str, Any]] = Field(
        default_factory=dict,
        description="Map of checklist item ID to {passed: bool | None, notes: str}",
    )
    recommendations: Optional[List[str]] = Field(
        default=None,
        description="Prioritized recommendation strings",
    )


class SectionScoreSummary(BaseModel):
    """Compact section score for the tool output (no full item list)."""

    section_id: str
    name: str = ""
    tested_count: int = 0
    passed_count: int = 0
    total_count: int = 0
    score_pct: float = 0.0
    grade: str = ""
    failing_items: List[str] = Field(default_factory=list)

    @classmethod
    def from_section_result(cls, result: ArchitectureSectionResult) -> "SectionScoreSummary":
        return cls(
            section_id=result.section_id,
            name=result.name,
            tested_count=result.tested_count,
            passed_count=result.passed_count,
            total_count=result.total_count,
            score_pct=result.score_pct,
            grade=result.grade,
            failing_items=result.issues,
        )


class WCAGComplianceEntry(BaseModel):
    """Per-criterion result for the tool output."""

    sc: str
    name: str = ""
    wcag_level: str = ""
    status: str = "not_tested"
    related_items: List[str] = Field(default_factory=list)

    @classmethod
    def from_status(cls, status: WCAGCriterionStatus) -> "WCAGComplianceEntry":
        return cls(
            sc=status.sc,
            name=status.name,
            wcag_level=status.wcag_level,
            status=status.status,
            related_items=status.related_items,
        )


class EvaluateSiteArchitectureOutput(BaseModel):
    """Output from site architecture evaluation."""

    url: str
    template_version: str = "1.0"
    section_scores: List[SectionScoreSummary] = Field(default_factory=list)
    overall_score_pct: float = 0.0
    overall_grade: str = ""
    wcag_compliance: List[WCAGComplianceEntry] = Field(default_factory=list)
    recommendations: List[str] = Field(default_factory=list)
    raw_ref: str = Field(default="", description="Reference to raw results artifact")


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


def _flatten_items(section_def: dict) -> list[dict]:
    items: list[dict] = []
    for sub in section_def.get("subsections", []):
        for item in sub.get("checklist_items", []):
            items.append(item)
    return items


async def evaluate_site_architecture(
    input_data: EvaluateSiteArchitectureInput,
) -> EvaluateSiteArchitectureOutput:
    """Evaluate site architecture and navigation accessibility.

    Loads the structured audit template, applies any checklist result
    overrides from ``input_data``, delegates scoring to the shared
    architecture tools, and returns an overall assessment with WCAG
    compliance mapping.

    This tool is typically called by the Web Audit Specialist (WAS)
    or a dedicated Architecture Auditor during the architecture_audit phase.
    """
    template = load_architecture_audit_template()
    overrides = input_data.checklist_overrides

    section_results: list[ArchitectureSectionResult] = []
    for section_def in template.get("sections", []):
        section_id = section_def["id"]
        section_name = section_def["name"]
        template_items = _flatten_items(section_def)

        evaluated: list[dict] = []
        for item in template_items:
            item_id = item["id"]
            override = overrides.get(item_id, {})
            evaluated.append(
                {
                    "id": item_id,
                    "label": item.get("label", ""),
                    "passed": override.get("passed"),
                    "notes": override.get("notes", ""),
                    "wcag_ref": item.get("wcag_ref"),
                    "wcag_level": item.get("wcag_level"),
                    "test_method": item.get("test_method", ""),
                }
            )

        scored = score_architecture_section(section_id, section_name, evaluated, template)
        section_results.append(scored)

    report = build_architecture_audit_report(
        input_data.url, section_results, input_data.recommendations, template
    )

    return EvaluateSiteArchitectureOutput(
        url=input_data.url,
        section_scores=[SectionScoreSummary.from_section_result(s) for s in report.sections],
        overall_score_pct=report.overall_score_pct,
        overall_grade=report.overall_grade,
        wcag_compliance=[WCAGComplianceEntry.from_status(s) for s in report.wcag_compliance],
        recommendations=report.recommendations,
        raw_ref=f"arch_audit_{input_data.audit_id}_{hash(input_data.url) % 10000}",
    )
