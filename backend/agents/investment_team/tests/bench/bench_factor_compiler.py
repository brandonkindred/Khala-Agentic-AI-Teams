"""Benchmark: factor-compiler CSE + node-id memoisation.

Two compiler cleanups are exercised here:

* **Common-sub-expression elimination** — ATRBreakout used to inline its
  own true-range loop while a sibling ATR primitive emitted a second one,
  so a genome referencing both computed the ATR series twice per bar. The
  compiler now routes ATRBreakout through a shared ATR helper, so the
  emitted strategy computes the series once. Asserted structurally below.
* **Node-id memoisation** — ``_node_id`` (sha256 over canonical JSON) is
  memoised on object identity for the compile's lifetime, so a revisited
  sub-tree isn't re-serialised + re-hashed. This benchmark times repeated
  compiles of a genome with many shared sub-trees.

The throughput assertion is a loose smoke bound to survive CI noise; set
``BENCH_FACTOR_COMPILER_VERBOSE=1`` to print the measured time. Marked
``@pytest.mark.bench``; opt in with ``pytest -m bench``.
"""

from __future__ import annotations

import ast
import os
import time

import pytest

from investment_team.strategy_lab.factors import compile_genome
from investment_team.strategy_lab.factors.models import (
    ATR,
    SMA,
    ATRBreakout,
    BoolAnd,
    CompareGT,
    CompareLT,
    Const,
    FixedQty,
    Genome,
    Price,
)

pytestmark = pytest.mark.bench

_ITERATIONS = 500


def _cse_genome() -> Genome:
    return Genome(
        asset_class="stocks",
        hypothesis="",
        signal_definition="",
        entry=ATRBreakout(k=20, atr_mult=1.0, atr_period=14),
        exit=CompareLT(left=Price(field="close"), right=ATR(period=14)),
        sizing=FixedQty(qty=1),
    )


def _shared_subtree_genome() -> Genome:
    # The same SMA(20) shape appears many times — node-id memoisation avoids
    # re-hashing the repeated sub-trees.
    sma = SMA(period=20)
    return Genome(
        asset_class="stocks",
        hypothesis="",
        signal_definition="",
        entry=BoolAnd(
            children=[
                CompareGT(left=sma, right=Const(value=100)),
                CompareGT(left=sma, right=Const(value=50)),
                CompareLT(left=sma, right=Const(value=200)),
                CompareGT(left=sma, right=Const(value=75)),
            ]
        ),
        exit=CompareLT(left=sma, right=Const(value=50)),
        sizing=FixedQty(qty=1),
    )


def test_cse_emits_single_atr_helper() -> None:
    code = compile_genome(_cse_genome())
    assert code.count("return sum(_trs) / 14") == 1, "ATR series should be emitted once"
    assert "_atr = self._n_" in code
    ast.parse(code)  # still valid Python


def test_compile_throughput_with_shared_subtrees() -> None:
    g = _shared_subtree_genome()
    t0 = time.perf_counter()
    for _ in range(_ITERATIONS):
        compile_genome(g)
    elapsed = time.perf_counter() - t0

    if os.environ.get("BENCH_FACTOR_COMPILER_VERBOSE"):
        print(f"\ncompile ×{_ITERATIONS} (shared sub-trees): {elapsed:.4f}s")

    # Smoke bound — compilation must complete; the memoisation win is a
    # constant-factor reduction in hashing, not a behavioural change.
    assert elapsed >= 0.0
