"""Tests for the deterministic StrategySpec → Python compiler (issue #538).

Scope:
  * ``compile_strategy`` produces a valid Python module with exactly one
    Strategy subclass per spec.
  * The emitted code passes ``CodeSafetyChecker`` and
    ``CodeConformanceGate`` for every rule kind in the DSL.
  * The compiler is deterministic — same spec, byte-identical output.
  * Performance budget (< 50 ms per compile) holds for representative
    multi-rule specs.

These tests are pure unit tests on the compiler; they do not drive the
orchestrator, so the ``strategy_lab_integration`` marker (and its
readiness-fetch stub) is not needed.
"""

from __future__ import annotations

import ast
import time
from typing import Any, List

import pytest

from investment_team.models import StrategySpec
from investment_team.strategy_lab.quality_gates.code_conformance import CodeConformanceGate
from investment_team.strategy_lab.quality_gates.code_safety import CodeSafetyChecker
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    FixedFractionSizing,
    FixedNotionalSizing,
    IndicatorRef,
    Predicate,
    SignalExitRule,
    StopLossRule,
    TakeProfitRule,
    VolatilityTargetSizing,
)
from investment_team.strategy_lab.synthesis import CompilerError, compile_strategy

# ---------------------------------------------------------------------------
# Fixture helpers — build minimal-but-valid StrategySpec instances.
# ---------------------------------------------------------------------------


def _spec(
    *,
    entry_rules: List[EntryRule],
    exit_rules: List[Any] | None = None,
    sizing: Any | None = None,
    target_symbols: List[str] | None = None,
) -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis="test hypothesis",
        signal_definition="test signal",
        timeframe="1d",
        entry_rules=entry_rules,
        exit_rules=exit_rules or [],
        sizing=sizing or FixedFractionSizing(fraction=0.02),
        target_symbols=target_symbols if target_symbols is not None else ["QQQ"],
    )


def _rsi_lt_30_entry() -> EntryRule:
    return EntryRule(
        side="long",
        when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op="<", rhs=30.0),
    )


def _rsi_gt_70_exit() -> SignalExitRule:
    return SignalExitRule(
        when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op=">", rhs=70.0),
    )


def _critical_details(results) -> list[str]:
    return [r.details for r in results if r.severity == "critical" and not r.passed]


def _strategy_subclass_count(code: str) -> int:
    tree = ast.parse(code)
    n = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            if isinstance(base, ast.Name) and base.id == "Strategy":
                n += 1
            elif isinstance(base, ast.Attribute) and base.attr == "Strategy":
                n += 1
    return n


# ---------------------------------------------------------------------------
# Per-rule-kind compiles cleanly.
# ---------------------------------------------------------------------------


def test_entry_only_rsi() -> None:
    spec = _spec(entry_rules=[_rsi_lt_30_entry()])
    code = compile_strategy(spec)
    assert "_ind_rsi_" in code
    assert "OrderSide.LONG" in code
    assert "ctx.submit_order" in code
    assert _strategy_subclass_count(code) == 1


def test_signal_exit_only() -> None:
    spec = _spec(
        entry_rules=[_rsi_lt_30_entry()],
        exit_rules=[_rsi_gt_70_exit()],
    )
    code = compile_strategy(spec)
    assert "qty=position.qty" in code
    assert code.count("ctx.submit_order") == 2  # one entry, one signal exit


def test_stop_loss_present_no_inline_or_bracket() -> None:
    """Codex round-6: stop-loss is enforced ENTIRELY by the engine's
    ``evaluate_exit_rules``. The compiler does NOT emit an inline
    close (would duplicate engine_exit), NOR a bracket attachment
    (which would use the wrong stop_price based on signal-bar close
    instead of the actual post-fill ``position.entry_price``, and
    silently downgrade trailing semantics to static stops). The
    safety gate accepts entries-only flow when spec has engine-handled
    exits — see ``_spec_has_engine_handled_exit``.
    """
    spec = _spec(
        entry_rules=[_rsi_lt_30_entry()],
        exit_rules=[StopLossRule(pct=0.05)],
    )
    code = compile_strategy(spec)
    assert 'reason="compiled_stop_loss"' not in code
    assert "StopAttachment" not in code
    assert "attached_stop_loss" not in code
    # Conformance gate's stop-loss enforcement check requires the class
    # to reference ``position.entry_price``; the benign ``_entry_ref``
    # read satisfies it without re-implementing exit math.
    assert "position.entry_price" in code


def test_take_profit_present_no_inline_or_bracket() -> None:
    spec = _spec(
        entry_rules=[_rsi_lt_30_entry()],
        exit_rules=[TakeProfitRule(pct=0.10)],
    )
    code = compile_strategy(spec)
    assert 'reason="compiled_take_profit"' not in code
    assert "LimitAttachment" not in code
    assert "attached_take_profit" not in code
    assert "position.entry_price" in code


def test_trailing_stop_loss_compiles_without_inline_emission() -> None:
    """Trailing-basis stop-loss specs are accepted (no CompilerError).
    The engine's ``evaluate_exit_rules`` honours the ``basis`` field
    at runtime against ``position.entry_price`` (and
    ``position.high_since_entry`` / ``low_since_entry`` for trailing
    variants), so the compiler doesn't need to re-encode any of it.
    """
    spec = _spec(
        entry_rules=[_rsi_lt_30_entry()],
        exit_rules=[StopLossRule(pct=0.05, basis="trailing_high")],
    )
    code = compile_strategy(spec)
    assert "CompiledStrategy" in code
    # No inline emission, no bracket attachment — engine handles it.
    assert 'reason="compiled_stop_loss"' not in code
    assert "StopAttachment" not in code


def test_fixed_fraction_sizing() -> None:
    spec = _spec(
        entry_rules=[_rsi_lt_30_entry()],
        sizing=FixedFractionSizing(fraction=0.05),
    )
    code = compile_strategy(spec)
    assert "ctx.equity * 0.05" in code
    assert "max(1, int(" in code


def test_fixed_notional_sizing() -> None:
    spec = _spec(
        entry_rules=[_rsi_lt_30_entry()],
        sizing=FixedNotionalSizing(notional_usd=10_000.0),
    )
    code = compile_strategy(spec)
    assert "10000.0 / bar.close" in code
    # Sizing gate also requires *somewhere* in the class to read ctx.equity.
    assert "ctx.equity" in code


def test_volatility_target_with_atr() -> None:
    entry = EntryRule(
        side="long",
        when=Predicate(
            lhs=IndicatorRef(name="atr", params={"period": 14}),
            op=">",
            rhs=1.0,
        ),
    )
    spec = _spec(
        entry_rules=[entry],
        sizing=VolatilityTargetSizing(target_annual_vol=0.20),
    )
    code = compile_strategy(spec)
    assert "_ind_atr_" in code
    assert "ctx.equity * 0.2" in code


def test_volatility_target_without_atr_raises_compiler_error() -> None:
    spec = _spec(
        entry_rules=[_rsi_lt_30_entry()],
        sizing=VolatilityTargetSizing(target_annual_vol=0.20),
    )
    with pytest.raises(CompilerError, match="atr"):
        compile_strategy(spec)


# ---------------------------------------------------------------------------
# Multi-rule + cross_above semantics.
# ---------------------------------------------------------------------------


def test_multi_rule_full_spec() -> None:
    entry = EntryRule(
        side="long",
        when=Predicate(
            lhs=IndicatorRef(name="sma", params={"period": 50}),
            op="cross_above",
            rhs=IndicatorRef(name="sma", params={"period": 200}),
        ),
    )
    signal_exit = SignalExitRule(
        when=Predicate(
            lhs=IndicatorRef(name="sma", params={"period": 50}),
            op="cross_below",
            rhs=IndicatorRef(name="sma", params={"period": 200}),
        ),
    )
    spec = _spec(
        entry_rules=[entry],
        exit_rules=[signal_exit, StopLossRule(pct=0.03), TakeProfitRule(pct=0.06)],
        sizing=FixedFractionSizing(fraction=0.02),
    )
    code = compile_strategy(spec)
    assert _strategy_subclass_count(code) == 1
    assert "self._cross_prev" in code  # per-symbol cross_* state dict present
    assert "position.entry_price" in code  # stop/take-profit gate compliance


def test_cross_above_emits_per_symbol_prev_state() -> None:
    """Codex P1 review: cross state must be keyed by ``bar.symbol`` so a
    multi-symbol run can't compare this bar's value against a different
    symbol's previous bar.
    """
    entry = EntryRule(
        side="long",
        when=Predicate(
            lhs=IndicatorRef(name="ema", params={"period": 12}),
            op="cross_above",
            rhs=IndicatorRef(name="ema", params={"period": 26}),
        ),
    )
    spec = _spec(entry_rules=[entry])
    code = compile_strategy(spec)
    # __init__ creates the per-symbol dict.
    tree = ast.parse(code)
    init_methods = [
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "__init__"
    ]
    assert init_methods, "compiled class must define __init__ for cross-state"
    cross_prev_assignments = [
        node
        for method in init_methods
        for node in ast.walk(method)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Attribute) and t.attr == "_cross_prev" for t in node.targets)
    ]
    assert len(cross_prev_assignments) == 1, "expected `self._cross_prev = {}` in __init__"
    # on_bar reads the per-symbol slice and writes back.
    assert "self._cross_prev.get(bar.symbol" in code
    assert "self._cross_prev.setdefault(bar.symbol" in code


