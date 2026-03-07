from typing import List

from pydantic import BaseModel


class Finding(BaseModel):
    finding_id: str
    title: str
    severity: str
    wcag_reference: str
    target: str
    remediation: str


class EvidenceBundle(BaseModel):
    finding_id: str
    screenshot_path: str
    dom_snippet: str
    user_impact: str
    remediation_suggestion: str
    wcag_reference: str


class TraceabilityLink(BaseModel):
    requirement_id: str
    finding_id: str
    evidence_id: str
    report_section: str
    remediation_ticket: str
    retest_status: str


class CoverageSummary(BaseModel):
    component_coverage: float
    page_coverage: float
    journey_coverage: float
    overall_coverage: float
    missing_statuses: List[str]
