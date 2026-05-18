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
            results.append(self._check_take_profit(rule, trades, exit_rules, firings))

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
        # Per-trade comparison: count trades whose return cleared the
        # rule's raw threshold (no slippage tolerance) and compare to
        # the recorded ``stop_loss`` firing count. If the number of
        # below-floor trades EXCEEDS the firing count, at least one
        # trade fell through without engine enforcement — that's a
        # leak. ``stop_firings >= tripped`` means each below-floor
        # trade is plausibly accounted for by a matching engine
        # emission (and the residual return shortfall is a gap fill, not
        # a leak). Pure trade-count comparison can't tell which
        # specific trade leaked, but it can't mask leaks behind
        # unrelated firings either.
        raw_floor_pct = -(rule.pct * 100.0)
        tripped = [t for t in trades if t.return_pct < raw_floor_pct]
        stop_firings = firings.get("stop_loss", 0)
        if len(tripped) > stop_firings:
            leak_count = len(tripped) - stop_firings
            sample = [(t.trade_num, t.return_pct) for t in tripped[:5]]
            return QualityGateResult(
                gate_name=GATE,
                passed=False,
                severity="critical",
                details=(
                    f"StopLossRule(pct={rule.pct}) leak: {len(tripped)} trade(s) "
                    f"closed below the {raw_floor_pct:.2f}% floor but the engine "
                    f"recorded only {stop_firings} stop_loss firing(s) "
                    f"({leak_count} unaccounted-for trade(s)). Sample "
                    f"(trade_num, return_pct)={sample}."
                ),
            )
        return QualityGateResult(
            gate_name=GATE,
            passed=True,
            severity="info",
            details=(
                f"StopLossRule(pct={rule.pct}) — "
                f"{stop_firings} engine firing(s) covers "
                f"{len(tripped)} below-floor trade(s) (gap-fill expected on next-bar opens)."
            ),
        )

    @staticmethod
    def _check_take_profit(
        rule: TakeProfitRule,
        trades: Sequence[TradeRecord],
        all_rules: Sequence[ExitRule],
        firings: Mapping[str, int],
    ) -> QualityGateResult:
        # Take-profit is hardest to assert on. The engine fires it whenever
        # ``bar.high >= entry * (1 + pct)``, but the synthetic close fills
        # on bar N+1's open — gaps and slippage can land the realised
        # ``return_pct`` below the rule's raw target even on a valid
        # firing. And when other exit rules (stop loss) co-exist, they
        # can close a trade first.
        #
        # The robust signal is the engine ``take_profit`` firings counter:
        # at least one emission means the rule did its job. We only flag
        # the "lonely take-profit" case: take-profit is the ONLY rule,
        # trades exist, and the engine never emitted any take_profit
        # close — meaning the rule was either misconfigured or the
        # threshold is unreachable on the symbols' actual price action.
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
        tp_firings = firings.get("take_profit", 0)
        if tp_firings >= 1:
            return QualityGateResult(
                gate_name=GATE,
                passed=True,
                severity="info",
                details=(
                    f"TakeProfitRule(pct={rule.pct}) — {tp_firings} engine firing(s) "
                    f"recorded across {len(trades)} trade(s)."
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
                f"TakeProfitRule(pct={rule.pct}) is the only exit rule but the engine "
                f"recorded zero take_profit firings across {len(trades)} trade(s) — "
                "the threshold may be unreachable on the strategy's universe."
            ),
        )
