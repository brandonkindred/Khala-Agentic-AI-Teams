"""Orchestration plan for the investment team workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from .agents import AgentIdentity, PolicyGuardianAgent, PromotionGateAgent
from .tool_agents.web_interfaces import InvestmentWebInterfaceCoordinator

from .models import (
    IPS,
    PortfolioProposal,
    PromotionDecision,
    StrategySpec,
    ValidationReport,
    WorkflowMode,
)


@dataclass
class QueueItem:
    queue: str
    payload_id: str
    priority: str = "normal"


@dataclass
class WorkflowState:
    queues: Dict[str, List[QueueItem]] = field(
        default_factory=lambda: {
            "research": [],
            "portfolio_design": [],
            "validation": [],
            "promotion": [],
            "execution": [],
            "escalation": [],
        }
    )
    mode: WorkflowMode = WorkflowMode.MONITOR_ONLY
    audit_log: List[str] = field(default_factory=list)


class InvestmentTeamOrchestrator:
    """
    Coordinates research -> proposal -> validation -> promotion with safety gates.

    Safety behavior:
    - If integrity_ok is false, workflow degrades to monitor_only.
    - Risk has veto power at promotion stage.
    - Live promotion requires IPS permission and independent approver.
    """

    def __init__(self, web_interface_coordinator: InvestmentWebInterfaceCoordinator | None = None) -> None:
        self.policy_guardian = PolicyGuardianAgent()
        self.promotion_gate = PromotionGateAgent()
        self.web_interface_coordinator = web_interface_coordinator

    def bootstrap(self, state: WorkflowState, ips: IPS) -> None:
        state.mode = ips.default_mode
        state.audit_log.append(f"workflow_bootstrap:mode={state.mode.value}")

    def enqueue(self, state: WorkflowState, item: QueueItem) -> None:
        state.queues[item.queue].append(item)
        state.audit_log.append(f"enqueued:{item.queue}:{item.payload_id}:{item.priority}")

    def handle_data_integrity(self, state: WorkflowState, integrity_ok: bool) -> None:
        if not integrity_ok:
            state.mode = WorkflowMode.MONITOR_ONLY
            state.audit_log.append("data_integrity_failed:degrade_to_monitor_only")

    def check_proposal(self, state: WorkflowState, ips: IPS, proposal: PortfolioProposal) -> List[str]:
        violations = self.policy_guardian.check_portfolio(ips, proposal)
        if violations:
            state.audit_log.append(f"proposal_rejected:{proposal.proposal_id}")
        else:
            state.audit_log.append(f"proposal_passed:{proposal.proposal_id}")
        return violations

    def promotion_decision(
        self,
        state: WorkflowState,
        strategy: StrategySpec,
        validation: ValidationReport,
        ips: IPS,
        proposer_agent_id: str,
        approver: AgentIdentity,
        risk_veto: bool,
        human_live_approval: bool = False,
    ) -> PromotionDecision:
        decision = self.promotion_gate.decide(
            strategy=strategy,
            validation=validation,
            ips=ips,
            proposer_agent_id=proposer_agent_id,
            approver=approver,
            risk_veto=risk_veto,
            human_live_approval=human_live_approval,
        )
        state.audit_log.append(f"promotion:{strategy.strategy_id}:{decision.outcome.value}")
        if decision.outcome.value in {"reject", "revise"}:
            self.enqueue(state, QueueItem(queue="escalation", payload_id=strategy.strategy_id, priority="high"))
        return decision

    def run_web_action(
        self,
        action: str,
        payload: Dict[str, Any] | None = None,
        workspace_name: str | None = None,
    ) -> Dict[str, Any]:
        """Run provider-backed web action when coordinator is configured."""
        if not self.web_interface_coordinator:
            raise RuntimeError("web interface coordinator is not configured")
        return self.web_interface_coordinator.execute_action(
            action=action,
            payload=payload,
            workspace_name=workspace_name,
        )
