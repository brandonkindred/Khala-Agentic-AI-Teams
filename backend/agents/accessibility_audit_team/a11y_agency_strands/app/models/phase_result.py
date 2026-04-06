"""Typed phase result models — every agent returns a PhaseResult subclass."""

from __future__ import annotations

from pydantic import BaseModel, Field


class PhaseResult(BaseModel):
    """Base return type for all audit-phase agent tools."""

    phase: str = Field(..., description="Phase name, e.g. 'architecture_audit'")
    artifact: str = Field(..., description="Persisted artifact path")


class DiscoveryResult(PhaseResult):
    phase: str = "discovery"
    tier1_count: int = 0


class InventorySetupResult(PhaseResult):
    phase: str = "inventory_setup"
    page: dict = Field(default_factory=dict)
    component: dict = Field(default_factory=dict)


class ComponentAuditResult(PhaseResult):
    phase: str = "component_audit"
    finding_id: str = ""


class JourneyAssessmentResult(PhaseResult):
    phase: str = "journey_assessment"
    journey: str = ""


class PageAuditResult(PhaseResult):
    phase: str = "page_audit"
    page: str = ""


class ArchitecturePhaseResult(PhaseResult):
    phase: str = "architecture_audit"
    overall_grade: str = ""


class InfrastructureAuditResult(PhaseResult):
    phase: str = "infrastructure_audit"


class WCAGCoverageResult(PhaseResult):
    phase: str = "wcag_coverage"
    overall_coverage: float = 0.0


class Sec508MappingResult(PhaseResult):
    phase: str = "sec508_mapping"


class ScoringResult(PhaseResult):
    phase: str = "scoring_prioritization"
    site_score: float = 0.0


class ReportingResult(PhaseResult):
    phase: str = "reporting"


class ApprovalResult(PhaseResult):
    phase: str = "approval"
    approved: bool = False


class RemediationResult(PhaseResult):
    phase: str = "remediation"
    tickets: list[str] = Field(default_factory=list)


class EvidenceResult(PhaseResult):
    phase: str = "evidence"
    finding_id: str = ""


class RetestResult(PhaseResult):
    phase: str = "retest"


class DeliveryResult(PhaseResult):
    phase: str = "delivery"
    status: str = "ready"
