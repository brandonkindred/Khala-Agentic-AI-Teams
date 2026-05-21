"""Coverage for ``strategy_lab.factors.compiler.compile_genome``.

The compiler walks the genome tree and emits a left-aligned Python module
string. The tests construct genomes that exercise each combinator branch
and assert the rendered output contains the expected method names —
enough to drive the ``_visit`` dispatcher through every node type without
needing the full sandbox execution path.
"""

from __future__ import annotations

from investment_team.strategy_lab.factors import Genome, compile_genome
from investment_team.strategy_lab.factors.models import (
    EMA,
    RSI,
    SMA,
    ATRBreakout,
    BoolAnd,
    BoolNot,
    BoolOr,
    CompareGT,
    CompareLT,
    Const,
    CrossOver,
    CrossUnder,
    FixedQty,
    IfRegime,
    MACDSignal,
    Price,
    VolRegimeState,
    WeightedSum,
)


def _genome(entry, exit_):
    return Genome(
        asset_class="stocks",
        hypothesis="",
        signal_definition="",
        entry=entry,
        exit=exit_,
        sizing=FixedQty(qty=1),
    )


def test_compile_simple_compare_gt() -> None:
    g = _genome(
        CompareGT(left=SMA(period=20), right=Const(value=100)),
        CompareLT(left=SMA(period=20), right=Const(value=50)),
    )
    src = compile_genome(g)
    assert isinstance(src, str)
    # Each helper method should appear in the rendered module.
    assert "_n_" in src
    assert "def on_bar" in src


def test_compile_weighted_sum_combinator() -> None:
    g = _genome(
        CompareGT(
            left=WeightedSum(
                children=[SMA(period=10), SMA(period=20)],
                weights=[0.5, 0.5],
            ),
            right=Const(value=100),
        ),
        CompareLT(left=SMA(period=10), right=Const(value=50)),
    )
    src = compile_genome(g)
    # WeightedSum body uses "_v0" / "_v1" temporaries.
    assert "_v0" in src
    assert "any(math.isnan" in src


def test_compile_if_regime() -> None:
    g = _genome(
        CompareGT(
            left=IfRegime(
                gate=CompareGT(left=SMA(period=10), right=Const(value=1)),
                if_true=SMA(period=5),
                if_false=SMA(period=50),
            ),
            right=Const(value=10),
        ),
        CompareLT(left=SMA(period=20), right=Const(value=50)),
    )
    src = compile_genome(g)
    # IfRegime emits ``if bool(self._n_*(bars)):``
    assert "if bool(self._n_" in src


def test_compile_crossover_branches() -> None:
    g = _genome(
        CrossOver(fast=SMA(period=5), slow=SMA(period=20)),
        CrossUnder(fast=SMA(period=5), slow=SMA(period=20)),
    )
    src = compile_genome(g)
    # Cross_body emits both ``cmp_now`` and ``cmp_prev`` blocks.
    assert "len(bars) < 2" in src


def test_compile_atr_breakout() -> None:
    g = _genome(
        ATRBreakout(k=10, atr_period=14, atr_mult=2.0),
        CompareLT(left=SMA(period=20), right=Const(value=50)),
    )
    src = compile_genome(g)
    assert "atr" in src.lower()


def test_compile_bool_and_or_not() -> None:
    g_and = _genome(
        BoolAnd(children=[
            CompareGT(left=SMA(period=10), right=Const(value=1)),
            CompareLT(left=SMA(period=20), right=Const(value=200)),
        ]),
        CompareLT(left=SMA(period=20), right=Const(value=50)),
    )
    src_and = compile_genome(g_and)
    assert "_n_" in src_and

    g_or = _genome(
        BoolOr(children=[
            CompareGT(left=SMA(period=10), right=Const(value=1)),
            CompareGT(left=SMA(period=20), right=Const(value=2)),
        ]),
        CompareLT(left=SMA(period=20), right=Const(value=50)),
    )
    src_or = compile_genome(g_or)
    assert "_n_" in src_or

    g_not = _genome(
        BoolNot(child=CompareGT(left=SMA(period=10), right=Const(value=1))),
        CompareLT(left=SMA(period=20), right=Const(value=50)),
    )
    src_not = compile_genome(g_not)
    assert "_n_" in src_not


def test_compile_with_macd_and_rsi() -> None:
    """RSI + MACDSignal + Price + Const + EMA + VolRegimeState — exercise
    multiple numeric primitive templates in one module."""
    g = _genome(
        CompareGT(
            left=WeightedSum(
                children=[RSI(period=14), EMA(period=12), MACDSignal(fast=12, slow=26, signal=9)],
                weights=[0.4, 0.3, 0.3],
            ),
            right=Const(value=50),
        ),
        CompareLT(
            left=VolRegimeState(lookback=20, threshold=1.2),
            right=Price(field="close"),
        ),
    )
    src = compile_genome(g)
    assert isinstance(src, str)
    # The compiled module wires `entry`/`exit` and hoists every node into a helper.
    assert "def on_bar" in src
    # Every primitive registered as a method:
    assert src.count("def _n_") >= 4