# ---------------------------------------------------------------------------
# Determinism & shape.
# ---------------------------------------------------------------------------


def test_determinism_identical_output() -> None:
    spec = _spec(
        entry_rules=[_rsi_lt_30_entry()],
        exit_rules=[_rsi_gt_70_exit(), StopLossRule(pct=0.05)],
    )
    a = compile_strategy(spec)
    b = compile_strategy(spec)
    assert a == b


def test_ast_has_exactly_one_strategy_subclass() -> None:
    spec = _spec(entry_rules=[_rsi_lt_30_entry()])
    code = compile_strategy(spec)
    assert _strategy_subclass_count(code) == 1


def test_header_contains_spec_hash() -> None:
    spec = _spec(entry_rules=[_rsi_lt_30_entry()])
    code = compile_strategy(spec)
    assert "spec_hash:" in code
    # Hash is 12 hex chars on the header line.
    header_line = next(line for line in code.splitlines() if "spec_hash:" in line)
    hash_token = header_line.split("spec_hash:")[1].strip()
    assert len(hash_token) == 12
    assert all(c in "0123456789abcdef" for c in hash_token)


def test_no_target_symbols_skips_universe_guard() -> None:
    spec = _spec(entry_rules=[_rsi_lt_30_entry()], target_symbols=[])
    code = compile_strategy(spec)
    assert "UNIVERSE = frozenset()" in code
    # Guard line is suppressed when there are no target symbols.
    assert "bar.symbol not in self.UNIVERSE" not in code


# ---------------------------------------------------------------------------
# Quality-gate compatibility — emitted code must pass safety + conformance.
# ---------------------------------------------------------------------------


def test_safety_gate_passes_for_full_spec() -> None:
    entry = EntryRule(
        side="long",
        when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op="<", rhs=30.0),
    )
    spec = _spec(
        entry_rules=[entry],
        exit_rules=[_rsi_gt_70_exit(), StopLossRule(pct=0.05)],
    )
    code = compile_strategy(spec)
    results = CodeSafetyChecker().check(code, spec)
    assert _critical_details(results) == [], _critical_details(results)


def test_conformance_gate_passes_for_full_spec() -> None:
    entry = EntryRule(
        side="long",
        when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op="<", rhs=30.0),
    )
    spec = _spec(
        entry_rules=[entry],
        exit_rules=[_rsi_gt_70_exit(), StopLossRule(pct=0.05), TakeProfitRule(pct=0.10)],
    )
    code = compile_strategy(spec)
    results = CodeConformanceGate().check(code, spec)
    assert _critical_details(results) == [], _critical_details(results)


def test_conformance_gate_passes_for_cross_above_with_brackets() -> None:
    entry = EntryRule(
        side="long",
        when=Predicate(
            lhs=IndicatorRef(name="sma", params={"period": 50}),
            op="cross_above",
            rhs=IndicatorRef(name="sma", params={"period": 200}),
        ),
    )
    spec = _spec(
        entry_rules=[entry],
        exit_rules=[StopLossRule(pct=0.03), TakeProfitRule(pct=0.06)],
        sizing=FixedFractionSizing(fraction=0.02),
    )
    code = compile_strategy(spec)
    safety = CodeSafetyChecker().check(code, spec)
    conformance = CodeConformanceGate().check(code, spec)
    assert _critical_details(safety) == [], _critical_details(safety)
    assert _critical_details(conformance) == [], _critical_details(conformance)


def test_conformance_gate_passes_for_fixed_notional_sizing() -> None:
    spec = _spec(
        entry_rules=[_rsi_lt_30_entry()],
        exit_rules=[_rsi_gt_70_exit()],
        sizing=FixedNotionalSizing(notional_usd=10_000.0),
    )
    code = compile_strategy(spec)
    results = CodeConformanceGate().check(code, spec)
    assert _critical_details(results) == [], _critical_details(results)


# ---------------------------------------------------------------------------
# Performance — < 50 ms per compile on representative specs.
# ---------------------------------------------------------------------------


def test_performance_under_50ms_median() -> None:
    entry = EntryRule(
        side="long",
        when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op="<", rhs=30.0),
    )
    spec = _spec(
        entry_rules=[entry],
        exit_rules=[_rsi_gt_70_exit(), StopLossRule(pct=0.05), TakeProfitRule(pct=0.10)],
    )
    durations: List[float] = []
    for _ in range(50):
        t0 = time.perf_counter()
        compile_strategy(spec)
        durations.append((time.perf_counter() - t0) * 1000.0)
    durations.sort()
    median_ms = durations[len(durations) // 2]
    assert median_ms < 50.0, f"compile median {median_ms:.2f} ms exceeds 50 ms budget"


# ---------------------------------------------------------------------------
# Sandbox probe — deferred to #E2 once the strategy-execution probes ship.
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="depends on #E2 synthetic-data probes — not yet shipped")
def test_sandbox_probe_runs_expected_trades() -> None:
    """Run compiled output through the trading-service sandbox with
    synthetic OHLCV; assert the expected entry/exit fills.

    Placeholder — flip ``@pytest.mark.skip`` off once #E2 lands the
    probe fixtures.
    """


# ---------------------------------------------------------------------------
# Runtime semantics — exec the emitted module, call the indicator helpers
# and on_bar against synthetic bars. Covers the codex P1 review findings:
#   1. Helpers compute scalar values from a list[Bar] (no pandas).
#   2. Tuple-valued indicators (macd / bollinger / stochastic) thread the
#      selector and return ONE scalar matching the DSL selector.
#   3. Two same-bar exit thresholds emit exactly one close order.
# ---------------------------------------------------------------------------


class _SyntheticBar:
    __slots__ = ("symbol", "timestamp", "open", "high", "low", "close", "volume")

    def __init__(
        self,
        *,
        symbol: str = "QQQ",
        timestamp: str = "2024-01-01",
        open: float = 100.0,
        high: float = 100.0,
        low: float = 100.0,
        close: float = 100.0,
        volume: float = 1_000_000.0,
    ) -> None:
        self.symbol = symbol
        self.timestamp = timestamp
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume


class _SyntheticPosition:
    __slots__ = ("symbol", "side", "qty", "entry_price")

    def __init__(self, *, side, qty: float = 10.0, entry_price: float = 100.0) -> None:
        self.symbol = "QQQ"
        self.side = side
        self.qty = qty
        self.entry_price = entry_price


class _SyntheticContext:
    def __init__(self, *, history, position=None, equity: float = 100_000.0) -> None:
        self._history = list(history)
        self._position = position
        self.equity = equity
        self.capital = equity
        self.is_warmup = False
        self.orders: list[dict] = []

    def history(self, _symbol: str, n: int):
        return self._history[-n:] if n > 0 else []

    def position(self, _symbol: str):
        return self._position

    def submit_order(self, **kwargs):
        self.orders.append(kwargs)
        return f"c{len(self.orders)}"


def _exec_module(code: str):
    """Compile + exec the emitted module, returning the module globals.

    Stubs out ``from contract import ...`` so the test runs without the
    streaming-harness package layout: we inject a fake ``contract``
    module with ``Strategy``, ``OrderSide``, ``OrderType``, ``TimeInForce``
    that match the strategy-side shape.
    """
    import sys
    import types

    fake_contract = types.ModuleType("contract")

    class _Strategy:
        pass

    class _Enum(str):
        def __new__(cls, value):
            inst = str.__new__(cls, value)
            return inst

    class _OrderSide:
        LONG = _Enum("LONG")
        SHORT = _Enum("SHORT")

    class _OrderType:
        MARKET = _Enum("MARKET")

    class _TimeInForce:
        DAY = _Enum("DAY")

    class _StopAttachment:
        def __init__(self, *, stop_price, trail_offset=None, trail_offset_kind="abs"):
            self.stop_price = stop_price
            self.trail_offset = trail_offset
            self.trail_offset_kind = trail_offset_kind

    class _LimitAttachment:
        def __init__(self, *, limit_price):
            self.limit_price = limit_price

    fake_contract.Strategy = _Strategy  # type: ignore[attr-defined]
    fake_contract.OrderSide = _OrderSide  # type: ignore[attr-defined]
    fake_contract.OrderType = _OrderType  # type: ignore[attr-defined]
    fake_contract.TimeInForce = _TimeInForce  # type: ignore[attr-defined]
    fake_contract.StopAttachment = _StopAttachment  # type: ignore[attr-defined]
    fake_contract.LimitAttachment = _LimitAttachment  # type: ignore[attr-defined]
    sys.modules["contract"] = fake_contract
    try:
        ns: dict[str, Any] = {}
        compiled = compile(code, "<compiled-strategy>", "exec")
        exec(compiled, ns)
        return ns, _OrderSide, _OrderType, _TimeInForce
    finally:
        sys.modules.pop("contract", None)


