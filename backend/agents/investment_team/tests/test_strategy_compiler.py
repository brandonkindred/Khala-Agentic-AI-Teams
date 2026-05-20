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


def test_stop_loss_present_compiles_clean() -> None:
    spec = _spec(
        entry_rules=[_rsi_lt_30_entry()],
        exit_rules=[StopLossRule(pct=0.05)],
    )
    code = compile_strategy(spec)
    # Inline exit branch threshold computed against position.entry_price;
    # close fires when bar.low (long) / bar.high (short) crosses it.
    assert "position.entry_price * (1.0 - 0.05)" in code
    assert "position.entry_price * (1.0 + 0.05)" in code
    assert 'reason="compiled_stop_loss"' in code


def test_take_profit_present_compiles_clean() -> None:
    spec = _spec(
        entry_rules=[_rsi_lt_30_entry()],
        exit_rules=[TakeProfitRule(pct=0.10)],
    )
    code = compile_strategy(spec)
    assert "position.entry_price * (1.0 + 0.1)" in code
    assert "position.entry_price * (1.0 - 0.1)" in code
    assert 'reason="compiled_take_profit"' in code


def test_trailing_stop_loss_raises_compiler_error() -> None:
    spec = _spec(
        entry_rules=[_rsi_lt_30_entry()],
        exit_rules=[StopLossRule(pct=0.05, basis="trailing_high")],
    )
    with pytest.raises(CompilerError, match="trailing"):
        compile_strategy(spec)


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
    assert "self._prev_" in code  # cross_* state slot present
    assert "position.entry_price" in code  # stop/take-profit gate compliance


def test_cross_above_emits_prev_state() -> None:
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
    # __init__ assigns prev slots; on_bar updates them at the end.
    tree = ast.parse(code)
    init_methods = [
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "__init__"
    ]
    assert init_methods, "compiled class must define __init__ for cross-state"
    prev_attr_assignments = [
        node
        for method in init_methods
        for node in ast.walk(method)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Attribute) and t.attr.startswith("_prev_") for t in node.targets)
    ]
    assert len(prev_attr_assignments) >= 2


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

    fake_contract.Strategy = _Strategy  # type: ignore[attr-defined]
    fake_contract.OrderSide = _OrderSide  # type: ignore[attr-defined]
    fake_contract.OrderType = _OrderType  # type: ignore[attr-defined]
    fake_contract.TimeInForce = _TimeInForce  # type: ignore[attr-defined]
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


def test_simultaneous_stop_and_take_profit_emits_one_close() -> None:
    """Codex P1 review: with both thresholds met on one bar, only the
    first matching exit may emit a submit_order — otherwise the second
    submit would open a reverse position after the first closes.
    """
    spec = _spec(
        entry_rules=[_rsi_lt_30_entry()],
        exit_rules=[StopLossRule(pct=0.05), TakeProfitRule(pct=0.10)],
    )
    code = compile_strategy(spec)
    assert "exit_submitted" in code
    ns, _OrderSide, *_ = _exec_module(code)
    strat = ns["CompiledStrategy"]()

    # Bar that flushes both thresholds — high spike AND low drop on the
    # same candle relative to entry_price=100.
    flush_bar = _SyntheticBar(symbol="QQQ", open=100.0, high=115.0, low=90.0, close=100.0)
    history = [_SyntheticBar(close=100.0) for _ in range(25)] + [flush_bar]
    position = _SyntheticPosition(side=_OrderSide.LONG, qty=10.0, entry_price=100.0)
    ctx = _SyntheticContext(history=history, position=position)
    strat.on_bar(ctx, flush_bar)
    # Exactly one close order — not two — even though both stop_loss and
    # take_profit thresholds are simultaneously satisfied.
    assert len(ctx.orders) == 1, ctx.orders
    order = ctx.orders[0]
    assert order["qty"] == position.qty
    assert order["side"] == _OrderSide.SHORT


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
