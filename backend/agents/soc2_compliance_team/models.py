"""Data models for SOC2 compliance audit team."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class TSCCategory(str, Enum):
    """SOC2 Trust Service Criteria categories."""

    SECURITY = "security"
    AVAILABILITY = "availability"
    PROCESSING_INTEGRITY = "processing_integrity"
    CONFIDENTIALITY = "confidentiality"
    PRIVACY = "privacy"


class FindingSeverity(str, Enum):
    """Severity of a compliance finding."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFORMATIONAL = "informational"


class TSCFinding(BaseModel):
    """A single SOC2 compliance finding (gap or observation)."""

    severity: FindingSeverity = Field(..., description="Severity of the finding")
    category: TSCCategory = Field(..., description="Which TSC this relates to")
    title: str = Field(..., description="Short title of the finding")
    description: str = Field(..., description="Detailed description")
    location: str = Field(
        default="", description="File, component, or area (e.g. file path or 'auth module')"
    )
    recommendation: str = Field(default="", description="Recommended remediation or next step")
    evidence_observed: str = Field(
        default="", description="What was observed in the repo (code/config/docs)"
    )


class TSCAuditResult(BaseModel):
    """Result of auditing one Trust Service Criterion."""

    category: TSCCategory = Field(..., description="TSC category audited")
    summary: str = Field(default="", description="Brief summary of the audit for this category")
    findings: List[TSCFinding] = Field(
        default_factory=list, description="List of compliance findings"
    )
    compliant: bool = Field(
        default=True,
        description="True if no critical/high findings; false if material gaps exist.",
    )


class RepoContext(BaseModel):
    """Structured context about the repository for audit agents."""

    repo_path: str = Field(default="", description="Path to the repository")
    code_summary: str = Field(default="", description="Relevant code and config content for audit")
    readme_content: str = Field(default="", description="README or top-level docs if present")
    file_list: List[str] = Field(
        default_factory=list, description="List of relevant file paths scanned"
    )
    tech_stack_hint: str = Field(
        default="", description="Inferred or stated tech stack (e.g. Python/FastAPI, Node)"
    )


class SOC2ComplianceReport(BaseModel):
    """Full SOC2 compliance audit report when issues are found."""

    executive_summary: str = Field(
        ..., description="Executive summary of the audit and overall compliance posture"
    )
    scope: str = Field(
        default="", description="Scope of the audit (repo path, date, criteria in scope)"
    )
    findings_by_tsc: Dict[str, List[TSCFinding]] = Field(
        default_factory=dict,
        description="Findings grouped by TSC category (security, availability, etc.)",
    )
    recommendations_summary: List[str] = Field(
        default_factory=list,
        description="Prioritized list of remediation recommendations",
    )
    report_type: str = Field(
        default="compliance_audit", description="Always 'compliance_audit' for this report"
    )
    raw_markdown: str = Field(default="", description="Full report as markdown for storage/display")


class NextStepsDocument(BaseModel):
    """Document describing next steps toward SOC2 certification when no material issues were found."""

    title: str = Field(default="Next Steps for SOC2 Certification", description="Document title")
    introduction: str = Field(
        default="",
        description="Context: codebase audit found no material SOC2 gaps; next steps to pursue certification",
    )
    steps: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Ordered list of steps, each with title, description, and optional resources",
    )
    recommended_timeline: str = Field(default="", description="High-level timeline suggestion")
    document_type: str = Field(default="next_steps", description="Always 'next_steps'")
    raw_markdown: str = Field(default="", description="Full document as markdown")


class SOC2AuditResult(BaseModel):
    """Final result of the SOC2 audit run."""

    status: str = Field(..., description="completed | failed")
    repo_path: str = Field(default="", description="Repository path audited")
    tsc_results: List[TSCAuditResult] = Field(
        default_factory=list, description="Per-TSC audit results"
    )
    has_findings: bool = Field(
        default=False,
        description="True if any critical/high findings exist; drives report vs next-steps output",
    )
    compliance_report: Optional[SOC2ComplianceReport] = Field(
        default=None,
        description="Present when has_findings is True",
    )
    next_steps_document: Optional[NextStepsDocument] = Field(
        default=None,
        description="Present when has_findings is False",
    )
    error: Optional[str] = Field(default=None, description="Error message if status is failed")
