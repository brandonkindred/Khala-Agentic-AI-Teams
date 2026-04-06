"""Pydantic models for the Site Architecture & Navigation Accessibility Audit."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ArchitectureChecklistItem(BaseModel):
    """Result for a single checklist item in the architecture audit."""

    id: str = Field(..., description="Checklist item identifier, e.g. 'nse_01'")
    label: str = Field(..., description="Human-readable description of the check")
    passed: Optional[bool] = Field(
        default=None, description="True=pass, False=fail, None=not tested"
    )
    notes: str = Field(default="", description="Auditor notes or failure details")
    wcag_ref: Optional[str] = Field(default=None, description="WCAG SC reference, e.g. '2.4.7'")
    wcag_level: Optional[str] = Field(default=None, description="WCAG level: A or AA")
    test_method: str = Field(default="", description="Testing method used")


class ArchitectureSectionResult(BaseModel):
    """Scored result for a single audit template section."""

    section_id: str = Field(
        ..., description="Section identifier, e.g. 'navigation_system_evaluation'"
    )
    name: str = Field(..., description="Section display name")
    items: list[ArchitectureChecklistItem] = Field(default_factory=list)
    tested_count: int = Field(default=0, description="Items that were actually tested")
    passed_count: int = Field(default=0)
    total_count: int = Field(default=0)
    score_pct: float = Field(default=0.0, description="Pass percentage of tested items, 0.0-100.0")
    grade: str = Field(default="", description="Excellent / Good / Needs Improvement / Poor")
    issues: list[str] = Field(default_factory=list, description="Summary of failing items")


class WCAGCriterionStatus(BaseModel):
    """Compliance status for a single navigation-related WCAG criterion."""

    sc: str = Field(..., description="Success criterion number, e.g. '2.4.7'")
    name: str = Field(default="", description="Criterion name, e.g. 'Focus Visible'")
    wcag_level: str = Field(default="", description="A or AA")
    status: str = Field(default="not_tested", description="pass / fail / partial / not_tested")
    related_items: list[str] = Field(
        default_factory=list, description="Checklist item IDs that cover this SC"
    )


class BusinessImpact(BaseModel):
    """Business impact assessment from the architecture audit."""

    keyboard_tasks_completable: Optional[bool] = Field(default=None)
    screen_reader_tasks_completable: Optional[bool] = Field(default=None)
    mobile_tasks_completable: Optional[bool] = Field(default=None)
    legal_compliance_risk: Optional[bool] = Field(
        default=None, description="True if barriers create legal risk"
    )
    top_strengths: list[str] = Field(default_factory=list)
    quick_wins: list[str] = Field(default_factory=list)
    strategic_opportunities: list[str] = Field(default_factory=list)


class ArchitectureAuditResult(BaseModel):
    """Full result of the site architecture and navigation accessibility audit."""

    target: str = Field(..., description="Site URL or identifier audited")
    template_version: str = Field(default="1.0")
    sections: list[ArchitectureSectionResult] = Field(default_factory=list)
    overall_score_pct: float = Field(
        default=0.0, description="Average of section pass percentages (equal section weighting)"
    )
    overall_grade: str = Field(default="", description="Overall grade from scoring scale")
    wcag_compliance: list[WCAGCriterionStatus] = Field(default_factory=list)
    business_impact: Optional[BusinessImpact] = Field(default=None)
    recommendations: list[str] = Field(
        default_factory=list, description="Prioritized recommendation list"
    )