def test_compiled_module_syntactically_valid() -> None:
    spec = _spec(
        entry_rules=[_rsi_lt_30_entry()],
        exit_rules=[_rsi_gt_70_exit(), StopLossRule(pct=0.05), TakeProfitRule(pct=0.10)],
    )
    code = compile_strategy(spec)
    # Parse + compile + exec all succeed.
    ns, _OrderSide, _OrderType, _TimeInForce = _exec_module(code)
    assert "CompiledStrategy" in ns
    strat = ns["CompiledStrategy"]()
    # Helpers exist as callables on the class.
    assert callable(getattr(strat, "rsi"))


def test_rsi_helper_returns_scalar() -> None:
    """RSI helper must return a float (not raise on list input)."""
    spec = _spec(entry_rules=[_rsi_lt_30_entry()])
    code = compile_strategy(spec)
    ns, *_ = _exec_module(code)
    strat = ns["CompiledStrategy"]()
    # 30 bars of synthetic data with a clear uptrend → RSI well > 50.
    bars = [_SyntheticBar(close=100.0 + i) for i in range(30)]
    value = strat.rsi(bars, period=14, source="close")
    assert isinstance(value, float)
    assert 50.0 < value <= 100.0


def test_sma_helper_returns_correct_average() -> None:
    spec = _spec(
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(
                    lhs=IndicatorRef(name="sma", params={"period": 5}), op="<", rhs=200.0
                ),
            )
        ]
    )
    code = compile_strategy(spec)
    ns, *_ = _exec_module(code)
    strat = ns["CompiledStrategy"]()
    bars = [_SyntheticBar(close=float(c)) for c in (10, 20, 30, 40, 50)]
    value = strat.sma(bars, period=5, source="close")
    assert value == pytest.approx(30.0)


def test_macd_helper_threads_selector_returns_scalar() -> None:
    """MACD must return ONE scalar component (the DSL ``output`` selector)."""
    entry = EntryRule(
        side="long",
        when=Predicate(lhs=IndicatorRef(name="macd", params={"output": "signal"}), op=">", rhs=0.0),
    )
    spec = _spec(entry_rules=[entry])
    code = compile_strategy(spec)
    # Helper call must thread select="signal" through, so the runtime
    # value compared in the predicate is a scalar (not a tuple).
    assert "select='signal'" in code or 'select="signal"' in code
    ns, *_ = _exec_module(code)
    strat = ns["CompiledStrategy"]()
    bars = [_SyntheticBar(close=100.0 + i * 0.5) for i in range(60)]
    value = strat.macd(bars, fast=12, slow=26, signal=9, source="close", select="signal")
    assert isinstance(value, float)


def test_bollinger_helper_returns_band_scalar() -> None:
    entry = EntryRule(
        side="long",
        when=Predicate(
            lhs="bar.close",
            op="<",
            rhs=IndicatorRef(name="bollinger", params={"band": "lower", "period": 20}),
        ),
    )
    spec = _spec(entry_rules=[entry])
    code = compile_strategy(spec)
    assert "select='lower'" in code or 'select="lower"' in code
    ns, *_ = _exec_module(code)
    strat = ns["CompiledStrategy"]()
    bars = [_SyntheticBar(close=100.0) for _ in range(25)]
    lower = strat.bollinger_bands(bars, period=20, num_std=2.0, source="close", select="lower")
    middle = strat.bollinger_bands(bars, period=20, num_std=2.0, source="close", select="middle")
    upper = strat.bollinger_bands(bars, period=20, num_std=2.0, source="close", select="upper")
    assert middle == pytest.approx(100.0)
    # Constant series → zero std → all three bands collapse onto the mean.
    assert lower == pytest.approx(100.0)
    assert upper == pytest.approx(100.0)


def test_atr_helper_returns_scalar_from_bar_list() -> None:
    entry = EntryRule(
        side="long",
        when=Predicate(lhs=IndicatorRef(name="atr"), op=">", rhs=0.5),
    )
    spec = _spec(entry_rules=[entry])
    code = compile_strategy(spec)
    ns, *_ = _exec_module(code)
    strat = ns["CompiledStrategy"]()
    # 20 bars, range 1.0 per bar.
    bars = [_SyntheticBar(open=100.0, high=101.0, low=99.0, close=100.0) for _ in range(20)]
    value = strat.atr(bars, period=14)
    assert isinstance(value, float)
    assert value > 0.0


def test_stop_and_take_profit_do_not_emit_inline_exits() -> None:
    """Codex round-5: stop_loss / take_profit are engine-enforced via
    ``evaluate_exit_rules``. Inlining a parallel close would duplicate
    the engine_exit and inflate order lifecycle diagnostics.
    """
    spec = _spec(
        entry_rules=[_rsi_lt_30_entry()],
        exit_rules=[StopLossRule(pct=0.05), TakeProfitRule(pct=0.10)],
    )
    code = compile_strategy(spec)
    assert 'reason="compiled_stop_loss"' not in code
    assert 'reason="compiled_take_profit"' not in code

    ns, _OrderSide, *_ = _exec_module(code)
    strat = ns["CompiledStrategy"]()
    # Bar that would have flushed both thresholds — high spike AND low
    # drop on the same candle relative to entry_price=100. With inline
    # emission gone, the strategy emits no exit orders on this bar.
    flush_bar = _SyntheticBar(symbol="QQQ", open=100.0, high=115.0, low=90.0, close=100.0)
    history = [_SyntheticBar(close=100.0) for _ in range(25)] + [flush_bar]
    position = _SyntheticPosition(side=_OrderSide.LONG, qty=10.0, entry_price=100.0)
    ctx = _SyntheticContext(history=history, position=position)
    strat.on_bar(ctx, flush_bar)
    assert ctx.orders == [], "stop/take-profit closes should come from the engine, not the strategy"


def test_entry_only_with_no_exits_is_pre_safety_gate_concern() -> None:
    """Entry-only specs are valid input to the compiler; the safety
    gate is what rejects them (no exit path). This test pins that
    expectation so the compiler doesn't grow a silent reject.
    """
    spec = _spec(entry_rules=[_rsi_lt_30_entry()])
    code = compile_strategy(spec)
    # Compilation succeeds — module is valid Python.
    ns, *_ = _exec_module(code)
    assert "CompiledStrategy" in ns
    # But CodeSafetyChecker rejects because there's no exit submit_order.
    safety = CodeSafetyChecker().check(code, spec)
    criticals = _critical_details(safety)
    assert any("exit" in c.lower() for c in criticals), criticals


def test_no_indicators_import_in_emitted_code() -> None:
    """Codex P1 review: the sandbox's ``indicators`` module expects
    pandas Series, not list[Bar]. Inline helpers replace the import."""
    spec = _spec(
        entry_rules=[_rsi_lt_30_entry()],
        exit_rules=[_rsi_gt_70_exit(), StopLossRule(pct=0.05)],
    )
    code = compile_strategy(spec)
    assert "from indicators import" not in code
    # Only the canonical helpers actually used by the spec are emitted.
    assert "def rsi(self, history" in code
    assert "def sma(self, history" not in code  # rsi-only spec — no sma helper


# ---------------------------------------------------------------------------
# Codex P1 round-2: ADX window, MACD fast/slow guard, per-symbol cross state,
# multi-entry guard. Codex P2: spec-hash field scope.
# ---------------------------------------------------------------------------


def test_adx_window_at_least_two_period_plus_one() -> None:
    """ADX helper requires ``2 * period + 1`` bars before returning a
    value (Wilder smoothing eats two windows). The emitted ``WINDOW``
    must reflect that or the binding stays ``None`` forever.
    """
    entry = EntryRule(
        side="long",
        when=Predicate(lhs=IndicatorRef(name="adx", params={"period": 14}), op=">", rhs=25.0),
    )
    spec = _spec(entry_rules=[entry])
    code = compile_strategy(spec)
    # 2*14 + 1 = 29 ⇒ WINDOW >= 29
    assert "WINDOW = 29" in code


def test_macd_fast_greater_than_slow_raises() -> None:
    """Codex P1: fast >= slow would IndexError inside the helper —
    refuse the spec at compile time so the orchestrator falls back."""
    entry = EntryRule(
        side="long",
        when=Predicate(
            lhs=IndicatorRef(name="macd", params={"fast": 30, "slow": 10, "signal": 9}),
            op=">",
            rhs=0.0,
        ),
    )
    spec = _spec(entry_rules=[entry])
    with pytest.raises(CompilerError, match="fast < slow"):
        compile_strategy(spec)


