"""Issue #527 — deterministic conformance gate for engine-enforced exit rules.

Once :class:`TradingService` enforces structured ``ExitRule`` objects in its
bar loop, every trade either obeys the rules within tolerance or comes from
the strategy code's own exit logic.  This gate iterates the trade ledger
and the execution diagnostics and reports:

* **StopLossRule(pct=P, basis="entry_price")** — count trades whose
  return cleared the raw ``-pct`` floor. The engine detects the trigger
  on bar N's low (long) / high (short) but the synthetic close fills on
  bar N+1's open, so an overnight gap can land the realised return
  arbitrarily far below the rule's threshold without indicating an
  enforcement bug. Critical failure fires only when below-floor trades
  exist *and* the engine never emitted any ``stop_loss`` close — i.e.
  the rule was tripped but the enforcement path didn't run. Trailing
  variants need bar-by-bar replay and are flagged as informational.
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

from typing import List, Mapping, Optional, Sequence

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
        firings = (diagnostics.exit_rule_firings if diagnostics is not None else None) or {}

        # ---- StopLossRule (entry_price basis only — trailing variants need
        # bar-by-bar replay which the gate cannot reconstruct from the trade
        # ledger alone) ----
        stop_losses = [r for r in exit_rules if isinstance(r, StopLossRule)]
        for rule in stop_losses:
            results.append(self._check_stop_loss(rule, trades, firings))

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
        firings: Mapping[str, int],
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
        # The engine detects the trigger on bar N's low (long) / high
        # (short), but the synthetic market close fills on bar N+1's
        # open. That next-bar fill can land arbitrarily far below the
        # trigger price on an overnight gap (long entry at 100, stop at
        # 95, next bar opens at 80 → return_pct ≈ -20% even though the
        # engine emitted the exit as designed). So we can't bound
        # ``return_pct`` against a strict floor without false-positive
        # criticals on every gap.
        #
        # Instead: count trades whose return clearly tripped the rule's
        # raw threshold (no slippage tolerance). If those exist AND the
        # engine never emitted any ``stop_loss`` close, that's a
        # genuine enforcement leak. Otherwise the firings counter
        # confirms the engine did its job and the residual gap is a
        # real-world market-execution cost, not a conformance bug.
        raw_floor_pct = -(rule.pct * 100.0)
        tripped = [t for t in trades if t.return_pct < raw_floor_pct]
        stop_firings = firings.get("stop_loss", 0)
        if tripped and stop_firings == 0:
            sample = [(t.trade_num, t.return_pct) for t in tripped[:5]]
            return QualityGateResult(
                gate_name=GATE,
                passed=False,
                severity="critical",
                details=(
                    f"StopLossRule(pct={rule.pct}) leak: {len(tripped)} trade(s) "
                    f"closed below the {raw_floor_pct:.2f}% floor but the engine "
                    "never emitted a stop_loss exit. Sample "
                    f"(trade_num, return_pct)={sample}."
                ),
            )
        return QualityGateResult(
            gate_name=GATE,
            passed=True,
            severity="info",
            details=(
                f"StopLossRule(pct={rule.pct}) — "
                f"{stop_firings} engine firing(s) recorded; "
                f"{len(tripped)} trade(s) below raw floor (gap-fill expected on next-bar opens)."
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
