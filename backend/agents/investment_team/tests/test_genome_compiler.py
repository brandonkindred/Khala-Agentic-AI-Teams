"""Compiler tests for the Strategy Lab factor DSL (issue #249).

These tests are the regression net for the indentation bug fixed in
PR #356 (codex review thread): every emitted module must be parseable
Python AND must execute end-to-end through ``contract.Strategy`` /
``StrategyContext``.

We don't yet drive a full backtester through the compiled output —
``test_strategy_lab_genome_e2e.py`` lands separately and exercises the
orchestrator path.  Here we focus on:

* AST validity for every node-family combination,
* Determinism (same genome → byte-identical output),
* Sub-tree sharing (identical SMA(20) appearing twice → one helper),
* Live execution via ``StrategyContext`` so we know orders flow with
  the expected payload shape.
"""

from __future__ import annotations

import ast
import datetime as dt

import pytest

from investment_team.strategy_lab.factors import compile_genome
from investment_team.strategy_lab.factors.models import (
    ATR,
    EMA,
    RSI,
    SMA,
    ATRBreakout,
    BoolAnd,
    CompareGT,
    CompareLT,
    Const,
    CrossOver,
    CrossUnder,
    FixedQty,
    FundingRateDeviation,
    Genome,
    PctOfEquity,
    Price,
    TermStructureSlope,
    VolRegimeState,
    VolTargeted,
)
from investment_team.trading_service.strategy.contract import (
    Bar,
    StrategyContext,
)


def _g(entry, exit_, sizing=None, asset_class="stocks", hypothesis=""):
    return Genome(
        asset_class=asset_class,
        hypothesis=hypothesis,
        signal_definition="",
        entry=entry,
        exit=exit_,
        sizing=sizing or FixedQty(qty=1),
    )


def _ramp_bars(n: int, base: float = 100.0, step: float = 1.0):
    base_date = dt.date(2026, 1, 1)
    out = []
    for i in range(n):
        px = base + i * step
        out.append(
            Bar(
                symbol="AAA",
                timestamp=str(base_date + dt.timedelta(days=i)),
                open=px,
                high=px + 0.5,
                low=px - 0.5,
                close=px,
                volume=1000.0,
            )
        )
    return out


# ---------------------------------------------------------------------------
# AST validity — covers every primitive family.  This is the regression
# guard for the indentation bug that prompted this test file.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,genome",
    [
        # Boolean comparisons + numeric primitives.
        (
            "price_const",
            _g(
                CompareGT(left=Price(field="close"), right=Const(value=0.0)),
                CompareLT(left=Price(field="close"), right=Const(value=0.0)),
            ),
        ),
        # Cross detection — uses the bars[:-1] sliced helper trick.
        (
            "sma_crossover",
            _g(
                CrossOver(fast=SMA(period=5), slow=SMA(period=15)),
                CrossUnder(fast=SMA(period=5), slow=SMA(period=15)),
            ),
        ),
        # Indicator + sizing variant.
        (
            "rsi_breakout_pct",
            _g(
                CompareGT(left=RSI(period=14), right=Const(value=50)),
                CompareLT(left=RSI(period=14), right=Const(value=30)),
                sizing=PctOfEquity(pct=10),
            ),
        ),
        # ATR breakout — has its own inline helper template.
        (
            "atr_breakout",
            _g(
                ATRBreakout(k=20, atr_mult=1.0, atr_period=14),
                CompareLT(left=Price(field="close"), right=ATR(period=14)),
            ),
        ),
        # Compound boolean + vol regime + vol-targeted sizing.
        (
            "compound_voltargeted",
            _g(
                BoolAnd(
                    children=[
                        CrossOver(fast=EMA(period=12), slow=EMA(period=26)),
                        CompareGT(
                            left=VolRegimeState(lookback=60, threshold=1.2),
                            right=Const(value=0.0),
                        ),
                    ]
                ),
                CrossUnder(fast=EMA(period=12), slow=EMA(period=26)),
                sizing=VolTargeted(target_annual_vol=0.15, lookback=20),
            ),
        ),
        # Cross-asset primitives compile to NaN helpers — must still parse.
        (
            "term_structure_slope",
            _g(
                CompareGT(
                    left=TermStructureSlope(front_symbol="CL1", back_symbol="CL2", window=20),
                    right=Const(value=0.0),
                ),
                CompareLT(
                    left=TermStructureSlope(front_symbol="CL1", back_symbol="CL2", window=20),
                    right=Const(value=0.0),
                ),
            ),
        ),
        (
            "funding_rate_deviation",
            _g(
                CompareGT(
                    left=FundingRateDeviation(symbol="BTCUSDT", lookback=24),
                    right=Const(value=0.0),
                ),
                CompareLT(
                    left=FundingRateDeviation(symbol="BTCUSDT", lookback=24),
                    right=Const(value=0.0),
                ),
                asset_class="crypto",
            ),
        ),
    ],
)
def test_compiled_module_parses_as_python(name, genome):
    """Every emitted module must be valid Python (regression for PR #356)."""
    code = compile_genome(genome)
    # Must parse without raising — this is the bug class we just fixed.
    ast.parse(code)
    # And must obviously not start indented (the specific symptom Codex flagged).
    first_line = code.splitlines()[0]
    assert not first_line.startswith(" "), (
        f"genome {name!r}: emitted module starts with indented line: {first_line!r}"
    )


