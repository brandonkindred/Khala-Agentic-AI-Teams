"""Unit tests for ``CodeConformanceGate`` (issue #541).

Hand-built ``StrategySpec`` + strategy-code fixtures exercise each of the
nine conformance checks in isolation, with at least one positive and one
negative case per check. The two mandatory acceptance tests from the
issue are kept distinct: dropping the entry submit_order triggers
check #3, and replacing the universe guard with ``True`` triggers
check #2.
"""

from __future__ import annotations

import textwrap

from investment_team.models import StrategySpec
from investment_team.strategy_lab.quality_gates.code_conformance import (
    CodeConformanceGate,
)
from investment_team.strategy_lab.spec_dsl import (
    DEFAULT_SIZING_PAYLOAD,
    EntryRule,
    IndicatorRef,
    Predicate,
    SignalExitRule,
    StopLossRule,
    TakeProfitRule,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _spec(
    *,
    entry_rules=None,
    exit_rules=None,
    target_symbols=None,
) -> StrategySpec:
    return StrategySpec(
        strategy_id="t-1",
        authored_by="test",
        asset_class="stocks",
        hypothesis="QQQ trend-follow on SMA cross.",
        signal_definition="sma(50) > sma(200)",
        timeframe="1d",
        entry_rules=list(entry_rules or []),
        exit_rules=list(exit_rules or []),
        sizing=DEFAULT_SIZING_PAYLOAD,
        target_symbols=list(target_symbols or []),
    )


def _sma_cross_entry(side: str = "long") -> EntryRule:
    return EntryRule(
        side=side,
        when=Predicate(
            lhs=IndicatorRef(name="sma", params={"period": 50}),
            op=">",
            rhs=IndicatorRef(name="sma", params={"period": 200}),
        ),
    )


def _rsi_signal_exit() -> SignalExitRule:
    return SignalExitRule(
        when=Predicate(
            lhs=IndicatorRef(name="rsi"),
            op=">",
            rhs=70.0,
        ),
    )


# Minimal valid strategy: SMA cross entry, RSI signal exit, stop-loss,
# universe guard, qty derived from ctx.equity. Every conformance check
# passes; mutated variants drop one piece to drive the negative tests.
_HAPPY_CODE = textwrap.dedent(
    """
    from contract import Strategy

    class S(Strategy):
        UNIVERSE = frozenset({"QQQ"})

        def on_bar(self, ctx, bar):
            if bar.symbol not in self.UNIVERSE:
                return
            bars = ctx.history(bar.symbol, 200)
            if len(bars) < 200:
                return
            fast = sma(bars, 50)
            slow = sma(bars, 200)
            r = rsi(bars, 14)
            pos = ctx.position(bar.symbol)
            qty = max(1, int(ctx.equity * 0.02 / bar.close))
            if pos is None and fast > slow:
                ctx.submit_order(symbol=bar.symbol, qty=qty, side="LONG")
            elif pos is not None and r > 70:
                ctx.submit_order(symbol=bar.symbol, qty=pos.qty, side="SHORT")
            elif pos is not None and bar.close < pos.entry_price * 0.95:
                ctx.submit_order(symbol=bar.symbol, qty=pos.qty, side="SHORT")
    """
)


def _happy_spec() -> StrategySpec:
    return _spec(
        entry_rules=[_sma_cross_entry()],
        exit_rules=[_rsi_signal_exit(), StopLossRule(pct=0.05)],
        target_symbols=["QQQ"],
    )


def _critical_details(results):
    return [r.details for r in results if r.severity == "critical" and not r.passed]


# ---------------------------------------------------------------------------
# Sanity
# ---------------------------------------------------------------------------


def test_happy_path_passes_every_check() -> None:
    results = CodeConformanceGate().check(_HAPPY_CODE, _happy_spec())
    assert _critical_details(results) == [], _critical_details(results)
    # Every result must carry the synthesis phase + the gate name.
    assert all(r.phase == "synthesis" for r in results)
    assert all(r.gate_name == "code_conformance" for r in results)


def test_syntax_error_is_critical() -> None:
    results = CodeConformanceGate().check("def broken(:\n", _happy_spec())
    assert len(results) == 1
    assert results[0].severity == "critical"
    assert "syntax error" in results[0].details.lower()


def test_multiple_strategy_classes_skips_with_info() -> None:
    code = textwrap.dedent(
        """
        from contract import Strategy
        class A(Strategy):
            def on_bar(self, ctx, bar): pass
        class B(Strategy):
            def on_bar(self, ctx, bar): pass
        """
    )
    results = CodeConformanceGate().check(code, _happy_spec())
    assert _critical_details(results) == []
    assert any("Skipped" in r.details for r in results)


# ---------------------------------------------------------------------------
# Check 1: indicator presence
# ---------------------------------------------------------------------------


def test_indicator_presence_passes_when_named_calls_exist() -> None:
    results = CodeConformanceGate().check(_HAPPY_CODE, _happy_spec())
    assert not any("indicator" in d.lower() for d in _critical_details(results))


def test_indicator_presence_fails_when_indicator_never_called() -> None:
    # Strip every sma() call by computing rolling mean inline — v1 does
    # NOT accept inline equivalents and must flag this.
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                bars = ctx.history(bar.symbol, 200)
                fast = sum(b.close for b in bars[-50:]) / 50
                slow = sum(b.close for b in bars[-200:]) / 200
                pos = ctx.position(bar.symbol)
                qty = max(1, int(ctx.equity * 0.02 / bar.close))
                if pos is None and fast > slow:
                    ctx.submit_order(symbol=bar.symbol, qty=qty, side="LONG")
                elif pos is not None and bar.close < pos.entry_price * 0.95:
                    ctx.submit_order(symbol=bar.symbol, qty=pos.qty, side="SHORT")
        """
    )
    spec = _spec(
        entry_rules=[_sma_cross_entry()],
        exit_rules=[StopLossRule(pct=0.05)],
        target_symbols=["QQQ"],
    )
    results = CodeConformanceGate().check(code, spec)
    crits = _critical_details(results)
    assert any("indicator" in c.lower() and "sma" in c for c in crits), crits


# ---------------------------------------------------------------------------
# Check 2: symbol gate
# ---------------------------------------------------------------------------


def test_symbol_gate_passes_with_universe_guard() -> None:
    results = CodeConformanceGate().check(_HAPPY_CODE, _happy_spec())
    assert not any("UNIVERSE" in d for d in _critical_details(results))


def test_symbol_gate_fails_when_universe_guard_replaced_with_true() -> None:
    """Mandatory acceptance test from the issue: swap the runtime
    membership check for ``True`` → critical failure on check #2."""
    code = _HAPPY_CODE.replace(
        "if bar.symbol not in self.UNIVERSE:",
        "if not True:",
    )
    results = CodeConformanceGate().check(code, _happy_spec())
    crits = _critical_details(results)
    assert any("UNIVERSE" in c and "guard" in c for c in crits), crits


def test_symbol_gate_fails_when_universe_constant_missing() -> None:
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                bars = ctx.history(bar.symbol, 200)
                fast = sma(bars, 50)
                slow = sma(bars, 200)
                r = rsi(bars, 14)
                pos = ctx.position(bar.symbol)
                qty = max(1, int(ctx.equity * 0.02 / bar.close))
                if pos is None and fast > slow:
                    ctx.submit_order(symbol=bar.symbol, qty=qty, side="LONG")
                elif pos is not None and r > 70:
                    ctx.submit_order(symbol=bar.symbol, qty=pos.qty, side="SHORT")
                elif pos is not None and bar.close < pos.entry_price * 0.95:
                    ctx.submit_order(symbol=bar.symbol, qty=pos.qty, side="SHORT")
        """
    )
    results = CodeConformanceGate().check(code, _happy_spec())
    crits = _critical_details(results)
    assert any("UNIVERSE" in c and "constant" in c for c in crits), crits


def test_symbol_gate_skipped_when_target_symbols_empty() -> None:
    # No UNIVERSE constant — but spec has no target_symbols, so check #2
    # should not fire.
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                bars = ctx.history(bar.symbol, 200)
                fast = sma(bars, 50)
                slow = sma(bars, 200)
                pos = ctx.position(bar.symbol)
                qty = max(1, int(ctx.equity * 0.02 / bar.close))
                if pos is None and fast > slow:
                    ctx.submit_order(symbol=bar.symbol, qty=qty, side="LONG")
                elif pos is not None and bar.close < pos.entry_price * 0.95:
                    ctx.submit_order(symbol=bar.symbol, qty=pos.qty, side="SHORT")
        """
    )
    spec = _spec(
        entry_rules=[_sma_cross_entry()],
        exit_rules=[StopLossRule(pct=0.05)],
        target_symbols=[],
    )
    results = CodeConformanceGate().check(code, spec)
    assert not any("UNIVERSE" in c for c in _critical_details(results))


