"""Agent roles and decision logic for the investment organization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .models import (
    IPS,
    InvestmentCommitteeMemo,
    PortfolioProposal,
    PromotionDecision,
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

        for pos in proposal.positions:
            if pos.weight_pct > constraints.max_single_position_pct:
                violations.append(
                    f"{pos.symbol} exceeds single-position cap "
                    f"({pos.weight_pct}% > {constraints.max_single_position_pct}%)."
                )

            class_cap = constraints.max_asset_class_pct.get(pos.asset_class)
            if class_cap is not None and pos.weight_pct > class_cap:
                violations.append(
                    f"{pos.symbol} breaches asset class cap for {pos.asset_class} "
                    f"({pos.weight_pct}% > {class_cap}%)."
                )

        if not ips.profile.preferences.crypto_allowed:
            for pos in proposal.positions:
                if pos.asset_class == "crypto":
                    violations.append("Crypto position present despite IPS disallowing crypto.")

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
    ) -> PromotionDecision:
        if proposer_agent_id == approver.agent_id:
            return PromotionDecision(
                strategy_id=strategy.strategy_id,
                decided_by=approver.agent_id,
                outcome=PromotionStage.REJECT,
                rationale="Separation-of-duties violation: proposer cannot self-approve.",
                required_actions=["Assign independent approval agent."],
            )

        validator = ValidationAgent()
        failures = validator.checklist_failures(validation)

        if risk_veto:
            return PromotionDecision(
                strategy_id=strategy.strategy_id,
                decided_by=approver.agent_id,
                outcome=PromotionStage.REJECT,
                rationale="Risk management veto invoked.",
                required_actions=["Address risk concerns and rerun validation."],
            )

        if failures:
            return PromotionDecision(
                strategy_id=strategy.strategy_id,
                decided_by=approver.agent_id,
                outcome=PromotionStage.REVISE,
                rationale="Validation checklist not satisfied.",
                required_actions=failures,
            )

        if not ips.live_trading_enabled:
            return PromotionDecision(
                strategy_id=strategy.strategy_id,
                decided_by=approver.agent_id,
                outcome=PromotionStage.PAPER,
                rationale="IPS does not permit live trading; defaulting to paper mode.",
                required_actions=["Obtain explicit human approval + IPS update for live promotion."],
            )

        return PromotionDecision(
            strategy_id=strategy.strategy_id,
            decided_by=approver.agent_id,
            outcome=PromotionStage.LIVE,
            rationale="All checklist gates passed, no veto, and IPS allows live trading.",
            required_actions=["Enable tight risk limits and monitor-first rollout window."],
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