def test_compiled_module_imports_only_sandbox_whitelisted_modules():
    """Compiler output must only import from ``contract`` + stdlib."""
    code = compile_genome(
        _g(
            CrossOver(fast=SMA(period=5), slow=SMA(period=15)),
            CrossUnder(fast=SMA(period=5), slow=SMA(period=15)),
        )
    )
    tree = ast.parse(code)
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module.split(".")[0])
    # Compiler emits ``from contract import ...``, ``import math``, and
    # ``from collections import deque`` (deque backs the streaming MACD
    # cache's macd_line). All three are on the sandbox import whitelist.
    assert imported_modules <= {"collections", "contract", "math"}, imported_modules


# ---------------------------------------------------------------------------
# Determinism + sub-tree sharing.
# ---------------------------------------------------------------------------


def test_compile_is_deterministic():
    """Identical genomes must produce byte-identical output."""
    g1 = _g(
        CrossOver(fast=SMA(period=5), slow=SMA(period=15)),
        CrossUnder(fast=SMA(period=5), slow=SMA(period=15)),
    )
    g2 = _g(
        CrossOver(fast=SMA(period=5), slow=SMA(period=15)),
        CrossUnder(fast=SMA(period=5), slow=SMA(period=15)),
    )
    assert compile_genome(g1) == compile_genome(g2)


def test_shared_subtrees_compile_to_a_single_helper():
    """SMA(20) referenced in entry AND exit produces exactly one helper method.

    This is the DAG-sharing property the issue calls out.  We count
    occurrences of ``def _n_<id>(self, bars):`` blocks for the SMA(20)
    sub-tree in the emitted module.
    """
    sma20 = SMA(period=20)
    g = _g(
        # Both entry and exit reference the SAME SMA(20) instance shape.
        CompareGT(left=sma20, right=Const(value=100)),
        CompareLT(left=sma20, right=Const(value=100)),
    )
    code = compile_genome(g)
    # Count helper method defs (every `_n_<hash>` is a unique sub-tree).
    helper_defs = [line for line in code.splitlines() if line.lstrip().startswith("def _n_")]
    # Unique SMA(20) appears once + Const(100) once + two CompareGT/LT:
    # 4 helpers total.  The key invariant: SMA(20) is not duplicated, so
    # the total is bounded by 4 (would be 5 if SMA was emitted twice).
    assert len(helper_defs) == 4, helper_defs


# ---------------------------------------------------------------------------
# End-to-end execution — exec the compiled module and drive on_bar through
# the real ``StrategyContext`` so we catch any broken contract API usage.
# ---------------------------------------------------------------------------


def _exec_strategy(code: str):
    """Compile + exec a generated module and return the Strategy class."""
    ns: dict = {}
    # The generated code does ``from contract import OrderSide, OrderType, Strategy``.
    # The sandbox harness puts the contract module on sys.path; here we shim it
    # by injecting the real one as a top-level ``contract`` module.
    import sys

    from investment_team.trading_service.strategy import contract as _contract

    sys.modules.setdefault("contract", _contract)
    exec(compile(code, "<generated_strategy>", "exec"), ns)
    return ns["GeneratedStrategy"]


def test_compiled_strategy_emits_long_order_on_entry():
    """Always-fire ``Price > 0`` entry → emits an OrderSide.LONG order."""
    g = _g(
        CompareGT(left=Price(field="close"), right=Const(value=0.0)),
        CompareLT(left=Price(field="close"), right=Const(value=0.0)),
        sizing=FixedQty(qty=5),
    )
    StratCls = _exec_strategy(compile_genome(g))
    strat = StratCls()

    emitted = []
    ctx = StrategyContext(emit=lambda evt: emitted.append(evt))
    for bar in _ramp_bars(5):
        ctx._ingest_bar(bar)
        ctx._ingest_state(capital=100_000, equity=100_000, positions=[], is_warmup=False)
        strat.on_bar(ctx, bar)

    assert emitted, "expected at least one emitted order"
    first = emitted[0]
    assert first["kind"] == "order"
    assert first["payload"]["side"] == "long"
    assert first["payload"]["qty"] == 5
    assert first["payload"]["order_type"] == "market"
    assert first["payload"]["reason"] == "genome:entry"


def test_compiled_strategy_does_not_emit_orders_during_warmup():
    """A genome that needs MIN_HISTORY bars must not fire on the first few."""
    g = _g(
        CrossOver(fast=SMA(period=5), slow=SMA(period=15)),
        CrossUnder(fast=SMA(period=5), slow=SMA(period=15)),
    )
    StratCls = _exec_strategy(compile_genome(g))
    strat = StratCls()

    emitted = []
    ctx = StrategyContext(emit=lambda evt: emitted.append(evt))
    # Only 5 bars — well below the MIN_HISTORY of 16 (15 SMA + cross slack).
    for bar in _ramp_bars(5):
        ctx._ingest_bar(bar)
        ctx._ingest_state(capital=100_000, equity=100_000, positions=[], is_warmup=False)
        strat.on_bar(ctx, bar)

    assert emitted == [], f"strategy should be in warm-up, got: {emitted}"
