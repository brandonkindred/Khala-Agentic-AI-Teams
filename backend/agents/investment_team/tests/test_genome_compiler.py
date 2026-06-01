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


def _imported_modules(code: str) -> set[str]:
    tree = ast.parse(code)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    return imported


def test_compiled_module_imports_only_sandbox_whitelisted_modules():
    """Non-MACD genome emits only ``contract`` + ``math``. ``collections``
    is gated on the genome actually using MACDSignal — see the next test."""
    code = compile_genome(
        _g(
            CrossOver(fast=SMA(period=5), slow=SMA(period=15)),
            CrossUnder(fast=SMA(period=5), slow=SMA(period=15)),
        )
    )
    assert _imported_modules(code) == {"contract", "math"}


def test_compiled_macd_genome_imports_collections_for_deque():
    """When the genome includes a MACDSignal node, ``from collections
    import deque`` is emitted to back the cached macd_line. Gating the
    import on actual usage keeps non-MACD strategies clean and avoids
    spurious F401 under any future linter pass on generated code."""
    from investment_team.strategy_lab.factors.models import MACDSignal

    code = compile_genome(
        _g(
            CompareGT(left=MACDSignal(fast=12, slow=26, signal=9), right=Const(value=0.0)),
            CompareLT(left=MACDSignal(fast=12, slow=26, signal=9), right=Const(value=0.0)),
        )
    )
    assert _imported_modules(code) == {"collections", "contract", "math"}


def test_compiled_macd_genome_cache_key_embeds_source():
    """The factors MACDSignal helper's cache key must include the
    ``source`` axis (``_close`` suffix) so any future DSL extension
    adding a non-close source cannot silently collide on the same
    ``(fast, slow, signal, symbol)`` tuple. Matches the registry and
    synthesis key shapes ``(name, symbol, fast, slow, signal, source)``."""
    from investment_team.strategy_lab.factors.models import MACDSignal

    code = compile_genome(
        _g(
            CompareGT(left=MACDSignal(fast=12, slow=26, signal=9), right=Const(value=0.0)),
            CompareLT(left=MACDSignal(fast=12, slow=26, signal=9), right=Const(value=0.0)),
        )
    )
    assert "macd_signal_12_26_9_close" in code, (
        "factors cache key must embed the source axis for forward-compat"
    )


def test_compiled_macd_rejects_non_integer_params_at_compile_time():
    """The factors MACDSignal template interpolates fast/slow/signal as
    raw literals: ``not ({signal} >= 2)``. A NaN-valued ``signal`` would
    emit unbound identifier ``nan`` and produce ``NameError`` at module
    exec time. Reject malformed params loudly at compile time rather
    than letting the bad source string through.

    Pydantic's ge=2 constraint normally catches malformed params at
    model construction, so this defence-in-depth path is reachable
    only via ``model_construct`` (validation bypass) or future schema
    drift. ``model_construct`` simulates the bypass.
    """
    from investment_team.strategy_lab.factors.models import MACDSignal

    # NaN signal — simulates a validation-bypass path that lets a NaN
    # slip into the genome. The compile-time gate must catch this
    # before the unbound ``nan`` identifier is emitted.
    nan_node = MACDSignal.model_construct(fast=12, slow=26, signal=float("nan"))
    with pytest.raises(TypeError, match="must be an int"):
        compile_genome(
            _g(
                CompareGT(left=nan_node, right=Const(value=0.0)),
                CompareLT(left=Const(value=0.0), right=Const(value=0.0)),
            )
        )
    # Out-of-range int (Pydantic ge=2 normally rejects this).
    bad_node = MACDSignal.model_construct(fast=1, slow=26, signal=9)
    with pytest.raises(ValueError, match=">= 2"):
        compile_genome(
            _g(
                CompareGT(left=bad_node, right=Const(value=0.0)),
                CompareLT(left=Const(value=0.0), right=Const(value=0.0)),
            )
        )


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


def test_atr_breakout_reuses_shared_atr_helper():
    """ATRBreakout and an ATR primitive of the same period collapse to one
    ATR computation (common-sub-expression elimination).

    Before CSE, ATRBreakout inlined its own true-range loop while the ATR
    primitive emitted a second one — the same series computed twice. After
    CSE both route through a single ``_n_<id>`` helper.
    """
    g = _g(
        ATRBreakout(k=20, atr_mult=1.0, atr_period=14),
        CompareLT(left=Price(field="close"), right=ATR(period=14)),
    )
    code = compile_genome(g)
    # The true-range loop body (``return sum(_trs) / 14``) must appear exactly
    # once — i.e. the ATR series is computed a single time.
    assert code.count("return sum(_trs) / 14") == 1, code
    # ATRBreakout reads ATR from the shared helper rather than inlining it.
    assert "_atr = self._n_" in code


def test_atr_breakout_without_separate_atr_node_still_emits_one_helper():
    """An ATRBreakout on its own emits exactly one ATR helper and parses."""
    g = _g(
        ATRBreakout(k=10, atr_period=14, atr_mult=2.0),
        CompareLT(left=SMA(period=20), right=Const(value=50)),
    )
    code = compile_genome(g)
    assert code.count("return sum(_trs) / 14") == 1, code
    ast.parse(code)  # still valid Python


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