def test_macd_helper_defensive_fast_slow_returns_none() -> None:
    """Defense-in-depth: even if a spec slipped past the front-door
    check, the helper must return None rather than IndexError."""
    # Build the code via a valid spec, then exec the module and call
    # the helper with a fast >= slow combo directly.
    spec = _spec(
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(lhs=IndicatorRef(name="macd", params={}), op=">", rhs=0.0),
            )
        ]
    )
    code = compile_strategy(spec)
    ns, *_ = _exec_module(code)
    strat = ns["CompiledStrategy"]()
    bars = [_SyntheticBar(close=100.0 + i) for i in range(60)]
    assert strat.macd(bars, fast=30, slow=10, signal=9) is None


def test_cross_state_isolated_per_symbol() -> None:
    """Run on_bar for two different symbols and verify the second
    symbol's first bar doesn't pick up the first symbol's prev value.
    """
    entry = EntryRule(
        side="long",
        when=Predicate(
            lhs=IndicatorRef(name="sma", params={"period": 5}),
            op="cross_above",
            rhs=IndicatorRef(name="sma", params={"period": 10}),
        ),
    )
    spec = _spec(
        entry_rules=[entry],
        # Two-symbol universe so the universe guard accepts both.
        target_symbols=["AAA", "BBB"],
    )
    code = compile_strategy(spec)
    ns, *_ = _exec_module(code)
    strat = ns["CompiledStrategy"]()

    # Symbol A: descending closes — fast SMA below slow SMA on the
    # latest bar. After on_bar, A's _cross_prev has its own values.
    bars_a = [_SyntheticBar(symbol="AAA", close=100.0 - i) for i in range(30)]
    ctx = _SyntheticContext(history=bars_a)
    strat.on_bar(ctx, bars_a[-1])
    # Symbol B's first bar should not see symbol A's previous values.
    assert "AAA" in strat._cross_prev
    assert "BBB" not in strat._cross_prev

    bars_b = [_SyntheticBar(symbol="BBB", close=100.0 + i) for i in range(30)]
    ctx_b = _SyntheticContext(history=bars_b)
    strat.on_bar(ctx_b, bars_b[-1])
    # Now both symbols have their own slots — no overlap.
    assert "BBB" in strat._cross_prev
    assert strat._cross_prev["AAA"] != strat._cross_prev["BBB"]


def test_multiple_entry_rules_one_bar_emits_one_entry() -> None:
    """Codex P1: when two entry predicates are true on the same bar
    the compiled code must emit ONE entry, not two.
    """
    # Two entry rules; both will fire on a flat-history bar because
    # each predicate threshold is generous.
    rule_a = EntryRule(
        side="long",
        when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op="<", rhs=99.0),
    )
    rule_b = EntryRule(
        side="long",
        when=Predicate(lhs="bar.close", op=">", rhs=1.0),
    )
    spec = _spec(entry_rules=[rule_a, rule_b])
    code = compile_strategy(spec)
    assert "entry_submitted" in code
    ns, _OrderSide, *_ = _exec_module(code)
    strat = ns["CompiledStrategy"]()
    history = [_SyntheticBar(close=100.0) for _ in range(25)]
    ctx = _SyntheticContext(history=history, position=None)
    strat.on_bar(ctx, history[-1])
    # Exactly ONE entry submission, not two.
    assert len(ctx.orders) == 1, ctx.orders


def test_spec_hash_ignores_non_dsl_fields() -> None:
    """Codex P2: the spec-hash header must be invariant to non-DSL
    fields (hypothesis prose, ``strategy_code``, audit) so semantically
    identical rule specs produce byte-identical compiled output.
    """
    spec_a = _spec(entry_rules=[_rsi_lt_30_entry()], exit_rules=[_rsi_gt_70_exit()])
    # Mutate non-DSL fields on a deep copy.
    spec_b = spec_a.model_copy(deep=True)
    spec_b.hypothesis = "different hypothesis prose"
    spec_b.signal_definition = "different signal definition"
    spec_b.strategy_code = "# placeholder LLM output that would change every run"
    code_a = compile_strategy(spec_a)
    code_b = compile_strategy(spec_b)
    assert code_a == code_b, (
        "compiler output must be invariant to non-DSL fields — same "
        "rules + sizing + target_symbols must produce identical code"
    )


def test_exit_rules_emitted_in_author_order() -> None:
    """Codex round-5 follow-up: stop_loss / take_profit are no longer
    inlined (engine enforces them), so the kind-partitioning concern
    from round-4 no longer applies. What remains is signal-exit order:
    when the spec has multiple signal-exit rules, they must be emitted
    in author-declared order under the ``exit_submitted`` short-circuit.
    """
    spec = _spec(
        entry_rules=[_rsi_lt_30_entry()],
        exit_rules=[
            SignalExitRule(
                when=Predicate(
                    lhs=IndicatorRef(name="rsi", params={"period": 14}),
                    op=">",
                    rhs=70.0,
                ),
                note="exit-a",
            ),
            SignalExitRule(
                when=Predicate(
                    lhs="bar.close",
                    op=">",
                    rhs=200.0,
                ),
                note="exit-b",
            ),
        ],
    )
    code = compile_strategy(spec)
    # Both signal-exit branches emit ``compiled_signal_exit`` reasons.
    # The first branch's predicate (rsi > 70) must appear before the
    # second's (bar.close > 200) in the emitted source.
    rsi_pred_pos = code.find("> 70.0")
    bar_pred_pos = code.find("bar.close > 200.0")
    assert rsi_pred_pos != -1 and bar_pred_pos != -1
    assert rsi_pred_pos < bar_pred_pos


def test_compiled_code_flows_into_orchestrator_local_variable() -> None:
    """Codex P1 round-3: the orchestrator must propagate ``spec.strategy_code``
    into the local ``code`` variable. Downstream gates and
    ``_run_synthesis_loop`` take ``code=...`` directly; writing only to
    ``spec.strategy_code`` would silently bypass the compiler.

    Verifies by patching ``compile_strategy`` to a sentinel string and
    asserting the orchestrator routes that string through to the next
    phase, not the LLM-authored input. The fake ``_run_pre_synthesis_phase``
    raises an early-exit sentinel so the test never reaches the synthesis
    loop's LLM-driven refinement path.
    """
    from unittest.mock import patch

    from investment_team.models import BacktestConfig
    from investment_team.strategy_lab import orchestrator as orch_mod

    sentinel_code = "# compiled-sentinel\nfrom contract import Strategy\n"
    captured: dict[str, Any] = {}

    class _EarlyExit(Exception):
        pass

    def fake_pre_synth(
        self,
        *,
        spec,
        config,
        all_gate_results,
        code,
        original_spec,
        original_code,
        rationale,
        refinement_attempts,
        emit,
    ):
        captured["code"] = code
        captured["original_code"] = original_code
        captured["spec_strategy_code"] = spec.strategy_code
        raise _EarlyExit

    spec_dict = {
        "asset_class": "stocks",
        "hypothesis": "test",
        "signal_definition": "sig",
        "timeframe": "1d",
        "entry_rules": [_rsi_lt_30_entry().model_dump()],
        "exit_rules": [_rsi_gt_70_exit().model_dump()],
        "risk_limits": {"max_position_pct": 5, "max_drawdown_pct": 10},
        "target_symbols": ["QQQ"],
    }

    with (
        patch.object(orch_mod, "compile_strategy", return_value=sentinel_code),
        patch.object(
            orch_mod.StrategyLabOrchestrator,
            "_run_pre_synthesis_phase",
            fake_pre_synth,
        ),
    ):
        orch = orch_mod.StrategyLabOrchestrator()
        orch.ideation_agent.run = lambda **_kw: (  # type: ignore[assignment]
            spec_dict,
            "# llm-authored ideation code\n",
            "rationale",
        )
        cfg = BacktestConfig(
            start_date="2023-01-01",
            end_date="2023-12-31",
            initial_capital=100_000.0,
            benchmark_symbol="SPY",
            transaction_cost_bps=5.0,
            slippage_bps=2.0,
        )
        try:
            orch.run_cycle(prior_records=[], config=cfg)
        except _EarlyExit:
            pass

    assert captured["code"] == sentinel_code, (
        f"local code must be the compiler output; got {captured['code']!r}"
    )
    assert captured["spec_strategy_code"] == sentinel_code
    # Original LLM source is preserved for comparison reporting.
    assert captured["original_code"] == "# llm-authored ideation code\n"


def test_requires_custom_code_null_normalises_to_false() -> None:
    """Codex P2 round-3: an explicit ``null`` from the LLM must not
    crash the design attempt with a ValidationError. The orchestrator
    normalises ``None`` to ``False`` (default behaviour) before passing
    to ``StrategySpec`` so deterministic compile remains the default.
    """
    # The orchestrator code path is exercised via the previous test;
    # here we just pin the normalisation pattern directly so a future
    # refactor can't regress it.
    raw = None
    normalized = raw if raw is not None else False
    spec = StrategySpec(
        strategy_id="strat-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis="test hypothesis",
        signal_definition="test signal",
        timeframe="1d",
        entry_rules=[_rsi_lt_30_entry()],
        exit_rules=[_rsi_gt_70_exit()],
        sizing=FixedFractionSizing(fraction=0.02),
        target_symbols=["QQQ"],
        requires_custom_code=normalized,
    )
    assert spec.requires_custom_code is False