# ---------------------------------------------------------------------------
# Check 3: entry coverage (the issue's other mandatory acceptance test)
# ---------------------------------------------------------------------------


def test_entry_coverage_passes_when_entry_branch_present() -> None:
    results = CodeConformanceGate().check(_HAPPY_CODE, _happy_spec())
    assert not any("entry rule" in d.lower() for d in _critical_details(results))


def test_entry_coverage_fails_when_entry_submit_order_dropped() -> None:
    """Mandatory acceptance test from the issue: drop the entry
    submit_order from a working strategy → critical failure on check #3.
    Uses requires_custom_code=True so the gate applies (engine-managed
    specs skip check #3)."""
    code = _HAPPY_CODE.replace(
        'ctx.submit_order(symbol=bar.symbol, qty=qty, side="LONG")',
        "pass  # entry submit_order removed",
    )
    spec = _happy_spec().model_copy(update={"requires_custom_code": True})
    results = CodeConformanceGate().check(code, spec)
    crits = _critical_details(results)
    assert any("entry rule" in c.lower() for c in crits), crits


def test_entry_coverage_ignores_unreachable_helper_branches() -> None:
    """Codex P1 from PR #588: a branch in an unused helper must not
    satisfy entry coverage. The submit_order in ``_dead`` is never
    executed because nothing in on_bar calls ``self._dead(...)``."""
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            def _dead(self, ctx, bar):
                # Unreachable from on_bar — must NOT satisfy entry coverage.
                if True:
                    ctx.submit_order(symbol=bar.symbol, qty=1, side="LONG")

            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                bars = ctx.history(bar.symbol, 200)
                fast = sma(bars, 50)
                slow = sma(bars, 200)
                qty = max(1, int(ctx.equity * 0.02 / bar.close))
                # No entry submit_order in any on_bar branch.
                pos = ctx.position(bar.symbol)
                if pos is not None and bar.close < pos.entry_price * 0.95:
                    ctx.submit_order(symbol=bar.symbol, qty=pos.qty, side="SHORT")
        """
    )
    spec = _spec(
        entry_rules=[_sma_cross_entry()],
        exit_rules=[StopLossRule(pct=0.05)],
        target_symbols=["QQQ"],
    )
    spec = spec.model_copy(update={"requires_custom_code": True})
    results = CodeConformanceGate().check(code, spec)
    crits = _critical_details(results)
    assert any("entry rule" in c.lower() and "reachable" in c.lower() for c in crits), crits


# ---------------------------------------------------------------------------
# Check 4: signal-exit coverage
# ---------------------------------------------------------------------------


def test_signal_exit_coverage_passes_with_position_qty_close() -> None:
    results = CodeConformanceGate().check(_HAPPY_CODE, _happy_spec())
    assert not any("signal-exit" in d.lower() for d in _critical_details(results))


def test_signal_exit_engine_owned_no_branch_required_for_custom_code() -> None:
    # Engine-owned-exits contract: custom code authors entries only; the
    # engine enforces every SignalExitRule. Entries-only code that declares
    # a signal exit must NOT raise any critical — and it need not read the
    # exit-only indicator (rsi), which the engine computes itself.
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                bars = ctx.history(bar.symbol, 200)
                fast = sma(bars, 50)
                slow = sma(bars, 200)
                pos = ctx.position(bar.symbol)
                qty = max(1, int(ctx.equity * 0.02 / bar.close))
                if pos is None and fast > slow:
                    ctx.submit_order(symbol=bar.symbol, qty=qty, side="LONG")
        """
    )
    spec = _spec(
        entry_rules=[_sma_cross_entry()],
        exit_rules=[_rsi_signal_exit(), StopLossRule(pct=0.05)],
        target_symbols=["QQQ"],
    )
    spec = spec.model_copy(update={"requires_custom_code": True})
    results = CodeConformanceGate().check(code, spec)
    crits = _critical_details(results)
    assert crits == [], crits


