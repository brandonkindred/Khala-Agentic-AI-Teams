"""Additional coverage for ``strategy_lab.factors.tree_edit_distance``."""

from __future__ import annotations

from investment_team.strategy_lab.factors import (
    Genome,
    tree_edit_distance,
)
from investment_team.strategy_lab.factors.models import (
    EMA,
    RSI,
    SMA,
    BoolAnd,
    CompareGT,
    CompareLT,
    Const,
    CrossOver,
    FixedQty,
    Price,
)
from investment_team.strategy_lab.factors.tree_edit_distance import (
    mean_pairwise_distance,
    node_type_counts,
)


def _genome(entry, exit_) -> Genome:
    return Genome(
        asset_class="stocks",
        hypothesis="",
        signal_definition="",
        entry=entry,
        exit=exit_,
        sizing=FixedQty(qty=1),
    )


def test_node_type_counts_includes_type_tag_for_every_node() -> None:
    g = _genome(
        CompareGT(left=SMA(period=20), right=Const(value=100)),
        CompareLT(left=SMA(period=20), right=Const(value=100)),
    )
    counts = node_type_counts(g)
    # Discriminator tags: gt / lt / sma / const / fixed_qty.
    assert counts.get("gt", 0) >= 1
    assert counts.get("lt", 0) >= 1
    assert counts.get("sma", 0) >= 2
    assert counts.get("const", 0) >= 2


def test_tree_edit_distance_grows_with_extra_combinator() -> None:
    g1 = _genome(
        CompareGT(left=SMA(period=20), right=Const(value=100)),
        CompareLT(left=SMA(period=20), right=Const(value=100)),
    )
    g2 = _genome(
        BoolAnd(
            children=[
                CompareGT(left=EMA(period=20), right=Const(value=100)),
                CompareGT(left=RSI(period=14), right=Const(value=50)),
            ]
        ),
        CrossOver(fast=Price(field="close"), slow=SMA(period=20)),
    )
    assert tree_edit_distance(g1, g2) > 0


def test_mean_pairwise_distance_empty_or_singleton_returns_zero() -> None:
    assert mean_pairwise_distance([]) == 0.0
    g = _genome(
        CompareGT(left=SMA(period=20), right=Const(value=100)),
        CompareLT(left=SMA(period=20), right=Const(value=100)),
    )
    assert mean_pairwise_distance([g]) == 0.0


def test_mean_pairwise_distance_returns_average() -> None:
    g1 = _genome(
        CompareGT(left=SMA(period=20), right=Const(value=100)),
        CompareLT(left=SMA(period=20), right=Const(value=100)),
    )
    g2 = _genome(
        CompareGT(left=EMA(period=20), right=Const(value=100)),
        CompareLT(left=EMA(period=20), right=Const(value=100)),
    )
    g3 = _genome(
        CompareGT(left=RSI(period=14), right=Const(value=50)),
        CompareLT(left=RSI(period=14), right=Const(value=50)),
    )
    avg = mean_pairwise_distance([g1, g2, g3])
    # Every pair should differ at least by the swapped SMA/EMA/RSI node.
    assert avg > 0.0
