"""
Tool: web.evaluate_site_architecture

Evaluate site architecture and navigation for accessibility using the
structured audit template.  Delegates all scoring to
:class:`TemplateAuditEngine` — no logic is duplicated here.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from ...a11y_agency_strands.app.models.architecture import (
    ArchitectureSectionResult,
    WCAGCriterionStatus,
)
from ...a11y_agency_strands.app.tools.template_audit_engine import TemplateAuditEngine

_TEMPLATE_NAME = "site_architecture_audit_template.yaml"

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


async def evaluate_site_architecture(
    input_data: EvaluateSiteArchitectureInput,
) -> EvaluateSiteArchitectureOutput:
    """Evaluate site architecture and navigation accessibility.

    Delegates entirely to :class:`TemplateAuditEngine` for template loading,
    section scoring, and report assembly.
    """
    engine = TemplateAuditEngine(_TEMPLATE_NAME)
    report = engine.evaluate(
        input_data.url, input_data.checklist_overrides, input_data.recommendations
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
