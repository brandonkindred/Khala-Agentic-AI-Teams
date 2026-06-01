"""Benchmark: shared AST parse across the gate phase.

Each synthesis round hands the same generated source to several gates
(code-safety, conformance, rule-probes …). Previously every gate ran its
own ``ast.parse(code)``; the gates now route through the memoised
``parse_strategy_source`` so a given source is parsed once across the whole
phase rather than once per gate.

This benchmark isolates the parse step itself: ``_GATES`` simulates the
number of gates that parse the same source in one round. The "cold" path
clears the cache before each parse (the pre-change per-gate behaviour); the
"warm" path parses once and serves the rest from cache. Isolating the parse
keeps the measurement honest and fast — the end-to-end gate phase also does
substantial AST walking, so parse cost is only one component of it.

The assertion is loose (warm meaningfully faster) to survive CI noise; set
``BENCH_GATE_PHASE_VERBOSE=1`` to print the measured ratio. Marked
``@pytest.mark.bench`` so the default suite skips it; opt in with
``pytest -m bench``.
"""

from __future__ import annotations

import ast
import os
import time

import pytest

from investment_team.strategy_lab.quality_gates.code_safety_ast import parse_strategy_source

pytestmark = pytest.mark.bench

_ITERATIONS = 2000
_GATES = 4  # gates that parse the same source in one synthesis round


def _strategy_source(n_branches: int = 40) -> str:
    """A representative generated Strategy class."""
    lines = [
        "from contract import Strategy",
        "",
        "class S(Strategy):",
        "    UNIVERSE = frozenset({'QQQ'})",
        "    def on_bar(self, ctx, bar):",
        "        if bar.symbol not in self.UNIVERSE:",
        "            return",
        "        bars = ctx.history(bar.symbol, 200)",
        "        if len(bars) < 200:",
        "            return",
    ]
    for i in range(n_branches):
        lines.append(f"        x{i} = sma(bars, {i + 2})")
        lines.append(f"        if x{i} > 0:")
        lines.append("            ctx.submit_order(symbol=bar.symbol, qty=1, side='LONG')")
    return "\n".join(lines) + "\n"


def test_shared_parse_beats_per_gate_parse() -> None:
    code = _strategy_source()

    # Cold: each of the _GATES gates parses the source fresh (pre-change).
    t0 = time.perf_counter()
    for _ in range(_ITERATIONS):
        for _ in range(_GATES):
            ast.parse(code)
    cold = time.perf_counter() - t0

    # Warm: the source is parsed once and the other _GATES-1 lookups hit the
    # shared cache.
    t0 = time.perf_counter()
    for _ in range(_ITERATIONS):
        parse_strategy_source.cache_clear()
        for _ in range(_GATES):
            parse_strategy_source(code)
    warm = time.perf_counter() - t0

    if os.environ.get("BENCH_GATE_PHASE_VERBOSE"):
        print(
            f"\ncold({_GATES} parses)={cold:.4f}s warm(1 parse + {_GATES - 1} hits)="
            f"{warm:.4f}s ratio={cold / warm:.2f}x"
        )

    # With _GATES gates the shared cache eliminates _GATES-1 parses, so the
    # warm path should be well under the cold path. Loose bound for CI noise.
    assert warm <= cold / 1.5, f"expected shared-parse speedup; cold={cold:.4f} warm={warm:.4f}"
