from __future__ import annotations

from dataclasses import dataclass, field

from ..models import TraceabilityLink
from ..tools import update_traceability_matrix
from .approval_agent import run_approval_and_comms
from .architecture_agent import run_architecture_audit
from .base import ToolContext
from .component_auditor import run_component_audit
from .discovery_agent import run_discovery
from .evidence_agent import run_evidence_curation
from .infrastructure_agent import run_infrastructure_audit
from .journey_agent import run_journey_assessment
from .page_auditor import run_page_audit
from .remediation_agent import run_remediation_planning
from .report_agent import run_reporting
from .retest_agent import run_retest_cycle
from .scoring_agent import run_scoring_and_prioritization
from .sec508_agent import run_508_mapping
from .wcag_agent import run_wcag_coverage


@dataclass(slots=True)
class OrchestratorState:
    current_phase: str = "discovery"
    completed_tasks: list[str] = field(default_factory=list)
    findings_index: list[str] = field(default_factory=list)
    coverage_counts: dict[str, int] = field(default_factory=dict)
    pending_approvals: list[str] = field(default_factory=list)
    retry_counters: dict[str, int] = field(default_factory=dict)
    approval_granted: bool = False


class EngagementOrchestrator:
    """Deterministic control plane for the accessibility agency workflow."""

    def __init__(self, invocation_state: dict):
        self.context = ToolContext(invocation_state=invocation_state)
        self.state = OrchestratorState()
        self.traceability_matrix: dict = {"links": []}

    def _mark_complete(self, phase: str) -> None:
        self.state.current_phase = phase
        self.state.completed_tasks.append(phase)

    def run_discovery(self, raw_answers: dict) -> dict:
        result = run_discovery(raw_answers, self.context)
        self._mark_complete("discovery")
        return result

    def run_inventory_setup(self, page_id: str, component_id: str) -> dict:
        page = run_page_audit(page_id, self.context)
        component = run_component_audit(component_id, self.context)
        self._mark_complete("inventory_setup")
        return {"page": page, "component": component}

    def run_component_audit(self, component_id: str) -> dict:
        result = run_component_audit(component_id, self.context)
        self.state.findings_index.append(result["finding_id"])
        self._mark_complete("component_audit")
        return result

    def run_journey_assessment(self, journey_id: str) -> dict:
        result = run_journey_assessment(journey_id, self.context)
        self._mark_complete("journey_assessment")
        return result

    def run_page_audit(self, page_id: str) -> dict:
        result = run_page_audit(page_id, self.context)
        self._mark_complete("page_audit")
        return result

    def run_architecture_audit(self, target: str) -> dict:
        result = run_architecture_audit(target, self.context)
        self._mark_complete("architecture_audit")
        return result

    def run_infrastructure_audit(self, target: str) -> dict:
        result = run_infrastructure_audit(target, self.context)
        self._mark_complete("infrastructure_audit")
        return result

    def run_wcag_coverage(self, engagement_id: str) -> dict:
        result = run_wcag_coverage(engagement_id, self.context)
        self.state.coverage_counts["overall"] = int(result["overall_coverage"] * 100)
        self._mark_complete("wcag_coverage")
        return result

    def run_508_mapping(self, engagement_id: str) -> dict:
        result = run_508_mapping(engagement_id, self.context)
        self._mark_complete("sec508_mapping")
        return result

    def run_scoring_and_prioritization(self, engagement_id: str) -> dict:
        result = run_scoring_and_prioritization(engagement_id, self.context)
        self._mark_complete("scoring_prioritization")
        return result

    def run_reporting(self, engagement_id: str) -> dict:
        self._enforce_reporting_gate()
        findings = [{"finding_id": fid} for fid in self.state.findings_index]
        evidence = (
            run_evidence_curation(
                self.state.findings_index[0], "checkout", self.context
            )
            if self.state.findings_index
            else None
        )
        if evidence:
            link = TraceabilityLink(
                requirement_id="REQ-1",
                finding_id=evidence["finding_id"],
                evidence_id=evidence["artifact"],
                report_section="technical-report",
                remediation_ticket="A11Y-1",
                retest_status="pending",
            )
            self.traceability_matrix = update_traceability_matrix(
                self.traceability_matrix, link.model_dump()
            )
        result = run_reporting(engagement_id, findings, self.context)
        self._mark_complete("reporting")
        return result

    def request_human_approval(self, engagement_id: str) -> dict:
        result = run_approval_and_comms(
            engagement_id, "Delivery package ready", self.context
        )
        self.state.pending_approvals.append(result["artifact"])
        self.state.approval_granted = bool(result.get("approved", False))
        if self.state.approval_granted:
            self._mark_complete("approval")
        return result

    def run_delivery(self) -> dict:
        self._enforce_delivery_gate()
        self._mark_complete("delivery")
        return {"phase": "delivery", "status": "ready"}

    def run_retest_cycle(self, engagement_id: str) -> dict:
        result = run_retest_cycle(engagement_id, self.context)
        self._mark_complete("retest")
        return result

    def run_remediation_planning(self) -> dict:
        findings = [{"finding_id": fid} for fid in self.state.findings_index]
        result = run_remediation_planning(findings, self.context)
        self._mark_complete("remediation")
        return result

    def _enforce_reporting_gate(self) -> None:
        required = {
            "component_audit",
            "journey_assessment",
            "page_audit",
            "wcag_coverage",
        }
        missing = required.difference(set(self.state.completed_tasks))
        if missing:
            raise ValueError(f"Reporting blocked; missing phases: {sorted(missing)}")

    def _enforce_delivery_gate(self) -> None:
        required = {"reporting", "remediation", "sec508_mapping"}
        missing = required.difference(set(self.state.completed_tasks))
        if missing:
            raise ValueError(f"Delivery blocked; missing phases: {sorted(missing)}")
        if not self.state.approval_granted:
            raise ValueError("Delivery blocked; approval not granted")
