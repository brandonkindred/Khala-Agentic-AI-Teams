"""Orchestration plan for the investment team workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List

from .agents import AgentIdentity, PolicyGuardianAgent, PromotionGateAgent
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


class WebActionClass(str, Enum):
    READ_ONLY = "read_only"
    PAPER_TRADING = "paper_trading"
    LIVE_TRADING = "live_trading"
    ACCOUNT_SETTINGS = "account_settings"


@dataclass
class ExternalUIAction:
    event_id: str
    platform: str
    action_class: WebActionClass
    operation: str

    @property
    def requires_human_approval(self) -> bool:
        irreversible_ops = {
            "submit_live_order",
            "modify_account_settings",
        }
        return self.operation in irreversible_ops or self.action_class in {
            WebActionClass.LIVE_TRADING,
            WebActionClass.ACCOUNT_SETTINGS,
        }


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

    def __init__(self) -> None:
        self.policy_guardian = PolicyGuardianAgent()
        self.promotion_gate = PromotionGateAgent()

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

    def dispatch_external_ui_action(
        self,
        state: WorkflowState,
        ips: IPS,
        action: ExternalUIAction,
        human_approval: bool = False,
    ) -> bool:
        allowed, reason = self._pre_execution_gate(
            state=state,
            ips=ips,
            action=action,
            human_approval=human_approval,
        )
        normalized_event_id = self._normalize_event_id(action.event_id)
        verdict = "approved" if allowed else "denied"
        state.audit_log.append(
            f"ui_action:{normalized_event_id}:{action.platform}:{action.action_class.value}:{verdict}:{reason}"
        )
        if allowed:
            self.enqueue(state, QueueItem(queue="execution", payload_id=normalized_event_id, priority="high"))
        return allowed

    def _pre_execution_gate(
        self,
        state: WorkflowState,
        ips: IPS,
        action: ExternalUIAction,
        human_approval: bool,
    ) -> tuple[bool, str]:
        mode_gate = {
            WorkflowMode.MONITOR_ONLY: {WebActionClass.READ_ONLY},
            WorkflowMode.PAPER: {WebActionClass.READ_ONLY, WebActionClass.PAPER_TRADING},
            WorkflowMode.LIVE: {
                WebActionClass.READ_ONLY,
                WebActionClass.PAPER_TRADING,
                WebActionClass.LIVE_TRADING,
                WebActionClass.ACCOUNT_SETTINGS,
            },
            WorkflowMode.ADVISORY: {WebActionClass.READ_ONLY},
        }
        allowed_actions = mode_gate.get(state.mode, {WebActionClass.READ_ONLY})
        if action.action_class not in allowed_actions:
            return False, f"mode_blocked:{state.mode.value}"

        if action.action_class == WebActionClass.LIVE_TRADING and not ips.live_trading_enabled:
            return False, "ips_live_trading_disabled"

        if action.requires_human_approval and not human_approval:
            return False, "missing_human_approval"

        return True, "gate_pass"

    def _normalize_event_id(self, event_id: str) -> str:
        normalized = "".join(ch if ch.isalnum() else "_" for ch in event_id.strip().lower())
        while "__" in normalized:
            normalized = normalized.replace("__", "_")
        return normalized.strip("_") or "ui_action"