# ---------------------------------------------------------------------------
# Check 4b: no manual close duplicating an engine-owned exit
# ---------------------------------------------------------------------------


def test_no_duplicate_engine_exit_fires_on_manual_close() -> None:
    # Custom-code strategy whose engine-handled exits cover the long entry
    # side, yet it still submits a manual qty=pos.qty close. The engine
    # owns that exit, so the manual close is a critical.
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                bars = ctx.history(bar.symbol, 200)
                fast = sma(bars, 50)
                slow = sma(bars, 200)
                r = rsi(bars, 14)
                pos = ctx.position(bar.symbol)
                qty = max(1, int(ctx.equity * 0.02 / bar.close))
                if pos is None and fast > slow:
                    ctx.submit_order(symbol=bar.symbol, qty=qty, side="LONG")
                elif pos is not None and r > 70:
                    ctx.submit_order(symbol=bar.symbol, qty=pos.qty, side="SHORT")
        """
    )
    spec = _spec(
        entry_rules=[_sma_cross_entry()],
        exit_rules=[_rsi_signal_exit(), StopLossRule(pct=0.05)],
        target_symbols=["QQQ"],
    )
    spec = spec.model_copy(update={"requires_custom_code": True})
    results = CodeConformanceGate().check(code, spec)
    crits = _critical_details(results)
    assert any("manual position-closing order" in c.lower() for c in crits), crits


def test_no_duplicate_engine_exit_silent_for_compiled_path() -> None:
    # requires_custom_code defaults to False: the compiled path submits no
    # orders in production, so the duplicate-exit check must never fire even
    # when the (compiled-style) fixture code contains a qty=pos.qty close.
    results = CodeConformanceGate().check(_HAPPY_CODE, _happy_spec())
    crits = _critical_details(results)
    assert not any("manual position-closing order" in c.lower() for c in crits), crits


def test_no_duplicate_engine_exit_silent_when_exits_dont_cover_sides() -> None:
    # Long entry but the only exit rule (trailing_low stop) covers short
    # only, so the engine does not own the long exit — a manual close is
    # legitimate and must not be flagged.
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                bars = ctx.history(bar.symbol, 200)
                fast = sma(bars, 50)
                slow = sma(bars, 200)
                pos = ctx.position(bar.symbol)
                qty = max(1, int(ctx.equity * 0.02 / bar.close))
                if pos is None and fast > slow:
                    ctx.submit_order(symbol=bar.symbol, qty=qty, side="LONG")
                elif pos is not None and fast < slow:
                    ctx.submit_order(symbol=bar.symbol, qty=pos.qty, side="SHORT")
        """
    )
    spec = _spec(
        entry_rules=[_sma_cross_entry()],
        exit_rules=[StopLossRule(pct=0.05, basis="trailing_low")],
        target_symbols=["QQQ"],
    )
    spec = spec.model_copy(update={"requires_custom_code": True})
    results = CodeConformanceGate().check(code, spec)
    crits = _critical_details(results)
    assert not any("manual position-closing order" in c.lower() for c in crits), crits


