"""Issue #527 — deterministic conformance gate for engine-enforced exit rules.

Once :class:`TradingService` enforces structured ``ExitRule`` objects in its
bar loop, every trade either obeys the rules within tolerance or comes from
the strategy code's own exit logic.  This gate iterates the trade ledger
and the execution diagnostics and reports:

* **StopLossRule(pct=P, basis="entry_price")** — for losing trades, the
  worst observed return is bounded by ``-pct - slippage_tolerance``.
  Slippage on the close fill can push the realised return slightly past
  the rule's floor; the tolerance band derives from
  ``BacktestConfig.slippage_bps``. Trailing variants need bar-by-bar
  replay and are flagged as informational.
* **TakeProfitRule(pct=P)** — sanity-only: when ``exit_rules`` contains
  exactly one rule and that rule is the take-profit, we expect at least
  one engine firing. When other rules are present, the take-profit may
  legitimately never fire (a stop-loss closed the trade first).
* **SignalExitRule** — informational; not yet engine-enforced.

The gate is **deterministic and post-hoc**. It does not re-run the
strategy or call out to an LLM. Critical failures here indicate either
a bug in :mod:`rule_compiler` or a violation of engine invariants — both
of which the alignment agent's LLM-driven audit should defer to.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from ...models import (
    BacktestConfig,
    BacktestExecutionDiagnostics,
    TradeRecord,
)
from ..spec_dsl import (
    ExitRule,
    SignalExitRule,
    StopLossRule,
    TakeProfitRule,
)
from .models import QualityGateResult

GATE = "exit_rule_conformance"


class ExitRuleConformanceGate:
    """Deterministic post-run check that engine-emitted exits match the spec."""

    def check(
        self,
        *,
        exit_rules: Sequence[ExitRule],
        trades: Sequence[TradeRecord],
        diagnostics: Optional[BacktestExecutionDiagnostics],
        config: BacktestConfig,
        timeframe: str = "1d",
    ) -> List[QualityGateResult]:
        if not exit_rules:
            return [
                QualityGateResult(
                    gate_name=GATE,
                    passed=True,
                    severity="info",
                    details="spec.exit_rules empty; engine-enforcement check skipped.",
                )
            ]

        results: List[QualityGateResult] = []

        # ---- StopLossRule (entry_price basis only — trailing variants need
        # bar-by-bar replay which the gate cannot reconstruct from the trade
        # ledger alone) ----
        stop_losses = [r for r in exit_rules if isinstance(r, StopLossRule)]
        for rule in stop_losses:
            results.append(self._check_stop_loss(rule, trades, config))

        # ---- TakeProfitRule (sanity only) ----
        take_profits = [r for r in exit_rules if isinstance(r, TakeProfitRule)]
        for rule in take_profits:
            results.append(self._check_take_profit(rule, trades, exit_rules))

        # ---- SignalExitRule — not yet engine-enforced ----
        signal_exits = [r for r in exit_rules if isinstance(r, SignalExitRule)]
        if signal_exits:
            results.append(
                QualityGateResult(
                    gate_name=GATE,
                    passed=True,
                    severity="info",
                    details=(
                        f"{len(signal_exits)} SignalExitRule(s) present but not yet "
                        "engine-enforced (see rule_compiler module docstring)."
                    ),
                )
            )

        # ---- Aggregate: engine emitted at least one exit when expected ----
        if diagnostics is not None:
            firings = diagnostics.exit_rule_firings or {}
            total = sum(firings.values())
            details = "engine_exits: " + (
                ", ".join(f"{k}={v}" for k, v in sorted(firings.items())) or "none"
            )
            results.append(
                QualityGateResult(
                    gate_name=GATE,
                    passed=True,
                    severity="info",
                    details=details + f" (total={total}, trades={len(trades)})",
                )
            )

        return results

    # ------------------------------------------------------------------
    # Per-rule checks
    # ------------------------------------------------------------------

    @staticmethod
    def _check_stop_loss(
        rule: StopLossRule,
        trades: Sequence[TradeRecord],
        config: BacktestConfig,
    ) -> QualityGateResult:
        if rule.basis != "entry_price":
            return QualityGateResult(
                gate_name=GATE,
                passed=True,
                severity="info",
                details=(
                    f"StopLossRule(basis={rule.basis!r}) conformance check skipped "
                    "(trailing variants require bar-by-bar replay)."
                ),
            )
        # Tolerance band: slippage on the close fill can push the realised
        # return past the rule's floor. Use 2x the configured slippage as
        # an upper-bound on cumulative entry+exit slip, plus a small
        # absolute pad so float-equality noise doesn't trip the gate.
        slip_pct = (config.slippage_bps / 10_000.0) * 2.0
        floor_pct = -(rule.pct * 100.0) - (slip_pct * 100.0) - 0.5
        offenders = [t for t in trades if t.return_pct < floor_pct]
        if offenders:
            sample = [(t.trade_num, t.return_pct) for t in offenders[:5]]
            return QualityGateResult(
                gate_name=GATE,
                passed=False,
                severity="critical",
                details=(
                    f"StopLossRule(pct={rule.pct}) violated: {len(offenders)} "
                    f"trade(s) below floor {floor_pct:.2f}%. Sample "
                    f"(trade_num, return_pct)={sample}."
                ),
            )
        return QualityGateResult(
            gate_name=GATE,
            passed=True,
            severity="info",
            details=(
                f"StopLossRule(pct={rule.pct}) satisfied across "
                f"{len(trades)} trade(s); floor={floor_pct:.2f}%."
            ),
        )

    @staticmethod
    def _check_take_profit(
        rule: TakeProfitRule,
        trades: Sequence[TradeRecord],
        all_rules: Sequence[ExitRule],
    ) -> QualityGateResult:
        # Take-profit is hardest to assert on. The engine fires it whenever
        # bar.high >= entry * (1 + pct), but other rules (stop loss) can
        # close a trade first. So a passing trade ledger may legitimately
        # never hit the take-profit floor. We only flag the "lonely
        # take-profit" case: take-profit is the ONLY rule and zero trades
        # cleared its threshold.
        only_rule = len(all_rules) == 1
        if not only_rule:
            return QualityGateResult(
                gate_name=GATE,
                passed=True,
                severity="info",
                details=(
                    f"TakeProfitRule(pct={rule.pct}) co-exists with other exit "
                    "rules; conformance is informational (other rules may close "
                    "trades before the take-profit threshold is reached)."
                ),
            )
        target_pct = rule.pct * 100.0
        if any(t.return_pct >= target_pct for t in trades):
            return QualityGateResult(
                gate_name=GATE,
                passed=True,
                severity="info",
                details=(
                    f"TakeProfitRule(pct={rule.pct}) reached on at least one of "
                    f"{len(trades)} trade(s)."
                ),
            )
        if not trades:
            return QualityGateResult(
                gate_name=GATE,
                passed=True,
                severity="info",
                details=(
                    f"TakeProfitRule(pct={rule.pct}) — zero trades produced; no firings to verify."
                ),
            )
        return QualityGateResult(
            gate_name=GATE,
            passed=False,
            severity="warning",
            details=(
                f"TakeProfitRule(pct={rule.pct}) is the only exit rule but no "
                f"trade reached the {target_pct:.2f}% threshold across "
                f"{len(trades)} trade(s)."
            ),
        )