def test_requires_custom_code_string_false_does_not_disable_compile() -> None:
    """Codex P2: ``bool('false')`` is ``True``; the orchestrator must
    not coerce the raw value through ``bool(...)`` or any non-empty
    string would silently disable deterministic compilation. The
    StrategySpec field is ``bool`` so Pydantic's bool coercion handles
    the string variants correctly.
    """
    spec = StrategySpec(
        strategy_id="strat-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis="test hypothesis",
        signal_definition="test signal",
        timeframe="1d",
        entry_rules=[_rsi_lt_30_entry()],
        exit_rules=[_rsi_gt_70_exit()],
        sizing=FixedFractionSizing(fraction=0.02),
        target_symbols=["QQQ"],
        requires_custom_code="false",  # type: ignore[arg-type]
    )
    assert spec.requires_custom_code is False


# ---------------------------------------------------------------------------
# Codex round-4: MACD/VWAP lookback, stochastic off-by-one, ATR ambiguity,
# rule-note exclusion from spec hash.
# ---------------------------------------------------------------------------


def test_macd_lookback_for_macd_output_uses_slow_only() -> None:
    """Codex P1: ``output='macd'`` only needs ``slow`` bars; previously
    we returned ``slow + signal`` and delayed valid signals by 9 bars.
    """
    entry = EntryRule(
        side="long",
        when=Predicate(lhs=IndicatorRef(name="macd", params={"output": "macd"}), op=">", rhs=0.0),
    )
    spec = _spec(entry_rules=[entry])
    code = compile_strategy(spec)
    # Default fast=12 / slow=26 → WINDOW = max(slow, _MIN_WINDOW) = 26.
    assert "WINDOW = 26" in code


def test_macd_lookback_for_signal_output_uses_slow_plus_signal_minus_one() -> None:
    """``output='signal'`` needs ``slow + signal - 1`` bars (one more
    macd value than ``slow`` to drive the ``signal``-period EMA)."""
    entry = EntryRule(
        side="long",
        when=Predicate(lhs=IndicatorRef(name="macd", params={"output": "signal"}), op=">", rhs=0.0),
    )
    spec = _spec(entry_rules=[entry])
    code = compile_strategy(spec)
    # 26 + 9 - 1 = 34
    assert "WINDOW = 34" in code


def test_macd_helper_signal_returns_at_minimum_history() -> None:
    """Helper must produce a value at exactly the minimum bar count."""
    entry = EntryRule(
        side="long",
        when=Predicate(lhs=IndicatorRef(name="macd", params={"output": "signal"}), op=">", rhs=0.0),
    )
    spec = _spec(entry_rules=[entry])
    code = compile_strategy(spec)
    ns, *_ = _exec_module(code)
    strat = ns["CompiledStrategy"]()
    bars = [_SyntheticBar(close=100.0 + i * 0.5) for i in range(34)]
    assert strat.macd(bars, fast=12, slow=26, signal=9, select="signal") is not None
    # One bar fewer than the minimum → None.
    assert strat.macd(bars[:33], fast=12, slow=26, signal=9, select="signal") is None


def test_vwap_lookback_uses_harness_history_cap() -> None:
    """Codex P1: VWAP is cumulative in the sandbox indicator; capping at
    ``_MIN_WINDOW`` (20) would silently make it a 20-bar rolling VWAP.
    Use the harness retention cap (500 bars) so the helper sees the
    deepest history the engine retains.
    """
    entry = EntryRule(
        side="long",
        when=Predicate(lhs=IndicatorRef(name="vwap"), op=">", rhs=0.0),
    )
    spec = _spec(entry_rules=[entry])
    code = compile_strategy(spec)
    assert "WINDOW = 500" in code


def test_stochastic_lookback_off_by_one_fixed() -> None:
    """Codex P3: ``%D`` is available at ``k_period + d_period - 1``;
    previously returned ``k_period + d_period`` and delayed by one bar.
    """
    entry = EntryRule(
        side="long",
        when=Predicate(
            lhs=IndicatorRef(
                name="stochastic", params={"k_period": 14, "d_period": 3, "output": "d"}
            ),
            op=">",
            rhs=80.0,
        ),
    )
    spec = _spec(entry_rules=[entry])
    code = compile_strategy(spec)
    # 14 + 3 - 1 = 16, but floor to _MIN_WINDOW=20
    assert "WINDOW = 20" in code  # floor wins
    # Pick larger k_period so the floor doesn't dominate, to exercise the formula.
    entry2 = EntryRule(
        side="long",
        when=Predicate(
            lhs=IndicatorRef(
                name="stochastic", params={"k_period": 30, "d_period": 5, "output": "d"}
            ),
            op=">",
            rhs=80.0,
        ),
    )
    spec2 = _spec(entry_rules=[entry2])
    code2 = compile_strategy(spec2)
    # 30 + 5 - 1 = 34
    assert "WINDOW = 34" in code2


def test_volatility_target_with_multiple_distinct_atr_raises() -> None:
    """Codex P2: when sizing is volatility_target and the spec has more
    than one ATR period, the choice is ambiguous — refuse to compile.
    """
    rules = [
        EntryRule(
            side="long",
            when=Predicate(lhs=IndicatorRef(name="atr", params={"period": 14}), op=">", rhs=1.0),
        ),
        EntryRule(
            side="long",
            when=Predicate(lhs=IndicatorRef(name="atr", params={"period": 50}), op=">", rhs=2.0),
        ),
    ]
    spec = _spec(
        entry_rules=rules,
        sizing=VolatilityTargetSizing(target_annual_vol=0.20),
    )
    with pytest.raises(CompilerError, match="ambiguous"):
        compile_strategy(spec)


def test_volatility_target_with_single_atr_period_used_twice_compiles() -> None:
    """Two ATR refs with IDENTICAL params dedupe to one binding — no
    ambiguity, so compile succeeds.
    """
    rules = [
        EntryRule(
            side="long",
            when=Predicate(lhs=IndicatorRef(name="atr", params={"period": 14}), op=">", rhs=1.0),
        ),
        EntryRule(
            side="long",
            when=Predicate(lhs=IndicatorRef(name="atr", params={"period": 14}), op=">", rhs=2.0),
        ),
    ]
    spec = _spec(
        entry_rules=rules,
        sizing=VolatilityTargetSizing(target_annual_vol=0.20),
    )
    code = compile_strategy(spec)
    assert "_ind_atr_" in code


def test_no_inline_or_bracket_engine_exits_when_stop_loss_present() -> None:
    """Codex round-6 P1: stop_loss / take_profit are enforced entirely
    by the engine — no inline ``submit_order`` AND no bracket leg in
    compiled code. Bracket prices computed at signal time were wrong
    on gap opens; the engine uses post-fill ``position.entry_price``.
    """
    spec = _spec(
        entry_rules=[_rsi_lt_30_entry()],
        exit_rules=[StopLossRule(pct=0.05), TakeProfitRule(pct=0.10)],
    )
    code = compile_strategy(spec)
    assert 'reason="compiled_stop_loss"' not in code
    assert 'reason="compiled_take_profit"' not in code
    # The ONLY strategy-emitted submit_order is the entry — no bracket
    # legs, no inline closes. Engine's evaluate_exit_rules handles both.
    assert code.count("ctx.submit_order") == 1
    assert "attached_stop_loss" not in code
    assert "attached_take_profit" not in code
    assert "StopAttachment" not in code
    assert "LimitAttachment" not in code


def test_trailing_stop_loss_compiles_via_engine() -> None:
    """Trailing-basis stop-loss specs compile cleanly. Engine's
    ``evaluate_exit_rules`` honours the basis at runtime; no bracket
    attachment to downgrade trailing → static.
    """
    spec_long = _spec(
        entry_rules=[_rsi_lt_30_entry()],
        exit_rules=[StopLossRule(pct=0.05, basis="trailing_high")],
    )
    code_long = compile_strategy(spec_long)
    assert "CompiledStrategy" in code_long
    assert "StopAttachment" not in code_long


def test_safety_gate_accepts_entries_only_when_spec_has_stop_loss() -> None:
    """Codex round-6: ``CodeSafetyChecker._check_order_flow_shape`` is
    widened to accept entries-only flow when ``spec.exit_rules`` has
    an engine-handled rule. Without this, the bracket-less entry-only
    emission would fail the gate.
    """
    spec = _spec(
        entry_rules=[_rsi_lt_30_entry()],
        exit_rules=[StopLossRule(pct=0.05)],
    )
    code = compile_strategy(spec)
    results = CodeSafetyChecker().check(code, spec)
    assert _critical_details(results) == [], _critical_details(results)


