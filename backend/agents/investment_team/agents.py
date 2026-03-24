"""Agent roles and decision logic for the investment organization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from .models import (
    IPS,
    AuditContext,
    GateCheckResult,
    GateResult,
    InvestmentCommitteeMemo,
    PortfolioProposal,
    PromotionDecision,
    PromotionGate,
    PromotionStage,
    StrategySpec,
    ValidationReport,
    ValidationStatus,
)


@dataclass(frozen=True)
class AgentIdentity:
    agent_id: str
    role: str
    version: str


class PolicyGuardianAgent:
    """Enforces IPS hard constraints before any recommendation advances."""

    def check_portfolio(self, ips: IPS, proposal: PortfolioProposal) -> List[str]:
        violations: List[str] = []
        constraints = ips.profile.constraints

        if not proposal.positions:
            return ["Portfolio has no positions."]

        asset_class_weights: Dict[str, float] = {}
        for pos in proposal.positions:
            asset_class_weights[pos.asset_class] = (
                asset_class_weights.get(pos.asset_class, 0.0) + pos.weight_pct
            )

            if pos.weight_pct > constraints.max_single_position_pct:
                violations.append(
                    f"{pos.symbol} exceeds single-position cap "
                    f"({pos.weight_pct}% > {constraints.max_single_position_pct}%)."
                )

        total_weight = sum(pos.weight_pct for pos in proposal.positions)
        if total_weight > 100:
            violations.append(f"Total portfolio weight exceeds 100% ({total_weight}%).")

        for asset_class, weight in sorted(asset_class_weights.items()):
            class_cap = constraints.max_asset_class_pct.get(asset_class)
            if class_cap is not None and weight > class_cap:
                violations.append(
                    f"Asset class {asset_class} exceeds cap ({weight}% > {class_cap}%)."
                )

        excluded_classes = set(ips.profile.preferences.excluded_asset_classes)
        for asset_class in sorted(excluded_classes):
            if asset_class_weights.get(asset_class, 0) > 0:
                violations.append(f"Asset class {asset_class} is excluded by IPS preferences.")

        if not ips.profile.preferences.crypto_allowed and asset_class_weights.get("crypto", 0) > 0:
            violations.append("Crypto position present despite IPS disallowing crypto.")
        if (
            not ips.profile.preferences.options_allowed
            and asset_class_weights.get("options", 0) > 0
        ):
            violations.append("Options position present despite IPS disallowing options.")

        speculative_weight = sum(
            pos.weight_pct for pos in proposal.positions if pos.asset_class in {"crypto", "options"}
        )
        if speculative_weight > ips.speculative_sleeve_cap_pct:
            violations.append(
                f"Speculative sleeve exceeds IPS cap ({speculative_weight}% > {ips.speculative_sleeve_cap_pct}%)."
            )

        return violations


class ValidationAgent:
    """Checks if a strategy passed required testing and scenario standards."""

    REQUIRED_CHECKS = {
        "backtest_quality",
        "walk_forward",
        "stress_test",
        "transaction_cost_model",
        "liquidity_impact",
    }

    def checklist_failures(self, report: ValidationReport) -> List[str]:
        failures: List[str] = []
        present = {c.name for c in report.checks}

        for required in sorted(self.REQUIRED_CHECKS - present):
            failures.append(f"Missing required validation check: {required}.")

        for check in report.checks:
            if check.status == ValidationStatus.FAIL:
                failures.append(f"Failed validation check: {check.name} ({check.details}).")

        return failures


class PromotionGateAgent:
    """Applies universal promotion checklist and enforces separation-of-duties gates."""

    def decide(
        self,
        strategy: StrategySpec,
        validation: ValidationReport,
        ips: IPS,
        proposer_agent_id: str,
        approver: AgentIdentity,
        risk_veto: bool,
        human_live_approval: bool = False,
    ) -> PromotionDecision:
        gate_results: List[GateCheckResult] = []

        def build_audit() -> AuditContext:
            return AuditContext(
                data_snapshot_id=validation.data_snapshot_id,
                assumptions=strategy.audit.assumptions,
                gate_trace=[f"{r.gate.value}:{r.result.value}" for r in gate_results],
                agent_versions={
                    "proposer": strategy.authored_by,
                    "approver": approver.version,
                    "validator": validation.generated_by,
                },
            )

        if proposer_agent_id == approver.agent_id:
            gate_results.append(
                GateCheckResult(
                    gate=PromotionGate.SEPARATION_OF_DUTIES,
                    result=GateResult.FAIL,
                    details="Proposer cannot self-approve.",
                )
            )
            return PromotionDecision(
                strategy_id=strategy.strategy_id,
                decided_by=approver.agent_id,
                outcome=PromotionStage.REJECT,
                rationale="Separation-of-duties violation: proposer cannot self-approve.",
                required_actions=["Assign independent approval agent."],
                gate_results=gate_results,
                audit=build_audit(),
            )
        gate_results.append(
            GateCheckResult(
                gate=PromotionGate.SEPARATION_OF_DUTIES,
                result=GateResult.PASS,
                details="Independent approver verified.",
            )
        )

        if risk_veto:
            gate_results.append(
                GateCheckResult(
                    gate=PromotionGate.RISK_VETO,
                    result=GateResult.FAIL,
                    details="Risk management veto invoked.",
                )
            )
            return PromotionDecision(
                strategy_id=strategy.strategy_id,
                decided_by=approver.agent_id,
                outcome=PromotionStage.REJECT,
                rationale="Risk management veto invoked.",
                required_actions=["Address risk concerns and rerun validation."],
                gate_results=gate_results,
                audit=build_audit(),
            )
        gate_results.append(
            GateCheckResult(
                gate=PromotionGate.RISK_VETO,
                result=GateResult.PASS,
                details="No veto from risk management.",
            )
        )

        validator = ValidationAgent()
        failures = validator.checklist_failures(validation)
        if validation.strategy_id != strategy.strategy_id:
            failures.append("Validation report strategy_id does not match strategy spec.")
        if failures:
            gate_results.append(
                GateCheckResult(
                    gate=PromotionGate.VALIDATION,
                    result=GateResult.FAIL,
                    details="Validation checklist not satisfied.",
                )
            )
            return PromotionDecision(
                strategy_id=strategy.strategy_id,
                decided_by=approver.agent_id,
                outcome=PromotionStage.REVISE,
                rationale="Validation checklist not satisfied.",
                required_actions=failures,
                gate_results=gate_results,
                audit=build_audit(),
            )
        gate_results.append(
            GateCheckResult(
                gate=PromotionGate.VALIDATION,
                result=GateResult.PASS,
                details="All required validation checks passed.",
            )
        )

        if not ips.live_trading_enabled:
            gate_results.append(
                GateCheckResult(
                    gate=PromotionGate.IPS_PERMISSION,
                    result=GateResult.WARN,
                    details="IPS does not permit live trading.",
                )
            )
            return PromotionDecision(
                strategy_id=strategy.strategy_id,
                decided_by=approver.agent_id,
                outcome=PromotionStage.PAPER,
                rationale="IPS does not permit live trading; defaulting to paper mode.",
                required_actions=[
                    "Obtain explicit human approval + IPS update for live promotion."
                ],
                gate_results=gate_results,
                audit=build_audit(),
            )

        gate_results.append(
            GateCheckResult(
                gate=PromotionGate.IPS_PERMISSION,
                result=GateResult.PASS,
                details="IPS permits live trading.",
            )
        )

        if ips.human_approval_required_for_live and not human_live_approval:
            gate_results.append(
                GateCheckResult(
                    gate=PromotionGate.HUMAN_APPROVAL,
                    result=GateResult.WARN,
                    details="Human live approval pending.",
                )
            )
            return PromotionDecision(
                strategy_id=strategy.strategy_id,
                decided_by=approver.agent_id,
                outcome=PromotionStage.PAPER,
                rationale="Live trading requires explicit human approval.",
                required_actions=["Obtain human approval before live promotion."],
                gate_results=gate_results,
                audit=build_audit(),
            )

        gate_results.append(
            GateCheckResult(
                gate=PromotionGate.HUMAN_APPROVAL,
                result=GateResult.PASS,
                details="Human live approval confirmed.",
            )
        )

        return PromotionDecision(
            strategy_id=strategy.strategy_id,
            decided_by=approver.agent_id,
            outcome=PromotionStage.LIVE,
            rationale="All checklist gates passed, no veto, IPS allows live trading, and human approval was recorded.",
            required_actions=["Enable tight risk limits and monitor-first rollout window."],
            gate_results=gate_results,
            audit=build_audit(),
        )


class InvestmentCommitteeAgent:
    """Produces a user-facing recommendation memo with rationale and dissent."""

    def draft_memo(
        self,
        user_id: str,
        recommendation: str,
        rationale: str,
        dissenting_views: List[str] | None = None,
    ) -> InvestmentCommitteeMemo:
        return InvestmentCommitteeMemo(
            memo_id=f"icm-{user_id}",
            prepared_for_user_id=user_id,
            recommendation=recommendation,
            rationale=rationale,
            dissenting_views=dissenting_views or [],
            attachments=[],
        )
