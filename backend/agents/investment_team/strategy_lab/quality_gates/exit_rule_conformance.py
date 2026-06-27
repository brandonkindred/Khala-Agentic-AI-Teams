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
  cannot reconstruct their moving floor from the ledger alone, so the
  static-floor leak check is not run for them by default; their firing
  counts surface via ``exit_rule_firings_by_basis``. When
  ``config.exit_rule_trailing_replay_enabled`` is set and cached bars are
  supplied via ``market_data``, an opt-in bar-by-bar replay
  (``_check_stop_loss_trailing_replay``) reconstructs the per-bar
  watermark and raises a ``warning`` (never ``critical``) if a trailing
  stop should have fired but did not. Execution correctness is also
  guaranteed deterministically by ``tests/test_trailing_stop.py``.
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

from ...market_data_service import OHLCVBar
from ...models import (
    BacktestConfig,
    BacktestExecutionDiagnostics,
    TradeRecord,
    scaled_level_key,
)
from ...trading_service.service import ENGINE_EXIT_REASON_PREFIX
from ..executor.rule_compiler import BarSnapshot, PositionState, _stop_loss_triggers
from ..spec_dsl import (
    ExitRule,
    ScaledTakeProfitRule,
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
        market_data: Optional[Mapping[str, Sequence[OHLCVBar]]] = None,
    ) -> List[QualityGateResult]:
        with self._using_phase(phase):
            if not exit_rules:
                return [self._info("spec.exit_rules empty; engine-enforcement check skipped.")]

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
            firings = diagnostics.exit_rule_firings or {}
            firings_by_symbol = diagnostics.exit_rule_firings_by_symbol or {}

            # ---- StopLossRule (entry_price basis only — trailing variants need
            # bar-by-bar replay which the gate cannot reconstruct from the trade
            # ledger alone) ----
            stop_losses = [r for r in exit_rules if isinstance(r, StopLossRule)]
            for rule in stop_losses:
                results.append(
                    self._check_stop_loss(
                        rule, trades, firings_by_symbol, config=config, market_data=market_data
                    )
                )

            # ---- TakeProfitRule (sanity only) ----
            take_profits = [r for r in exit_rules if isinstance(r, TakeProfitRule)]
            for rule in take_profits:
                results.append(self._check_take_profit(rule, trades, exit_rules, firings))

            # ---- ScaledTakeProfitRule (sanity + per-rung telemetry) ----
            level_firings = diagnostics.scaled_take_profit_level_firings or {}
            scaled_take_profits = [
                (idx, r) for idx, r in enumerate(exit_rules) if isinstance(r, ScaledTakeProfitRule)
            ]
            for idx, rule in scaled_take_profits:
                results.append(
                    self._check_scaled_take_profit(idx, rule, trades, exit_rules, level_firings)
                )

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
            firings = diagnostics.exit_rule_firings or {}
            total = sum(firings.values())
            details = "engine_exits: " + (
                ", ".join(f"{k}={v}" for k, v in sorted(firings.items())) or "none"
            )
            results.append(self._info(details + f" (total={total}, trades={len(trades)})"))

            # ---- Additive telemetry: per-basis firing breakdown so a trailing
            # stop fire is distinguishable from a fixed stop fire, plus any
            # stop-limit triggers that gapped through their limit unfilled. ----
            by_basis = diagnostics.exit_rule_firings_by_basis or {}
            if by_basis:
                basis_details = ", ".join(f"{k}={v}" for k, v in sorted(by_basis.items()))
                results.append(self._info(f"engine_exits_by_basis: {basis_details}"))
            # Fill counts (engine exits that actually closed a position), surfaced
            # as telemetry alongside the emission firings above so a limit-style
            # stop's fire-vs-fill divergence is observable. Not used for the leak
            # check: a fill count derived from the trade ledger cannot detect a
            # leak in that same ledger (see ``_check_stop_loss``).
            fills = diagnostics.exit_rule_fills or {}
            if fills:
                fill_details = ", ".join(f"{k}={v}" for k, v in sorted(fills.items()))
                results.append(self._info(f"engine_exit_fills: {fill_details}"))
            unfilled = diagnostics.stop_limit_unfilled_triggers
            if unfilled:
                results.append(
                    self._info(
                        f"stop_limit_unfilled_triggers={unfilled} (triggered stop-limit "
                        "orders that gapped through their limit; position stayed open — "
                        "intended stop-limit behavior, not a leak)."
                    )
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
        *,
        config: BacktestConfig,
        market_data: Optional[Mapping[str, Sequence[OHLCVBar]]] = None,
    ) -> QualityGateResult:
        """Reconcile engine-attributed below-floor trades against per-symbol
        ``stop_loss`` emission firings.

        Preconditions: ``rule`` is a ``StopLossRule``; ``trades`` is the run's
        closed-trade ledger; ``firings_by_symbol`` is the emission-time per-symbol
        firing telemetry (``symbol -> rule_kind -> count``). ``config`` is the run
        config; ``market_data`` (when provided) maps symbol -> ascending bar series.
        Postconditions: returns a critical result iff some symbol's
        ``engine_exit:stop_loss`` below-floor trade count exceeds its emission
        firing count (a real enforcement/bookkeeping leak); otherwise an info
        result. Holds for both ``style`` values — see the note below on why firings
        (not fills) is the reconciliation denominator even for limit-style stops.

        Trailing-basis rules (``trailing_high`` / ``trailing_low``) take a separate
        path: by default they are skipped (info), because the static-floor leak
        check cannot reconstruct the per-bar watermark from the ledger alone. When
        ``config.exit_rule_trailing_replay_enabled`` is set AND ``market_data`` is
        available, they are routed to :meth:`_check_stop_loss_trailing_replay`,
        which reconstructs the watermark bar-by-bar and surfaces a ``warning``
        (never a ``critical``) on divergence.

        Reconciliation denominator. We compare against emission *firings*, which
        come from ``_record_emission`` independently of the trade ledger — the
        only signal that can reveal a discrepancy between what the engine emitted
        and what closed. A fill-based count cannot serve here: it is derived from
        the same ``closed_trades`` ledger the gate iterates, so it would always be
        ``>= tripped`` and the leak branch would be unreachable. Firing-based
        reconciliation already tolerates a limit-style "fired but did not fill":
        a non-filling stop-limit increments firings but produces no trade, only
        inflating the denominator (more lenient), never a false critical. (The
        per-symbol ``exit_rule_fills`` divergence is surfaced as telemetry in
        ``check``.)
        """
        if rule.basis != "entry_price":
            if config.exit_rule_trailing_replay_enabled and market_data:
                return self._check_stop_loss_trailing_replay(rule, trades, market_data)
            return self._info(
                f"StopLossRule(basis={rule.basis!r}) ledger-leak check not run "
                "(trailing variants require bar-by-bar replay the post-hoc gate "
                "cannot reconstruct from the ledger alone; enable "
                "config.exit_rule_trailing_replay_enabled with cached bars to opt "
                "in). Trailing-stop execution correctness is covered "
                "deterministically by tests/test_trailing_stop.py; the per-basis "
                "firing counts above show how often it fired."
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
        # emission path and the diagnostics counter. Firing-based for both
        # styles (see method docstring): a limit-style non-fill only inflates
        # the firing denominator, never causing a false leak.
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
            return self._critical(
                f"StopLossRule(pct={rule.pct}) leak: {total_tripped} engine-attributed "
                f"trade(s) closed below the {raw_floor_pct:.2f}% floor; "
                f"{len(unaccounted)} unaccounted for by per-symbol firings. "
                f"Sample (trade_num, symbol, return_pct)={sample}{skipped_suffix}."
            )
        limit_suffix = (
            " (limit-style: fired-but-unfilled gap-throughs tolerated — firings may exceed fills)"
            if rule.style == "limit"
            else ""
        )
        return self._info(
            f"StopLossRule(pct={rule.pct}) — per-symbol firings cover "
            f"{total_tripped} engine-attributed below-floor trade(s) across "
            f"{len(by_symbol_tripped)} symbol(s); "
            f"total firings={total_firings}{skipped_suffix}{limit_suffix}."
        )

    def _check_stop_loss_trailing_replay(
        self,
        rule: StopLossRule,
        trades: Sequence[TradeRecord],
        market_data: Mapping[str, Sequence[OHLCVBar]],
    ) -> QualityGateResult:
        """Opt-in bar-by-bar replay verifying a trailing stop fired on time.

        The static-floor leak check cannot run for a trailing stop because the
        floor (``high_since_entry * (1 - pct)`` long / ``low_since_entry *
        (1 + pct)`` short) moves every bar and the trade ledger does not carry the
        per-bar watermark. This replay reconstructs the watermark from cached bars
        and asks the SAME geometry the executor uses
        (:func:`_stop_loss_triggers`) whether the floor was breached on a bar
        strictly before the position actually closed — i.e. the engine had a bar
        to emit the close AND a following bar to fill it, yet the position stayed
        open. That is a trailing-stop leak.

        Watermark reconstruction mirrors ``TradingService._extend_watermarks``:
        the watermark starts at ``entry_price`` and is extended with each bar's
        high/low AFTER that bar's rule evaluation, so a trailing rule never fires
        off a floor that moved up on the same bar's high (the executor's
        intrabar-lookahead guard). A legitimate next-bar gap fill trips its
        trigger on the bar immediately before the fill bar (no EARLIER breach), so
        it produces no warning — that is what keeps gap fills from becoming false
        positives.

        Preconditions: ``rule.basis`` is ``trailing_high`` or ``trailing_low``;
        ``market_data`` maps symbol -> bars in ascending date order.
        Postconditions: returns a ``warning`` (never ``critical``) listing a sample
        of leaking ``(trade_num, symbol, first_breach_date)`` when any matching
        trade's floor was breached with a fill bar to spare; otherwise an info
        result summarizing how many trades/symbols were replayed and how many were
        skipped (a trade whose symbol or entry/exit date is absent from the bar
        series cannot be replayed). Severity is capped at ``warning`` by contract:
        the ledger cannot reveal entry order type or scale-in re-basing, so the
        reconstructed watermark is an approximation and must not veto a run.
        """
        # Pair the rule with the side it governs: ``trailing_high`` ratchets a
        # long's floor up off the running high; ``trailing_low`` ratchets a
        # short's cap down off the running low. ``_stop_loss_triggers`` already
        # no-ops the mismatched side, but filtering keeps the skip accounting
        # honest and avoids wasted replays.
        applicable_side = "long" if rule.basis == "trailing_high" else "short"
        candidates = [t for t in trades if t.side == applicable_side]

        leaks: list[tuple[int, str, str]] = []
        replayed = 0
        skipped = 0
        symbols_seen: set[str] = set()
        for t in candidates:
            bars = market_data.get(t.symbol)
            if not bars:
                skipped += 1
                continue
            date_to_idx = {b.date: i for i, b in enumerate(bars)}
            entry_i = date_to_idx.get(t.entry_date)
            exit_i = date_to_idx.get(t.exit_date)
            if entry_i is None or exit_i is None or exit_i < entry_i:
                skipped += 1
                continue
            replayed += 1
            symbols_seen.add(t.symbol)
            hi = t.entry_price
            lo = t.entry_price
            # Decision bars are those strictly before the fill bar (``exit_i``):
            # a trigger on bar ``i`` lets the engine fill on bar ``i + 1``.
            for i in range(entry_i, exit_i):
                bar = bars[i]
                position = PositionState(
                    symbol=t.symbol,
                    side=applicable_side,  # type: ignore[arg-type]
                    qty=1.0,
                    entry_price=t.entry_price,
                    high_since_entry=hi,
                    low_since_entry=lo,
                )
                snapshot = BarSnapshot(high=bar.high, low=bar.low, close=bar.close)
                if _stop_loss_triggers(rule, position, snapshot):
                    # Breach on bar ``i`` → engine should fill on ``i + 1``. A leak
                    # only if the position survived past that fill opportunity
                    # (actual fill bar ``exit_i`` is strictly later).
                    if i + 1 < exit_i:
                        leaks.append((t.trade_num, t.symbol, bar.date))
                    break
                # Extend the watermark AFTER evaluation (deferred, as the engine
                # does) so the next bar sees this bar's extreme but this bar does
                # not fire off its own freshly-raised floor.
                hi = max(hi, bar.high)
                lo = min(lo, bar.low)

        skipped_suffix = (
            f"; skipped {skipped} trade(s) with no matching bar window" if skipped else ""
        )
        if leaks:
            return self._warning(
                f"StopLossRule(basis={rule.basis!r}, pct={rule.pct}) trailing replay: "
                f"{len(leaks)} {applicable_side} trade(s) breached the trailing floor on a "
                f"bar before the position closed but the engine did not fire in time. "
                f"Sample (trade_num, symbol, first_breach_date)={leaks[:5]}"
                f"{skipped_suffix}."
            )
        return self._info(
            f"StopLossRule(basis={rule.basis!r}, pct={rule.pct}) trailing replay: "
            f"no leaks across {replayed} {applicable_side} trade(s) over "
            f"{len(symbols_seen)} symbol(s){skipped_suffix}."
        )

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
            return self._info(
                f"TakeProfitRule(pct={rule.pct}) co-exists with other exit "
                "rules; conformance is informational (other rules may close "
                "trades before the take-profit threshold is reached)."
            )
        tp_firings = firings.get("take_profit", 0)
        if tp_firings >= 1:
            return self._info(
                f"TakeProfitRule(pct={rule.pct}) — {tp_firings} engine firing(s) "
                f"recorded across {len(trades)} trade(s)."
            )
        if not trades:
            return self._info(
                f"TakeProfitRule(pct={rule.pct}) — zero trades produced; no firings to verify."
            )
        return self._warning(
            f"TakeProfitRule(pct={rule.pct}) is the only exit rule but the engine "
            f"recorded zero take_profit firings across {len(trades)} trade(s) — "
            "the threshold may be unreachable on the strategy's universe."
        )

    def _check_scaled_take_profit(
        self,
        rule_index: int,
        rule: ScaledTakeProfitRule,
        trades: Sequence[TradeRecord],
        all_rules: Sequence[ExitRule],
        level_firings: Mapping[str, int],
    ) -> QualityGateResult:
        """Sanity + per-rung telemetry for a laddered take-profit.

        Like :meth:`_check_take_profit`, a scaled take-profit is hard to assert on
        post-hoc: each rung fires when ``bar.high >= entry*(1+pct)`` (long) but the
        partial close fills next bar, and co-existing exits (a stop) can close the
        position before later rungs reach their target. So this is informational —
        it reports how many times each rung scaled out — and only WARNs the lonely
        case (every exit rule is a scaled ladder, trades exist, yet NO rung of ANY
        ladder fired across the whole strategy).

        Preconditions: ``rule`` is a ``ScaledTakeProfitRule`` at ``rule_index`` in
        ``all_rules``; ``level_firings`` is keyed ``"<rule_index>:<level_index>"``
        across EVERY ladder in the strategy (not just this one).
        Postconditions: returns an info result with per-rung counts, or a warning
        when the strategy has no non-ladder exit (every rule in ``all_rules`` is a
        ``ScaledTakeProfitRule``), a non-empty trade ledger exists, and not a single
        rung of any ladder fired strategy-wide.
        """
        per_rung = {
            level_idx: level_firings.get(scaled_level_key(rule_index, level_idx), 0)
            for level_idx in range(len(rule.levels))
        }
        total_firings = sum(per_rung.values())
        rung_details = ", ".join(
            f"L{level_idx}(@{rule.levels[level_idx].pct}, "
            f"{rule.levels[level_idx].qty_fraction})={count}"
            for level_idx, count in sorted(per_rung.items())
        )
        # WARN only when EVERY exit rule is a scaled ladder AND not a single rung of
        # ANY ladder fired strategy-wide — the position then relies entirely on
        # rungs reaching their targets, and none did. The condition is whole-strategy
        # (``sum(level_firings.values())``), NOT this ladder's ``total_firings``:
        # with multiple ladders, one ladder can carry the exits while another's
        # higher rungs legitimately never reach their target, so a per-ladder zero
        # is a false alarm. With any non-ladder exit (stop / take-profit / signal)
        # present, zero rung firings is acceptable (that exit may close trades
        # first), so this stays informational. The per-rung counts below stay
        # informational in every non-warning case.
        only_scaled = all(isinstance(r, ScaledTakeProfitRule) for r in all_rules)
        strategy_rung_firings = sum(level_firings.values())
        if strategy_rung_firings >= 1 or not trades or not only_scaled:
            coexist = "" if only_scaled else " (co-exists with other exit rules)"
            return self._info(
                f"ScaledTakeProfitRule[{rule_index}] — {total_firings} rung "
                f"firing(s) across {len(trades)} trade(s){coexist}; "
                f"per-rung: {rung_details}."
            )
        return self._warning(
            f"ScaledTakeProfitRule[{rule_index}] recorded zero rung firings across "
            f"{len(trades)} trade(s); the strategy's only exits are scaled ladders "
            "and no ladder rung fired strategy-wide, so the rung targets may be "
            "unreachable on the strategy's universe."
        )