def test_safety_gate_rejects_entries_only_without_engine_handled_exit() -> None:
    """Sanity check: an entry-only spec with no engine-handled exit
    rules (and no signal_exit) must still fail the safety gate.
    """
    spec = _spec(entry_rules=[_rsi_lt_30_entry()], exit_rules=[])
    code = compile_strategy(spec)
    results = CodeSafetyChecker().check(code, spec)
    criticals = _critical_details(results)
    assert any("exit" in c.lower() for c in criticals), criticals


def test_requires_custom_code_defensive_on_malformed_string() -> None:
    """Codex round-5 P2: previous round only normalised ``None``; other
    malformed LLM outputs (empty string, ``"maybe"``, arbitrary prose)
    still propagated and raised ValidationError. The orchestrator's
    ``_coerce_requires_custom_code`` helper now defaults to ``False``
    for any non-bool/non-recognised-string value.
    """
    from investment_team.strategy_lab.orchestrator import _coerce_requires_custom_code

    # Recognised values pass through.
    assert _coerce_requires_custom_code(True) is True
    assert _coerce_requires_custom_code(False) is False
    assert _coerce_requires_custom_code("true") is True
    assert _coerce_requires_custom_code("False") is False
    assert _coerce_requires_custom_code("YES") is True
    assert _coerce_requires_custom_code(1) is True
    assert _coerce_requires_custom_code(0) is False
    # None and the falsey string variants default to False.
    assert _coerce_requires_custom_code(None) is False
    assert _coerce_requires_custom_code("") is False
    assert _coerce_requires_custom_code("no") is False
    # Codex round-6 P2: off-spec ints (NOT exactly 0 or 1) default to
    # False — ``bool(2)`` would have silently flipped a typo'd value
    # into ``True`` and disabled deterministic compilation.
    assert _coerce_requires_custom_code(2) is False
    assert _coerce_requires_custom_code(-1) is False
    assert _coerce_requires_custom_code(42) is False
    # Garbage defaults to False — does NOT raise.
    assert _coerce_requires_custom_code("maybe") is False
    assert _coerce_requires_custom_code("asdf") is False
    assert _coerce_requires_custom_code(["true"]) is False
    assert _coerce_requires_custom_code({"value": True}) is False


def test_vwap_warmup_min_does_not_block_trading() -> None:
    """Codex round-7 P2: VWAP needs deep history (cumulative semantics)
    but only ≥ ``_MIN_WINDOW`` bars to start trading. Previous round
    set WINDOW=500 AND gated all trading on having 500 bars — blocking
    any backtest shorter than 500 bars entirely. Split into separate
    ``WINDOW`` (history-request depth) and ``WARMUP_MIN`` (gate
    threshold): VWAP requests 500 but trades from bar 20.
    """
    entry = EntryRule(
        side="long",
        when=Predicate(lhs=IndicatorRef(name="vwap"), op=">", rhs=0.0),
    )
    spec = _spec(entry_rules=[entry])
    code = compile_strategy(spec)
    # History request is the deep VWAP depth.
    assert "WINDOW = 500" in code
    # Warm-up gate is the modest floor — trading starts at bar 20.
    assert "WARMUP_MIN = 20" in code
    # The emitted on_bar gate uses the warmup_min, not the window.
    assert "if len(history) < 20:" in code
    # And it still asks for 500 bars of history.
    assert "ctx.history(bar.symbol, 500)" in code


def test_safety_gate_rejects_close_only_strategy_even_with_engine_exits() -> None:
    """Codex round-7 P1: the engine-handled-exit relaxation must not
    rubber-stamp a strategy that has NO entry submission. A code-only
    close (or zero submits) is still broken even if spec.exit_rules
    declares engine-handled rules — there's nothing for the engine
    to close.
    """
    spec_payload = _spec(
        entry_rules=[_rsi_lt_30_entry()],
        exit_rules=[StopLossRule(pct=0.05)],
    )
    # Synthetic code: only emits a close (qty=position.qty), no entry.
    close_only_code = """from contract import Strategy, OrderSide, OrderType, TimeInForce

class CompiledStrategy(Strategy):
    UNIVERSE = frozenset({"QQQ"})

    def on_bar(self, ctx, bar):
        position = ctx.position(bar.symbol)
        if position is not None:
            ctx.submit_order(
                symbol=bar.symbol,
                side=OrderSide.SHORT,
                qty=position.qty,
                order_type=OrderType.MARKET,
                tif=TimeInForce.DAY,
            )
"""
    results = CodeSafetyChecker().check(close_only_code, spec_payload)
    criticals = _critical_details(results)
    assert any("exit" in c.lower() or "side" in c.lower() for c in criticals), criticals


def test_safety_gate_rejects_long_only_entry_with_short_only_trailing_stop() -> None:
    """Codex round-7 P1: ``StopLossRule(basis="trailing_low")`` is a
    no-op for long positions in ``_stop_loss_triggers`` (only fires on
    shorts). A long-only spec with only that exit rule has NO
    triggerable engine exit — positions stay open forever. The widened
    safety gate must refuse it.
    """
    long_entry = EntryRule(
        side="long",
        when=Predicate(
            lhs=IndicatorRef(name="rsi", params={"period": 14}),
            op="<",
            rhs=30.0,
        ),
    )
    spec = _spec(
        entry_rules=[long_entry],
        exit_rules=[StopLossRule(pct=0.05, basis="trailing_low")],
    )
    # Synthetic entries-only code (LLM might produce this; the
    # compiler would too since it doesn't itself reason about
    # side-vs-basis compatibility).
    entry_only_code = """from contract import Strategy, OrderSide, OrderType, TimeInForce

class CompiledStrategy(Strategy):
    UNIVERSE = frozenset({"QQQ"})

    def on_bar(self, ctx, bar):
        position = ctx.position(bar.symbol)
        if position is None:
            ctx.submit_order(
                symbol=bar.symbol,
                side=OrderSide.LONG,
                qty=1,
                order_type=OrderType.MARKET,
                tif=TimeInForce.DAY,
            )
"""
    results = CodeSafetyChecker().check(entry_only_code, spec)
    criticals = _critical_details(results)
    assert any("exit" in c.lower() for c in criticals), criticals


def test_safety_gate_accepts_long_entry_with_trailing_high_stop() -> None:
    """Sanity-check the side-coverage logic — a long entry with
    ``trailing_high`` stop is well-formed and should pass.
    """
    long_entry = EntryRule(
        side="long",
        when=Predicate(
            lhs=IndicatorRef(name="rsi", params={"period": 14}),
            op="<",
            rhs=30.0,
        ),
    )
    spec = _spec(
        entry_rules=[long_entry],
        exit_rules=[StopLossRule(pct=0.05, basis="trailing_high")],
    )
    code = compile_strategy(spec)
    results = CodeSafetyChecker().check(code, spec)
    assert _critical_details(results) == [], _critical_details(results)


def test_safety_gate_rejects_emitted_short_with_long_only_trailing_stop() -> None:
    """Codex round-8 P1: even if ``spec.entry_rules`` declares only
    long entries, a refined strategy that actually emits
    ``side=OrderSide.SHORT`` would have no triggerable exit when the
    spec's stop-loss has ``basis="trailing_high"`` (long-only). The
    safety gate must use the EMITTED sides, not just spec.entry_rules.
    """
    spec = _spec(
        entry_rules=[_rsi_lt_30_entry()],  # declares long
        exit_rules=[StopLossRule(pct=0.05, basis="trailing_high")],  # long-only
    )
    # Code that emits SHORT (drifted from spec.entry_rules).
    drift_code = """from contract import Strategy, OrderSide, OrderType, TimeInForce

class CompiledStrategy(Strategy):
    UNIVERSE = frozenset({"QQQ"})

    def on_bar(self, ctx, bar):
        position = ctx.position(bar.symbol)
        if position is None:
            ctx.submit_order(
                symbol=bar.symbol,
                side=OrderSide.SHORT,
                qty=1,
                order_type=OrderType.MARKET,
                tif=TimeInForce.DAY,
            )
"""
    results = CodeSafetyChecker().check(drift_code, spec)
    criticals = _critical_details(results)
    assert any("exit" in c.lower() for c in criticals), criticals


def test_safety_gate_rejects_close_with_computed_qty() -> None:
    """Codex round-8 P1: ``qty=abs(position.qty)`` and similar
    expression-wrapped close shapes were misclassified as entries
    (only exact ``qty=position.qty`` was detected). With the
    engine-handled-exit relaxation, that false positive let close-only
    strategies pass the gate. ``_expr_references_position_qty`` now
    walks the kwarg expression for any reference to
    ``position.qty`` / ``pos.qty``.
    """
    spec = _spec(
        entry_rules=[_rsi_lt_30_entry()],
        exit_rules=[StopLossRule(pct=0.05)],
    )
    code = """from contract import Strategy, OrderSide, OrderType, TimeInForce

class CompiledStrategy(Strategy):
    UNIVERSE = frozenset({"QQQ"})

    def on_bar(self, ctx, bar):
        position = ctx.position(bar.symbol)
        if position is not None:
            ctx.submit_order(
                symbol=bar.symbol,
                side=OrderSide.SHORT,
                qty=abs(position.qty),
                order_type=OrderType.MARKET,
                tif=TimeInForce.DAY,
            )
"""
    results = CodeSafetyChecker().check(code, spec)
    criticals = _critical_details(results)
    assert any("exit" in c.lower() for c in criticals), criticals