def test_no_duplicate_engine_exit_detects_close_in_else_block() -> None:
    # The close lives in a plain ``else:`` block (the old template shape),
    # not an ``if`` branch — the scan must still catch it.
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                bars = ctx.history(bar.symbol, 200)
                fast = sma(bars, 50)
                slow = sma(bars, 200)
                pos = ctx.position(bar.symbol)
                qty = max(1, int(ctx.equity * 0.02 / bar.close))
                if pos is None and fast > slow:
                    ctx.submit_order(symbol=bar.symbol, qty=qty, side="LONG")
                else:
                    ctx.submit_order(symbol=bar.symbol, qty=pos.qty, side="SHORT")
        """
    )
    spec = _spec(
        entry_rules=[_sma_cross_entry()],
        exit_rules=[_rsi_signal_exit(), StopLossRule(pct=0.05)],
        target_symbols=["QQQ"],
    )
    spec = spec.model_copy(update={"requires_custom_code": True})
    results = CodeConformanceGate().check(code, spec)
    crits = _critical_details(results)
    assert any("manual position-closing order" in c.lower() for c in crits), crits


def test_no_duplicate_engine_exit_partial_coverage_flags_covered_side() -> None:
    # Two entry sides; the exit rule (trailing_high stop) covers LONG only.
    # A manual close of the long position (side=SHORT) duplicates the
    # engine-owned long exit and must be flagged, even though the short side
    # is not engine-covered.
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                bars = ctx.history(bar.symbol, 200)
                fast = sma(bars, 50)
                slow = sma(bars, 200)
                pos = ctx.position(bar.symbol)
                qty = max(1, int(ctx.equity * 0.02 / bar.close))
                if pos is None and fast > slow:
                    ctx.submit_order(symbol=bar.symbol, qty=qty, side="LONG")
                elif pos is None and fast < slow:
                    ctx.submit_order(symbol=bar.symbol, qty=qty, side="SHORT")
                elif pos is not None and pos.side == "long":
                    ctx.submit_order(symbol=bar.symbol, qty=pos.qty, side="SHORT")
        """
    )
    spec = _spec(
        entry_rules=[_sma_cross_entry("long"), _sma_cross_entry("short")],
        exit_rules=[StopLossRule(pct=0.05, basis="trailing_high")],
        target_symbols=["QQQ"],
    )
    spec = spec.model_copy(update={"requires_custom_code": True})
    results = CodeConformanceGate().check(code, spec)
    crits = _critical_details(results)
    assert any("manual position-closing order" in c.lower() for c in crits), crits


def test_no_duplicate_engine_exit_partial_coverage_allows_uncovered_side() -> None:
    # Same partial coverage (trailing_high stop = LONG only), but the manual
    # close retires a SHORT position (side=LONG). Short is not engine-owned,
    # so the close is legitimate and must NOT be flagged.
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                bars = ctx.history(bar.symbol, 200)
                fast = sma(bars, 50)
                slow = sma(bars, 200)
                pos = ctx.position(bar.symbol)
                qty = max(1, int(ctx.equity * 0.02 / bar.close))
                if pos is None and fast > slow:
                    ctx.submit_order(symbol=bar.symbol, qty=qty, side="LONG")
                elif pos is None and fast < slow:
                    ctx.submit_order(symbol=bar.symbol, qty=qty, side="SHORT")
                elif pos is not None and pos.side == "short":
                    ctx.submit_order(symbol=bar.symbol, qty=pos.qty, side="LONG")
        """
    )
    spec = _spec(
        entry_rules=[_sma_cross_entry("long"), _sma_cross_entry("short")],
        exit_rules=[StopLossRule(pct=0.05, basis="trailing_high")],
        target_symbols=["QQQ"],
    )
    spec = spec.model_copy(update={"requires_custom_code": True})
    results = CodeConformanceGate().check(code, spec)
    crits = _critical_details(results)
    assert not any("manual position-closing order" in c.lower() for c in crits), crits


def test_no_duplicate_engine_exit_ignores_same_side_scale_in() -> None:
    # Long-only spec with a take-profit (covers both sides). The strategy
    # doubles its long with a SAME-side qty=pos.qty order — a scale-in, not
    # a close. Its inferred closed side ("short") is not an entered side, so
    # it must NOT be flagged as a duplicate exit.
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                bars = ctx.history(bar.symbol, 200)
                fast = sma(bars, 50)
                slow = sma(bars, 200)
                pos = ctx.position(bar.symbol)
                qty = max(1, int(ctx.equity * 0.02 / bar.close))
                if pos is None and fast > slow:
                    ctx.submit_order(symbol=bar.symbol, qty=qty, side="LONG")
                elif pos is not None and fast > slow:
                    ctx.submit_order(symbol=bar.symbol, qty=pos.qty, side="LONG")
        """
    )
    spec = _spec(
        entry_rules=[_sma_cross_entry("long")],
        exit_rules=[TakeProfitRule(pct=0.1)],
        target_symbols=["QQQ"],
    )
    spec = spec.model_copy(update={"requires_custom_code": True})
    results = CodeConformanceGate().check(code, spec)
    crits = _critical_details(results)
    assert not any("manual position-closing order" in c.lower() for c in crits), crits


