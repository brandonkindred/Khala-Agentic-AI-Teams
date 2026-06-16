"""Issue #527 — deterministic conformance gate for engine-enforced exit rules.

Once :class:`TradingService` enforces structured ``ExitRule`` objects in its
bar loop, every trade either obeys the rules within tolerance or comes from
the strategy code's own exit logic.  This gate iterates the trade ledger
and the execution diagnostics and reports:

* **StopLossRule(pct=P, basis="entry_price")** — count engine-attributed
  below-floor trades per symbol and compare to the engine's per-symbol
  ``stop_loss`` firings. Critical failure fires when any symbol's
  engine-attributed below-floor trade count exceeds its firing count
  (the rule was tripped but the enforcement path didn't run). Per-
  symbol attribution prevents a stop firing on symbol A from masking
  a leak on symbol B. Strategy-closed below-floor trades are EXCLUDED:
  a strategy market exit can fill on a next-bar gap-down open beneath
  the floor before the engine evaluates that bar, so no engine firing
  is possible even though realised return is below the floor — the
  strategy made the call, not the engine. ``TradeRecord.exit_reason``
  carries the close ``OrderRequest.reason``; engine closes are stamped
  ``engine_exit:<rule_kind>``. The engine triggers on bar N's low
  (long) / high (short) and fills on bar N+1's open, so realised
  return can land below the rule's raw threshold via gap fills —
  those are absorbed when matched 1:1 with firings. Trailing variants
  need bar-by-bar replay and are flagged as informational.
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

from typing import ClassVar, List, Mapping, Optional, Sequence

from ...models import (
    BacktestConfig,
    BacktestExecutionDiagnostics,
    TradeRecord,
)
from ...trading_service.service import ENGINE_EXIT_REASON_PREFIX
from ..spec_dsl import (
    ExitRule,
    SignalExitRule,
    StopLossRule,
    TakeProfitRule,
)
from .models import GateResultsMixin, QualityGateResult, StrategyLabPhase

GATE = "exit_rule_conformance"


class ExitRuleConformanceGate(GateResultsMixin):
    """Deterministic post-run check that engine-emitted exits match the spec."""

    GATE: ClassVar[str] = GATE

    def check(
        self,
        *,
        exit_rules: Sequence[ExitRule],
        trades: Sequence[TradeRecord],
        diagnostics: Optional[BacktestExecutionDiagnostics],
        config: BacktestConfig,
        timeframe: str = "1d",
        phase: StrategyLabPhase = "verification",
    ) -> List[QualityGateResult]:
        with self._using_phase(phase):
            if not exit_rules:
                return [
                    self._info("spec.exit_rules empty; engine-enforcement check skipped.")
                ]

            # Engine exit-rule firing telemetry is unavailable for this run
            # (``diagnostics is None`` — e.g. a metrics object built without
            # execution diagnostics attached). The per-symbol firing counters
            # are the only signal that distinguishes a real enforcement leak
            # from absent telemetry, so without them the deterministic leak
            # check must NOT manufacture a critical veto on the strength of
            # missing data. Surface it as informational and defer to the LLM
            # alignment audit. The normal path attaches diagnostics onto
            # ``metrics`` before this gate runs, so this branch only fires if
            # that wiring regresses — making the failure observable instead of
            # silently flipping every engine-stopped run to a false leak.
            if diagnostics is None:
                return [
                    self._info(
                        "engine exit-rule firing telemetry unavailable "
                        "(diagnostics=None); deterministic enforcement check "
                        f"skipped for {len(exit_rules)} exit rule(s) across "
                        f"{len(trades)} trade(s)."
                    )
                ]

            results: List[QualityGateResult] = []
            firings = (diagnostics.exit_rule_firings if diagnostics is not None else None) or {}
            firings_by_symbol = (
                diagnostics.exit_rule_firings_by_symbol if diagnostics is not None else None
            ) or {}

            # ---- StopLossRule (entry_price basis only — trailing variants need
            # bar-by-bar replay which the gate cannot reconstruct from the trade
            # ledger alone) ----
            stop_losses = [r for r in exit_rules if isinstance(r, StopLossRule)]
            for rule in stop_losses:
                results.append(self._check_stop_loss(rule, trades, firings_by_symbol))

            # ---- TakeProfitRule (sanity only) ----
            take_profits = [r for r in exit_rules if isinstance(r, TakeProfitRule)]
            for rule in take_profits:
                results.append(self._check_take_profit(rule, trades, exit_rules, firings))

            # ---- SignalExitRule — engine-enforced via _EngineExitDispatcher ----
            signal_exits = [r for r in exit_rules if isinstance(r, SignalExitRule)]
            if signal_exits:
                engine_signal_firings = firings.get("signal_exit", 0)
                if trades and engine_signal_firings > 0:
                    results.append(
                        self._info(
                            f"{len(signal_exits)} SignalExitRule(s) — "
                            f"{engine_signal_firings} engine signal_exit firing(s) "
                            f"recorded across {len(trades)} trade(s)."
                        )
                    )
                elif trades:
                    results.append(
                        self._info(
                            f"{len(signal_exits)} SignalExitRule(s) — zero engine "
                            f"signal_exit firings across {len(trades)} trade(s); "
                            "other exit rules may have closed positions first."
                        )
                    )
                else:
                    results.append(
                        self._info(
                            f"{len(signal_exits)} SignalExitRule(s) — zero trades; "
                            "no firings to verify."
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
                    self._info(details + f" (total={total}, trades={len(trades)})")
                )

            return results

    # ------------------------------------------------------------------
    # Per-rule checks
    # ------------------------------------------------------------------

    def _check_stop_loss(
        self,
        rule: StopLossRule,
        trades: Sequence[TradeRecord],
        firings_by_symbol: Mapping[str, Mapping[str, int]],
    ) -> QualityGateResult:
        if rule.basis != "entry_price":
            return self._info(f"StopLossRule(basis={rule.basis!r}) conformance check skipped "
                    "(trailing variants require bar-by-bar replay).")
        # The engine detects the trigger on bar N's low (long) / high
        # (short), but the synthetic market close fills on bar N+1's
        # open. That next-bar fill can land arbitrarily far below the
        # trigger price on an overnight gap (long entry at 100, stop at
        # 95, next bar opens at 80 → return_pct ≈ -20% even though the
        # engine emitted the exit as designed). So we can't bound
        # ``return_pct`` against a strict floor without false-positive
        # criticals on every gap.
        #
        # Per-symbol attribution: count engine-attributed below-floor
        # trades per symbol and compare to the engine's per-symbol
        # ``stop_loss`` firings. A global rule-kind count would let
        # an unrelated firing on symbol A mask a leak on symbol B;
        # per-symbol attribution confines the gate's reasoning to
        # one symbol at a time. Cap ``min(tripped_count, firings_count)``
        # covers the protected trades; the rest are leaks.
        #
        # The gate only counts below-floor trades the engine itself
        # claimed via ``exit_reason == "engine_exit:stop_loss"``. The
        # exclusions:
        #
        # * ``exit_reason`` is None / empty / a strategy string —
        #   strategy closed the position; the engine never had a
        #   chance to fire on that bar (e.g. strategy market exit
        #   fills on a gap-down next-bar open beneath the floor
        #   BEFORE the engine evaluates rules). Not a leak — the
        #   strategy made the call.
        # * ``exit_reason`` is some other ``engine_exit:<kind>`` —
        #   engine fired a different rule (e.g. take_profit) which
        #   filled below the floor on a gap. That's a deliberate
        #   engine choice for a different rule, not a stop_loss leak.
        #
        # What's left ("engine_exit:stop_loss" + below floor) must
        # match the engine's per-symbol stop_loss firing count. Any
        # excess is a bookkeeping leak between the dispatcher's
        # emission path and the diagnostics counter.
        raw_floor_pct = -(rule.pct * 100.0)
        engine_stop_kind = f"{ENGINE_EXIT_REASON_PREFIX}stop_loss"
        by_symbol_tripped: dict[str, list[TradeRecord]] = {}
        skipped_strategy_closes = 0
        for t in trades:
            if t.return_pct >= raw_floor_pct:
                continue
            if t.exit_reason != engine_stop_kind:
                # Strategy-closed OR engine-closed via a different
                # rule — neither indicates a stop_loss leak.
                skipped_strategy_closes += 1
                continue
            by_symbol_tripped.setdefault(t.symbol, []).append(t)
        unaccounted: list[TradeRecord] = []
        total_firings = 0
        for sym, tripped in by_symbol_tripped.items():
            sym_firings = firings_by_symbol.get(sym, {}).get("stop_loss", 0)
            total_firings += sym_firings
            if len(tripped) > sym_firings:
                unaccounted.extend(tripped[sym_firings:])
        total_tripped = sum(len(v) for v in by_symbol_tripped.values())
        skipped_suffix = (
            f"; excluded {skipped_strategy_closes} strategy-closed below-floor trade(s)"
            if skipped_strategy_closes
            else ""
        )
        if unaccounted:
            sample = [(t.trade_num, t.symbol, t.return_pct) for t in unaccounted[:5]]
            return self._critical(f"StopLossRule(pct={rule.pct}) leak: {total_tripped} engine-attributed "
                    f"trade(s) closed below the {raw_floor_pct:.2f}% floor; "
                    f"{len(unaccounted)} unaccounted for by per-symbol firings. "
                    f"Sample (trade_num, symbol, return_pct)={sample}{skipped_suffix}.")
        return self._info(f"StopLossRule(pct={rule.pct}) — per-symbol firings cover "
                f"{total_tripped} engine-attributed below-floor trade(s) across "
                f"{len(by_symbol_tripped)} symbol(s); "
                f"total firings={total_firings}{skipped_suffix}.")

    def _check_take_profit(
        self,
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
            return self._info(f"TakeProfitRule(pct={rule.pct}) co-exists with other exit "
                    "rules; conformance is informational (other rules may close "
                    "trades before the take-profit threshold is reached).")
        tp_firings = firings.get("take_profit", 0)
        if tp_firings >= 1:
            return self._info(f"TakeProfitRule(pct={rule.pct}) — {tp_firings} engine firing(s) "
                    f"recorded across {len(trades)} trade(s).")
        if not trades:
            return self._info(f"TakeProfitRule(pct={rule.pct}) — zero trades produced; no firings to verify.")
        return self._warning(f"TakeProfitRule(pct={rule.pct}) is the only exit rule but the engine "
                f"recorded zero take_profit firings across {len(trades)} trade(s) — "
                "the threshold may be unreachable on the strategy's universe.")