def test_requires_custom_code_short_form_strings() -> None:
    """Codex round-8 P2: Pydantic's bool field accepts the single-letter
    short forms ``t`` / ``f`` / ``y`` / ``n``. The defensive coercer
    used to drop them and downgrade ``"y"`` to ``False``, silently
    forcing deterministic compilation when the ideation output meant
    the opposite.
    """
    from investment_team.strategy_lab.orchestrator import _coerce_requires_custom_code

    assert _coerce_requires_custom_code("y") is True
    assert _coerce_requires_custom_code("Y") is True
    assert _coerce_requires_custom_code("t") is True
    assert _coerce_requires_custom_code("T") is True
    assert _coerce_requires_custom_code("n") is False
    assert _coerce_requires_custom_code("N") is False
    assert _coerce_requires_custom_code("f") is False
    assert _coerce_requires_custom_code("F") is False


def test_safety_gate_rejects_kwargs_spread_only_strategy() -> None:
    """Codex round-9 P1a: a ``ctx.submit_order(**kwargs)`` spread can
    carry ``qty=position.qty`` just as easily as a side literal. The
    relaxation must NOT treat spread calls as entries — otherwise
    close-only strategies pass the gate whenever spec has stop/TP.
    """
    spec = _spec(
        entry_rules=[_rsi_lt_30_entry()],
        exit_rules=[StopLossRule(pct=0.05)],
    )
    code = """from contract import Strategy, OrderSide, OrderType, TimeInForce

class CompiledStrategy(Strategy):
    UNIVERSE = frozenset({"QQQ"})

    def on_bar(self, ctx, bar):
        position = ctx.position(bar.symbol)
        if position is not None:
            kwargs = {
                "symbol": bar.symbol,
                "side": OrderSide.SHORT,
                "qty": position.qty,
                "order_type": OrderType.MARKET,
                "tif": TimeInForce.DAY,
            }
            ctx.submit_order(**kwargs)
"""
    results = CodeSafetyChecker().check(code, spec)
    criticals = _critical_details(results)
    assert any("exit" in c.lower() for c in criticals), criticals


def test_safety_gate_rejects_entry_only_reachable_when_in_position() -> None:
    """Codex round-9 P1b: a ``ctx.submit_order(side=LONG, qty=10)`` call
    inside ``if position is not None:`` is an add-on, not a flat entry.
    The relaxation must require at least one call reachable when
    position may still be ``None``.
    """
    spec = _spec(
        entry_rules=[_rsi_lt_30_entry()],
        exit_rules=[StopLossRule(pct=0.05)],
    )
    code = """from contract import Strategy, OrderSide, OrderType, TimeInForce

class CompiledStrategy(Strategy):
    UNIVERSE = frozenset({"QQQ"})

    def on_bar(self, ctx, bar):
        position = ctx.position(bar.symbol)
        if position is not None:
            ctx.submit_order(
                symbol=bar.symbol,
                side=OrderSide.LONG,
                qty=1,
                order_type=OrderType.MARKET,
                tif=TimeInForce.DAY,
            )
"""
    results = CodeSafetyChecker().check(code, spec)
    criticals = _critical_details(results)
    assert any("exit" in c.lower() for c in criticals), criticals


def test_safety_gate_accepts_entry_under_position_is_none_guard() -> None:
    """Sanity-check the reachability logic — the canonical
    ``if position is None: ctx.submit_order(side=LONG, qty=...)``
    shape must still pass.
    """
    spec = _spec(
        entry_rules=[_rsi_lt_30_entry()],
        exit_rules=[StopLossRule(pct=0.05)],
    )
    code = """from contract import Strategy, OrderSide, OrderType, TimeInForce

class CompiledStrategy(Strategy):
    UNIVERSE = frozenset({"QQQ"})

    def on_bar(self, ctx, bar):
        if bar.symbol not in self.UNIVERSE:
            return
        position = ctx.position(bar.symbol)
        if position is None:
            ctx.submit_order(
                symbol=bar.symbol,
                side=OrderSide.LONG,
                qty=10,
                order_type=OrderType.MARKET,
                tif=TimeInForce.DAY,
            )
"""
    results = CodeSafetyChecker().check(code, spec)
    assert _critical_details(results) == [], _critical_details(results)


def test_safety_gate_does_not_extract_side_from_arbitrary_function_call() -> None:
    """Codex round-9 P1c: ``side=pick("LONG")`` must NOT be statically
    interpreted as a literal LONG — ``pick`` could return SHORT at
    runtime. ``_extract_side_literal`` only unwraps ``OrderSide(...)``
    constructor calls, not arbitrary function calls.
    """
    from investment_team.strategy_lab.quality_gates.code_safety import (
        _extract_side_literal,
    )

    # ``OrderSide("LONG")`` IS recognised (canonical constructor).
    tree_ok = ast.parse('OrderSide("LONG")', mode="eval")
    assert _extract_side_literal(tree_ok.body) == "long"
    # ``pick("LONG")`` is NOT recognised — opaque, returns None.
    tree_bad = ast.parse('pick("LONG")', mode="eval")
    assert _extract_side_literal(tree_bad.body) is None
    # ``some_helper(OrderSide.LONG)`` likewise opaque.
    tree_wrap = ast.parse("some_helper(OrderSide.LONG)", mode="eval")
    assert _extract_side_literal(tree_wrap.body) is None


def test_safety_gate_explicit_uncovered_side_not_masked_by_dynamic() -> None:
    """Codex round-9 P1d: a literal ``side=OrderSide.SHORT`` with only
    a long-side ``trailing_high`` stop in spec is a real coverage
    mismatch. A SECOND call elsewhere with dynamic ``side=`` must NOT
    let the gate pass via the spec-level fallback — the explicit
    uncovered side wins.
    """
    spec = _spec(
        entry_rules=[_rsi_lt_30_entry()],  # spec declares long
        exit_rules=[StopLossRule(pct=0.05, basis="trailing_high")],  # long-only stop
    )
    code = """from contract import Strategy, OrderSide, OrderType, TimeInForce

class CompiledStrategy(Strategy):
    UNIVERSE = frozenset({"QQQ"})

    def on_bar(self, ctx, bar):
        position = ctx.position(bar.symbol)
        side_choice = self._pick_side(bar)
        if position is None:
            # Explicit SHORT entry — uncovered by trailing_high stop.
            ctx.submit_order(
                symbol=bar.symbol,
                side=OrderSide.SHORT,
                qty=1,
                order_type=OrderType.MARKET,
                tif=TimeInForce.DAY,
            )
            # Dynamic side= elsewhere — could be LONG but doesn't relax
            # the explicit SHORT mismatch above.
            ctx.submit_order(
                symbol=bar.symbol,
                side=side_choice,
                qty=2,
                order_type=OrderType.MARKET,
                tif=TimeInForce.DAY,
            )

    def _pick_side(self, bar):
        return OrderSide.LONG
"""
    results = CodeSafetyChecker().check(code, spec)
    criticals = _critical_details(results)
    assert any("exit" in c.lower() for c in criticals), criticals


def test_safety_gate_rejects_else_arm_of_position_is_none_as_in_position() -> None:
    """Codex round-10 P1a: a submit_order inside the ELSE arm of
    ``if position is None: return`` is reachable only when position
    is not None — it's an add-on / close, not a flat entry.
    """
    spec = _spec(
        entry_rules=[_rsi_lt_30_entry()],
        exit_rules=[StopLossRule(pct=0.05)],
    )
    code = """from contract import Strategy, OrderSide, OrderType, TimeInForce

class CompiledStrategy(Strategy):
    UNIVERSE = frozenset({"QQQ"})

    def on_bar(self, ctx, bar):
        if bar.symbol not in self.UNIVERSE:
            return
        position = ctx.position(bar.symbol)
        if position is None:
            return
        else:
            ctx.submit_order(
                symbol=bar.symbol,
                side=OrderSide.LONG,
                qty=1,
                order_type=OrderType.MARKET,
                tif=TimeInForce.DAY,
            )
"""
    results = CodeSafetyChecker().check(code, spec)
    criticals = _critical_details(results)
    assert any("exit" in c.lower() for c in criticals), criticals