# ---------------------------------------------------------------------------
# Engine-delegated exits: the gate must NOT require the strategy to reference
# ``entry_price``. Stop-loss / take-profit thresholds are enforced
# deterministically by ``executor/rule_compiler.py`` against the engine-owned
# ``PositionState.entry_price``; conformant strategies delegate to the engine
# and never compute the threshold themselves.
# ---------------------------------------------------------------------------

# ``_HAPPY_CODE`` with its only ``pos.entry_price`` reference removed — the
# canonical shape of an engine-delegated strategy.
_NO_ENTRY_PRICE_CODE = _HAPPY_CODE.replace("pos.entry_price * 0.95", "0.0")


def test_stop_loss_passes_without_entry_price_reference() -> None:
    spec = _spec(
        entry_rules=[_sma_cross_entry()],
        exit_rules=[StopLossRule(pct=0.05)],
        target_symbols=["QQQ"],
    )
    results = CodeConformanceGate().check(_NO_ENTRY_PRICE_CODE, spec)
    assert not any("StopLossRule" in d for d in _critical_details(results))


def test_take_profit_passes_without_entry_price_reference() -> None:
    spec = _spec(
        entry_rules=[_sma_cross_entry()],
        exit_rules=[TakeProfitRule(pct=0.10)],
        target_symbols=["QQQ"],
    )
    results = CodeConformanceGate().check(_NO_ENTRY_PRICE_CODE, spec)
    assert not any("TakeProfitRule" in d for d in _critical_details(results))


# ---------------------------------------------------------------------------
# Check 5: bar-counting exit rejection
# ---------------------------------------------------------------------------


def test_bar_counting_exit_rejected_bars_held() -> None:
    code = (
        "from contract import OrderSide, OrderType, Strategy, TimeInForce\n"
        "from indicators import rsi\n\n"
        "class S(Strategy):\n"
        "    UNIVERSE = frozenset({'QQQ'})\n"
        "    WINDOW = 20\n"
        "    def on_bar(self, ctx, bar):\n"
        "        if ctx.is_warmup:\n"
        "            return\n"
        "        if bar.symbol not in self.UNIVERSE:\n"
        "            return\n"
        "        history = ctx.history(bar.symbol, self.WINDOW)\n"
        "        if len(history) < self.WINDOW:\n"
        "            return\n"
        "        pos = ctx.position(bar.symbol)\n"
        "        if pos is not None:\n"
        "            self.bars_held = getattr(self, 'bars_held', 0) + 1\n"
        "            if self.bars_held >= 10:\n"
        "                ctx.submit_order(symbol=bar.symbol, qty=pos.qty,\n"
        "                    side=OrderSide.SHORT, order_type=OrderType.MARKET,\n"
        "                    tif=TimeInForce.DAY, reason='time exit')\n"
        "        else:\n"
        "            closes = [b.close for b in history]\n"
        "            r = rsi(closes)\n"
        "            if r < 30:\n"
        "                qty = int((ctx.equity * 0.02) / bar.close)\n"
        "                if qty > 0:\n"
        "                    ctx.submit_order(symbol=bar.symbol, qty=qty,\n"
        "                        side=OrderSide.LONG, order_type=OrderType.MARKET,\n"
        "                        tif=TimeInForce.DAY, reason='rsi entry')\n"
    )
    results = CodeConformanceGate().check(code, _happy_spec())
    crits = [r for r in results if r.severity == "critical"]
    assert any("bar-counting" in r.details.lower() or "bars_held" in r.details for r in crits)