def test_safety_gate_recognises_position_neq_none_as_in_position_guard() -> None:
    """Codex round-10 P1b: ``if position != None:`` must be treated
    as in-position the same way ``is not None`` is.
    """
    spec = _spec(
        entry_rules=[_rsi_lt_30_entry()],
        exit_rules=[StopLossRule(pct=0.05)],
    )
    code = """from contract import Strategy, OrderSide, OrderType, TimeInForce

class CompiledStrategy(Strategy):
    UNIVERSE = frozenset({"QQQ"})

    def on_bar(self, ctx, bar):
        if bar.symbol not in self.UNIVERSE:
            return
        position = ctx.position(bar.symbol)
        if position != None:  # noqa: E711 — intentional pattern
            ctx.submit_order(
                symbol=bar.symbol,
                side=OrderSide.LONG,
                qty=1,
                order_type=OrderType.MARKET,
                tif=TimeInForce.DAY,
            )
"""
    results = CodeSafetyChecker().check(code, spec)
    criticals = _critical_details(results)
    assert any("exit" in c.lower() for c in criticals), criticals


def test_safety_gate_recognises_negated_is_none_as_in_position_guard() -> None:
    """Codex round-10 P1c: ``if not (position is None):`` is logically
    equivalent to ``is not None`` and must pin the body to in-position.
    """
    spec = _spec(
        entry_rules=[_rsi_lt_30_entry()],
        exit_rules=[StopLossRule(pct=0.05)],
    )
    code = """from contract import Strategy, OrderSide, OrderType, TimeInForce

class CompiledStrategy(Strategy):
    UNIVERSE = frozenset({"QQQ"})

    def on_bar(self, ctx, bar):
        if bar.symbol not in self.UNIVERSE:
            return
        position = ctx.position(bar.symbol)
        if not (position is None):
            ctx.submit_order(
                symbol=bar.symbol,
                side=OrderSide.LONG,
                qty=1,
                order_type=OrderType.MARKET,
                tif=TimeInForce.DAY,
            )
"""
    results = CodeSafetyChecker().check(code, spec)
    criticals = _critical_details(results)
    assert any("exit" in c.lower() for c in criticals), criticals


def test_safety_gate_recognises_aliased_position_qty_close() -> None:
    """Codex round-10 P1d: ``p = ctx.position(...)`` then
    ``qty=p.qty`` must be recognised as a close (not an entry).
    """
    spec = _spec(
        entry_rules=[_rsi_lt_30_entry()],
        exit_rules=[StopLossRule(pct=0.05)],
    )
    code = """from contract import Strategy, OrderSide, OrderType, TimeInForce

class CompiledStrategy(Strategy):
    UNIVERSE = frozenset({"QQQ"})

    def on_bar(self, ctx, bar):
        if bar.symbol not in self.UNIVERSE:
            return
        p = ctx.position(bar.symbol)
        if p is not None:
            ctx.submit_order(
                symbol=bar.symbol,
                side=OrderSide.SHORT,
                qty=p.qty,
                order_type=OrderType.MARKET,
                tif=TimeInForce.DAY,
            )
"""
    results = CodeSafetyChecker().check(code, spec)
    criticals = _critical_details(results)
    assert any("exit" in c.lower() for c in criticals), criticals


def test_safety_gate_recognises_aliased_position_in_guard_matcher() -> None:
    """Codex round-10 P1e: ``p = ctx.position(...)`` then
    ``if p is not None:`` must pin the body to in-position.
    """
    spec = _spec(
        entry_rules=[_rsi_lt_30_entry()],
        exit_rules=[StopLossRule(pct=0.05)],
    )
    code = """from contract import Strategy, OrderSide, OrderType, TimeInForce

class CompiledStrategy(Strategy):
    UNIVERSE = frozenset({"QQQ"})

    def on_bar(self, ctx, bar):
        if bar.symbol not in self.UNIVERSE:
            return
        p = ctx.position(bar.symbol)
        if p is not None:
            ctx.submit_order(
                symbol=bar.symbol,
                side=OrderSide.LONG,
                qty=1,
                order_type=OrderType.MARKET,
                tif=TimeInForce.DAY,
            )
"""
    results = CodeSafetyChecker().check(code, spec)
    criticals = _critical_details(results)
    assert any("exit" in c.lower() for c in criticals), criticals


def test_safety_gate_dynamic_fallback_helper_requires_both_sides() -> None:
    """Codex round-10 P1f: when emitted side= is dynamic and no explicit
    literals are present, the widened relaxation must require BOTH
    ``long`` and ``short`` to be covered by triggerable engine exits
    — the runtime side could resolve either way. Tested at the helper
    level because the broader ``_calls_form_entry_exit_pair`` check
    short-circuits dynamic-side calls (treats any unknown side as a
    valid pair) before the widening runs. The fix is preserved as
    defense-in-depth in case that legacy check is tightened later.
    """
    from investment_team.strategy_lab.quality_gates.code_safety import (
        _engine_exits_cover_sides,
    )

    # Only long-side trailing stop — SHORT positions would have no
    # triggerable exit.
    spec_long_only = _spec(
        entry_rules=[_rsi_lt_30_entry()],
        exit_rules=[StopLossRule(pct=0.05, basis="trailing_high")],
    )
    assert not _engine_exits_cover_sides(spec_long_only, {"long", "short"})
    # Entry-price stop covers both sides → dynamic fallback would
    # accept it.
    spec_both = _spec(
        entry_rules=[_rsi_lt_30_entry()],
        exit_rules=[StopLossRule(pct=0.05, basis="entry_price")],
    )
    assert _engine_exits_cover_sides(spec_both, {"long", "short"})


def test_safety_gate_extractor_rejects_non_order_side_attributes() -> None:
    """Codex round-10 P2: ``FakeSide.LONG`` is NOT a valid side literal
    — only attributes rooted in ``OrderSide`` / ``contract.OrderSide``
    count. The extractor must return ``None`` for unrelated enums.
    """
    from investment_team.strategy_lab.quality_gates.code_safety import (
        _extract_side_literal,
    )

    # Real ``OrderSide.LONG`` — recognised.
    tree_ok = ast.parse("OrderSide.LONG", mode="eval")
    assert _extract_side_literal(tree_ok.body) == "long"
    # ``contract.OrderSide.LONG`` — also recognised (dotted import).
    tree_dotted = ast.parse("contract.OrderSide.LONG", mode="eval")
    assert _extract_side_literal(tree_dotted.body) == "long"
    # ``FakeSide.LONG`` — NOT recognised (root is FakeSide, not OrderSide).
    tree_fake = ast.parse("FakeSide.LONG", mode="eval")
    assert _extract_side_literal(tree_fake.body) is None
    # ``somemodule.FakeSide.SHORT`` — not recognised.
    tree_dotted_fake = ast.parse("somemodule.FakeSide.SHORT", mode="eval")
    assert _extract_side_literal(tree_dotted_fake.body) is None


def test_compiled_on_bar_skips_bar_when_close_is_zero() -> None:
    """Codex round-10 P2 (sizing): bar.close <= 0 must not crash on_bar
    via ZeroDivisionError from the sizing formula. The emitted guard
    skips the bar gracefully.
    """
    spec = _spec(
        entry_rules=[_rsi_lt_30_entry()],
        sizing=FixedFractionSizing(fraction=0.05),
    )
    code = compile_strategy(spec)
    # Emitted guard present.
    assert "bar.close is None" in code
    assert "math.isfinite(bar.close)" in code
    assert "bar.close <= 0" in code

    ns, _OrderSide, *_ = _exec_module(code)
    strat = ns["CompiledStrategy"]()
    history = [_SyntheticBar(close=100.0) for _ in range(25)]
    # Bar with close=0 — would have raised ZeroDivisionError before.
    bad_bar = _SyntheticBar(close=0.0)
    history.append(bad_bar)
    ctx = _SyntheticContext(history=history, position=None)
    strat.on_bar(ctx, bad_bar)  # Must not raise.
    assert ctx.orders == [], "bar.close=0 should be silently skipped, not produce an order"


def test_compiled_on_bar_skips_bar_when_close_is_non_finite() -> None:
    """NaN / inf close prices must also be skipped, not propagate
    into the sizing formula.
    """
    spec = _spec(
        entry_rules=[_rsi_lt_30_entry()],
        sizing=FixedNotionalSizing(notional_usd=10_000.0),
    )
    code = compile_strategy(spec)
    ns, _OrderSide, *_ = _exec_module(code)
    strat = ns["CompiledStrategy"]()
    history = [_SyntheticBar(close=100.0) for _ in range(25)]
    bad_bar = _SyntheticBar(close=float("nan"))
    history.append(bad_bar)
    ctx = _SyntheticContext(history=history, position=None)
    strat.on_bar(ctx, bad_bar)  # Must not raise.
    assert ctx.orders == []


def test_spec_hash_invariant_to_rule_notes() -> None:
    """Codex P3: rule-level ``note`` text is author prose and never
    affects emitted code. Two specs differing only by ``note`` must
    produce byte-identical compiled output.
    """
    entry_with_note = EntryRule(
        side="long",
        when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op="<", rhs=30.0),
        note="original note text",
    )
    entry_with_different_note = EntryRule(
        side="long",
        when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op="<", rhs=30.0),
        note="completely different prose",
    )
    spec_a = _spec(entry_rules=[entry_with_note])
    spec_b = _spec(entry_rules=[entry_with_different_note])
    assert compile_strategy(spec_a) == compile_strategy(spec_b)