def test_bar_counting_exit_rejected_hold_count() -> None:
    code = (
        "from contract import OrderSide, OrderType, Strategy, TimeInForce\n"
        "from indicators import rsi\n\n"
        "class S(Strategy):\n"
        "    UNIVERSE = frozenset({'QQQ'})\n"
        "    WINDOW = 20\n"
        "    def on_bar(self, ctx, bar):\n"
        "        if ctx.is_warmup:\n"
        "            return\n"
        "        if bar.symbol not in self.UNIVERSE:\n"
        "            return\n"
        "        history = ctx.history(bar.symbol, self.WINDOW)\n"
        "        if len(history) < self.WINDOW:\n"
        "            return\n"
        "        pos = ctx.position(bar.symbol)\n"
        "        if pos is not None:\n"
        "            self.hold_count = getattr(self, 'hold_count', 0) + 1\n"
        "            if self.hold_count >= 5:\n"
        "                ctx.submit_order(symbol=bar.symbol, qty=pos.qty,\n"
        "                    side=OrderSide.SHORT, order_type=OrderType.MARKET,\n"
        "                    tif=TimeInForce.DAY, reason='hold exit')\n"
        "        else:\n"
        "            closes = [b.close for b in history]\n"
        "            r = rsi(closes)\n"
        "            if r < 30:\n"
        "                qty = int((ctx.equity * 0.02) / bar.close)\n"
        "                if qty > 0:\n"
        "                    ctx.submit_order(symbol=bar.symbol, qty=qty,\n"
        "                        side=OrderSide.LONG, order_type=OrderType.MARKET,\n"
        "                        tif=TimeInForce.DAY, reason='rsi entry')\n"
    )
    results = CodeConformanceGate().check(code, _happy_spec())
    crits = [r for r in results if r.severity == "critical"]
    assert any("bar-counting" in r.details.lower() or "hold_count" in r.details for r in crits)


def test_no_bar_counting_false_positive_on_clean_code() -> None:
    results = CodeConformanceGate().check(_HAPPY_CODE, _happy_spec())
    crits = [r for r in results if r.severity == "critical" and "bar-counting" in r.details.lower()]
    assert crits == []


def test_no_bar_counting_false_positive_on_local_variable() -> None:
    """Bare local variables (e.g. diagnostic ``bar_count``) must not
    trigger the bar-counting exit guard — only ``self.<name>`` instance
    attributes are flagged."""
    code = (
        "from contract import OrderSide, OrderType, Strategy, TimeInForce\n"
        "from indicators import rsi\n\n"
        "class S(Strategy):\n"
        "    UNIVERSE = frozenset({'QQQ'})\n"
        "    WINDOW = 20\n"
        "    def on_bar(self, ctx, bar):\n"
        "        if ctx.is_warmup:\n"
        "            return\n"
        "        if bar.symbol not in self.UNIVERSE:\n"
        "            return\n"
        "        history = ctx.history(bar.symbol, self.WINDOW)\n"
        "        if len(history) < self.WINDOW:\n"
        "            return\n"
        "        bar_count = len(history)\n"
        "        days_held = 0\n"
        "        closes = [b.close for b in history]\n"
        "        r = rsi(closes)\n"
        "        pos = ctx.position(bar.symbol)\n"
        "        if pos is None and r < 30:\n"
        "            qty = int((ctx.equity * 0.02) / bar.close)\n"
        "            if qty > 0:\n"
        "                ctx.submit_order(symbol=bar.symbol, qty=qty,\n"
        "                    side=OrderSide.LONG, order_type=OrderType.MARKET,\n"
        "                    tif=TimeInForce.DAY, reason='rsi entry')\n"
        "        elif pos is not None and r > 70:\n"
        "            ctx.submit_order(symbol=bar.symbol, qty=pos.qty,\n"
        "                side=OrderSide.SHORT, order_type=OrderType.MARKET,\n"
        "                tif=TimeInForce.DAY, reason='rsi exit')\n"
    )
    results = CodeConformanceGate().check(code, _happy_spec())
    crits = [r for r in results if r.severity == "critical" and "bar-counting" in r.details.lower()]
    assert crits == []


# ---------------------------------------------------------------------------
# Check 6: sizing math
# ---------------------------------------------------------------------------


def test_sizing_passes_when_qty_uses_ctx_equity() -> None:
    results = CodeConformanceGate().check(_HAPPY_CODE, _happy_spec())
    assert not any("Sizing" in d or "qty=" in d for d in _critical_details(results))


def test_sizing_fails_when_qty_is_hardcoded_int() -> None:
    code = _HAPPY_CODE.replace(
        "qty = max(1, int(ctx.equity * 0.02 / bar.close))",
        "qty = 10",
    )
    results = CodeConformanceGate().check(code, _happy_spec())
    crits = _critical_details(results)
    assert any("ctx.equity" in c or "ctx.capital" in c for c in crits), crits


def test_sizing_fails_when_qty_is_inline_literal_int() -> None:
    code = _HAPPY_CODE.replace(
        'ctx.submit_order(symbol=bar.symbol, qty=qty, side="LONG")',
        'ctx.submit_order(symbol=bar.symbol, qty=5, side="LONG")',
    )
    # The unused ``qty`` assignment still references ctx.equity above —
    # to truly exercise the check, also strip the qty derivation.
    code = code.replace(
        "qty = max(1, int(ctx.equity * 0.02 / bar.close))",
        "qty = 1",
    )
    results = CodeConformanceGate().check(code, _happy_spec())
    crits = _critical_details(results)
    assert any("Every entry" in c or "ctx.equity" in c or "ctx.capital" in c for c in crits), crits


# ---------------------------------------------------------------------------
# Check 7: no side-effects outside hooks/helpers
# ---------------------------------------------------------------------------


def test_no_extra_side_effects_passes_when_calls_only_in_on_bar() -> None:
    results = CodeConformanceGate().check(_HAPPY_CODE, _happy_spec())
    assert not any("disallowed scope" in d for d in _critical_details(results))


def test_no_extra_side_effects_passes_for_helper_underscore_method() -> None:
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            def _enter(self, ctx, bar):
                qty = max(1, int(ctx.equity * 0.02 / bar.close))
                ctx.submit_order(symbol=bar.symbol, qty=qty, side="LONG")

            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                bars = ctx.history(bar.symbol, 200)
                fast = sma(bars, 50)
                slow = sma(bars, 200)
                pos = ctx.position(bar.symbol)
                if pos is None and fast > slow:
                    self._enter(ctx, bar)
                elif pos is not None and bar.close < pos.entry_price * 0.95:
                    ctx.submit_order(symbol=bar.symbol, qty=pos.qty, side="SHORT")
        """
    )
    spec = _spec(
        entry_rules=[_sma_cross_entry()],
        exit_rules=[StopLossRule(pct=0.05)],
        target_symbols=["QQQ"],
    )
    results = CodeConformanceGate().check(code, spec)
    assert not any("disallowed scope" in d for d in _critical_details(results))


def test_no_extra_side_effects_fails_when_submit_in_init() -> None:
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            def __init__(self, ctx=None):
                super().__init__()
                if ctx is not None:
                    ctx.submit_order(symbol="QQQ", qty=1, side="LONG")

            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                bars = ctx.history(bar.symbol, 200)
                fast = sma(bars, 50)
                slow = sma(bars, 200)
                pos = ctx.position(bar.symbol)
                qty = max(1, int(ctx.equity * 0.02 / bar.close))
                if pos is None and fast > slow:
                    ctx.submit_order(symbol=bar.symbol, qty=qty, side="LONG")
                elif pos is not None and bar.close < pos.entry_price * 0.95:
                    ctx.submit_order(symbol=bar.symbol, qty=pos.qty, side="SHORT")
        """
    )
    spec = _spec(
        entry_rules=[_sma_cross_entry()],
        exit_rules=[StopLossRule(pct=0.05)],
        target_symbols=["QQQ"],
    )
    results = CodeConformanceGate().check(code, spec)
    crits = _critical_details(results)
    assert any("disallowed scope" in c and "__init__" in c for c in crits), crits


# ---------------------------------------------------------------------------
# PR #588 Codex review — follow-up regressions
# ---------------------------------------------------------------------------


def test_indicator_presence_ignores_calls_in_unreachable_methods() -> None:
    """Codex P1: an ``sma(...)`` call in a never-called helper must not
    satisfy the indicator-presence check. ``on_bar`` is what runs at
    runtime, so calls only count when reachable from it."""
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            def _dead(self, ctx, bar):
                # Reference sma here only — never called from on_bar.
                _ = sma(ctx.history(bar.symbol, 50), 50)

            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                bars = ctx.history(bar.symbol, 200)
                # Inline mean rather than calling sma() — and the spec's
                # sma reference lives in the unreachable _dead helper.
                fast = sum(b.close for b in bars[-50:]) / 50
                slow = sum(b.close for b in bars[-200:]) / 200
                pos = ctx.position(bar.symbol)
                qty = max(1, int(ctx.equity * 0.02 / bar.close))
                if pos is None and fast > slow:
                    ctx.submit_order(symbol=bar.symbol, qty=qty, side="LONG")
                elif pos is not None and bar.close < pos.entry_price * 0.95:
                    ctx.submit_order(symbol=bar.symbol, qty=pos.qty, side="SHORT")
        """
    )
    spec = _spec(
        entry_rules=[_sma_cross_entry()],
        exit_rules=[StopLossRule(pct=0.05)],
        target_symbols=["QQQ"],
    )
    results = CodeConformanceGate().check(code, spec)
    crits = _critical_details(results)
    assert any("indicator" in c.lower() and "sma" in c for c in crits), crits


def test_branch_coverage_does_not_double_count_nested_branches() -> None:
    """Codex P1: ``if A: if B: submit()`` is one logical entry, not two.

    The strategy below has two entry rules in spec but only ONE nested
    submit_order. Without the nested-branch fix, both the outer and the
    inner If would count as entry branches (2 ≥ 2) and the test would
    silently pass. With the fix, only the innermost If is credited,
    leaving 1 < 2 entry branches — critical fires."""
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                bars = ctx.history(bar.symbol, 200)
                fast = sma(bars, 50)
                slow = sma(bars, 200)
                r = rsi(bars, 14)
                pos = ctx.position(bar.symbol)
                qty = max(1, int(ctx.equity * 0.02 / bar.close))
                if pos is None:
                    if fast > slow:
                        ctx.submit_order(symbol=bar.symbol, qty=qty, side="LONG")
                elif bar.close < pos.entry_price * 0.95:
                    ctx.submit_order(symbol=bar.symbol, qty=pos.qty, side="SHORT")
        """
    )
    spec = _spec(
        entry_rules=[_sma_cross_entry(), _sma_cross_entry(side="short")],
        exit_rules=[StopLossRule(pct=0.05)],
        target_symbols=["QQQ"],
    )
    spec = spec.model_copy(update={"requires_custom_code": True})
    results = CodeConformanceGate().check(code, spec)
    crits = _critical_details(results)
    assert any("2 entry rule" in c and "only 1" in c for c in crits), crits


def test_no_extra_side_effects_flags_dead_helper_with_submit() -> None:
    """Codex P2: an unreachable ``_helper`` containing a submit_order
    must trip check #9 — the helper convention is only safe when the
    helper is actually called from a hook."""
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            def _dead(self, ctx, bar):
                # Never called from any hook — stray order code that the
                # gate must surface, not silently accept.
                ctx.submit_order(symbol="QQQ", qty=1, side="LONG")

            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                bars = ctx.history(bar.symbol, 200)
                fast = sma(bars, 50)
                slow = sma(bars, 200)
                pos = ctx.position(bar.symbol)
                qty = max(1, int(ctx.equity * 0.02 / bar.close))
                if pos is None and fast > slow:
                    ctx.submit_order(symbol=bar.symbol, qty=qty, side="LONG")
                elif pos is not None and bar.close < pos.entry_price * 0.95:
                    ctx.submit_order(symbol=bar.symbol, qty=pos.qty, side="SHORT")
        """
    )
    spec = _spec(
        entry_rules=[_sma_cross_entry()],
        exit_rules=[StopLossRule(pct=0.05)],
        target_symbols=["QQQ"],
    )
    results = CodeConformanceGate().check(code, spec)
    crits = _critical_details(results)
    assert any("disallowed scope" in c and "_dead" in c for c in crits), crits


def test_entry_coverage_accepts_kwargs_spread_submit() -> None:
    """Codex P2: ``ctx.submit_order(**order_kwargs)`` may dynamically
    carry ``side`` — the gate must treat the spread as a plausible entry
    rather than failing closed."""
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                bars = ctx.history(bar.symbol, 200)
                fast = sma(bars, 50)
                slow = sma(bars, 200)
                pos = ctx.position(bar.symbol)
                qty = max(1, int(ctx.equity * 0.02 / bar.close))
                if pos is None and fast > slow:
                    order_kwargs = {"symbol": bar.symbol, "qty": qty, "side": "LONG"}
                    ctx.submit_order(**order_kwargs)
                elif pos is not None and bar.close < pos.entry_price * 0.95:
                    ctx.submit_order(symbol=bar.symbol, qty=pos.qty, side="SHORT")
        """
    )
    spec = _spec(
        entry_rules=[_sma_cross_entry()],
        exit_rules=[StopLossRule(pct=0.05)],
        target_symbols=["QQQ"],
    )
    results = CodeConformanceGate().check(code, spec)
    crits = _critical_details(results)
    assert not any("entry rule" in c.lower() for c in crits), crits


def test_duplicate_exit_check_ignores_kwargs_spread_close() -> None:
    """The duplicate-exit check matches only the literal
    ``ctx.submit_order(..., qty=position.qty)`` shape; a
    ``ctx.submit_order(**close_kwargs)`` spread is deliberately not flagged
    (conservative — avoids false positives on dynamically-built kwargs)."""
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                bars = ctx.history(bar.symbol, 200)
                fast = sma(bars, 50)
                slow = sma(bars, 200)
                r = rsi(bars, 14)
                pos = ctx.position(bar.symbol)
                qty = max(1, int(ctx.equity * 0.02 / bar.close))
                if pos is None and fast > slow:
                    ctx.submit_order(symbol=bar.symbol, qty=qty, side="LONG")
                elif pos is not None and r > 70:
                    close_kwargs = {"symbol": bar.symbol, "qty": pos.qty, "side": "SHORT"}
                    ctx.submit_order(**close_kwargs)
        """
    )
    spec = _spec(
        entry_rules=[_sma_cross_entry()],
        exit_rules=[_rsi_signal_exit()],
        target_symbols=["QQQ"],
    )
    spec = spec.model_copy(update={"requires_custom_code": True})
    results = CodeConformanceGate().check(code, spec)
    crits = _critical_details(results)
    assert not any("manual position-closing order" in c.lower() for c in crits), crits
    assert not any("signal-exit" in c.lower() for c in crits), crits


def test_sizing_does_not_flag_boolean_qty_as_hardcoded_int() -> None:
    """Codex P3: ``True`` / ``False`` are ``int`` subclasses; the
    hardcoded-int check must exclude them so a boolean qty (itself an
    anti-pattern, but a different one) is not misreported."""
    code = _HAPPY_CODE.replace(
        'ctx.submit_order(symbol=bar.symbol, qty=qty, side="LONG")',
        'ctx.submit_order(symbol=bar.symbol, qty=True, side="LONG")',
    )
    results = CodeConformanceGate().check(code, _happy_spec())
    crits = _critical_details(results)
    # The "Every entry … literal integer ``qty=``" message must not fire
    # for a boolean. ``ctx.equity`` is still referenced in the (unused)
    # qty assignment, so the sizing fallback also passes — no sizing
    # critical at all.
    assert not any("literal integer" in c or "Every entry" in c for c in crits), crits
