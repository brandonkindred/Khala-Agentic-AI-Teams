"""Unit tests for the coverage-probe harness wiring added in issue #450.

Covers the streaming-harness surface in isolation (subprocess round-trip,
no ``TradingService``):

* Off path: ``coverage_probe_mode=False`` leaves ``harness.probe_events``
  ``None`` and never emits a ``probe_event`` frame — the existing JSONL
  protocol is unchanged.
* On path: a strategy instrumented by ``instrument_strategy_code`` (#449)
  and run with ``coverage_probe_mode=True`` produces aggregated per-rule
  hit counts plus first/last true bar indices.
* Cap behaviour: when more distinct ``rule_id``s than the env-configured
  cap fire, the flushed payload sets ``truncated: True``.
"""

from __future__ import annotations

import textwrap

import pytest

from investment_team.strategy_lab.coverage_probe.runtime_instrument import (
    instrument_strategy_code,
)
from investment_team.trading_service.strategy.streaming_harness import (
    StreamingHarness,
)

# ---------------------------------------------------------------------------
# Fixtures: tiny strategies whose on_bar contains predicate logic the
# instrumenter can wrap. Each emits one MARKET order when the predicate
# fires so we can also assert behaviour-preservation off/on.
# ---------------------------------------------------------------------------


_TWO_RULE_CODE = textwrap.dedent('''\
    """Two-rule strategy: an entry guarded by ``close > 100`` and an exit
    guarded by ``close < 50``. Used to exercise per-rule aggregation and
    first/last_true_bar tracking under both probe-off and probe-on runs.
    """
    from contract import OrderSide, OrderType, Strategy


    class TwoRule(Strategy):
        def on_bar(self, ctx, bar):
            if bar.close > 100:
                ctx.submit_order(
                    symbol=bar.symbol,
                    side=OrderSide.LONG,
                    qty=1.0,
                    order_type=OrderType.MARKET,
                    reason="entry",
                )
            if bar.close < 50:
                ctx.submit_order(
                    symbol=bar.symbol,
                    side=OrderSide.SHORT,
                    qty=1.0,
                    order_type=OrderType.MARKET,
                    reason="exit",
                )
''')


_AND_RULE_CODE = textwrap.dedent('''\
    """Conjunction: both legs (volume > 1000 AND close > 100) must fire
    for the order to emit, exercising the per-leg unwrap of BoolOp in
    the instrumenter and bar-wise aggregation in the collector.
    """
    from contract import OrderSide, OrderType, Strategy


    class AndRule(Strategy):
        def on_bar(self, ctx, bar):
            if bar.volume > 1000 and bar.close > 100:
                ctx.submit_order(
                    symbol=bar.symbol,
                    side=OrderSide.LONG,
                    qty=1.0,
                    order_type=OrderType.MARKET,
                    reason="and",
                )
''')


def _bar(ts: str, *, close: float = 100.0, volume: float = 1000.0, symbol: str = "AAA") -> dict:
    return {
        "symbol": symbol,
        "timestamp": ts,
        "timeframe": "1d",
        "open": close,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "volume": volume,
    }


def _state() -> dict:
    return {"capital": 100_000.0, "equity": 100_000.0, "positions": []}


def _run_bars(harness: StreamingHarness, closes: list[float], volumes: list[float] | None = None):
    """Drive a list of bars through ``harness`` per-bar, return aggregated orders."""
    if volumes is None:
        volumes = [1000.0] * len(closes)
    assert len(closes) == len(volumes)
    orders: list[dict] = []
    for i, (close, volume) in enumerate(zip(closes, volumes), start=1):
        resp = harness.send_bar(
            bar=_bar(f"2024-01-{i:02d}", close=close, volume=volume),
            state=_state(),
        )
        orders.extend(resp.orders)
    return orders


# ---------------------------------------------------------------------------
# Off path
# ---------------------------------------------------------------------------


def test_probe_mode_off_leaves_probe_events_none() -> None:
    """``coverage_probe_mode=False`` is the zero-overhead default — no
    ``probe_event`` frame flushed, ``harness.probe_events`` stays
    ``None`` end-to-end."""
    instrumented, rule_index = instrument_strategy_code(_TWO_RULE_CODE)
    assert len(rule_index.rules) == 2  # sanity: instrumenter wrapped both legs

    with StreamingHarness(instrumented) as harness:
        harness.send_start(config={"initial_capital": 100_000.0})
        _run_bars(harness, closes=[150.0, 25.0, 75.0])
        harness.send_end()
        assert harness.probe_events is None


def test_probe_mode_off_preserves_orders_byte_identical_to_uninstrumented() -> None:
    """Probe mode off, instrumented vs uninstrumented strategy: identical
    emitted orders. Proves the default no-op ``__probe_record__`` from
    the prelude is true-identity and doesn't drift the strategy state."""
    closes = [150.0, 25.0, 75.0, 200.0]

    with StreamingHarness(_TWO_RULE_CODE) as harness:
        harness.send_start(config={"initial_capital": 100_000.0})
        baseline = _run_bars(harness, closes=closes)
        harness.send_end()

    instrumented, _ = instrument_strategy_code(_TWO_RULE_CODE)
    with StreamingHarness(instrumented) as harness:
        harness.send_start(config={"initial_capital": 100_000.0})
        rewritten = _run_bars(harness, closes=closes)
        harness.send_end()

    assert baseline == rewritten


# ---------------------------------------------------------------------------
# On path
# ---------------------------------------------------------------------------


def test_probe_mode_on_populates_per_rule_hit_counts() -> None:
    """Probe mode on: ``harness.probe_events`` is a dict with one entry
    per wrapped subcondition, carrying hit_count plus first/last
    true bar indices keyed by the AST counter from #449."""
    instrumented, rule_index = instrument_strategy_code(_TWO_RULE_CODE)
    rule_ids = list(rule_index.rules)
    assert rule_ids == ["r0", "r1"]

    closes = [150.0, 25.0, 30.0, 75.0, 200.0]  # r0 fires on bars 0, 4; r1 fires on bars 1, 2
    with StreamingHarness(instrumented, coverage_probe_mode=True) as harness:
        harness.send_start(config={"initial_capital": 100_000.0})
        _run_bars(harness, closes=closes)
        harness.send_end()
        payload = harness.probe_events

    assert payload is not None
    assert payload["truncated"] is False
    events = {ev["rule_id"]: ev for ev in payload["events"]}
    # r0 == close > 100 fires on bars 0 (close=150) and 4 (close=200).
    assert events["r0"]["hit_count"] == 2
    assert events["r0"]["first_true_bar"] == 0
    assert events["r0"]["last_true_bar"] == 4
    # r1 == close < 50 fires on bars 1 (close=25) and 2 (close=30).
    assert events["r1"]["hit_count"] == 2
    assert events["r1"]["first_true_bar"] == 1
    assert events["r1"]["last_true_bar"] == 2


def test_probe_mode_on_preserves_orders_vs_off() -> None:
    """Same instrumented strategy, same bars: trades / orders are
    byte-identical with probe mode on vs off — the collector must be
    an identity function on the AST-rewritten leg value."""
    instrumented, _ = instrument_strategy_code(_TWO_RULE_CODE)
    closes = [150.0, 25.0, 75.0, 200.0]

    with StreamingHarness(instrumented) as harness:
        harness.send_start(config={"initial_capital": 100_000.0})
        off_orders = _run_bars(harness, closes=closes)
        harness.send_end()

    with StreamingHarness(instrumented, coverage_probe_mode=True) as harness:
        harness.send_start(config={"initial_capital": 100_000.0})
        on_orders = _run_bars(harness, closes=closes)
        harness.send_end()

    assert off_orders == on_orders


def test_probe_mode_on_handles_conjunction_per_leg() -> None:
    """``BoolOp`` predicates are split per-leg by the instrumenter, so
    each leg has its own rule_id. Short-circuit semantics are preserved
    (Python's ``and`` skips the right leg when the left is falsy), which
    is exactly what makes the per-leg signal useful: the harness sees
    "r0 fired but r1 never got the chance to" without re-evaluating.
    This matches the intent of #406 — diagnosing which leg blocks
    entry without re-running the strategy."""
    instrumented, rule_index = instrument_strategy_code(_AND_RULE_CODE)
    assert len(rule_index.rules) == 2

    # Bar 0: volume=2000 (r0 truthy), close=50 (r1 evaluated, falsy) → no order.
    # Bar 1: volume=500 (r0 falsy), r1 short-circuited (never evaluated).
    # Bar 2: volume=3000 (r0 truthy), close=200 (r1 truthy) → order emitted.
    closes = [50.0, 150.0, 200.0]
    volumes = [2000.0, 500.0, 3000.0]

    with StreamingHarness(instrumented, coverage_probe_mode=True) as harness:
        harness.send_start(config={"initial_capital": 100_000.0})
        orders = _run_bars(harness, closes=closes, volumes=volumes)
        harness.send_end()
        payload = harness.probe_events

    assert len(orders) == 1  # only bar 2 satisfied both legs
    assert payload is not None
    events = {ev["rule_id"]: ev for ev in payload["events"]}
    # r0 == volume > 1000 was evaluated on every bar; truthy on 0 and 2.
    assert events["r0"]["hit_count"] == 2
    assert events["r0"]["first_true_bar"] == 0
    assert events["r0"]["last_true_bar"] == 2
    # r1 == close > 100 only evaluates when r0 is truthy (short-circuit),
    # so it fires once on bar 2 only.
    assert events["r1"]["hit_count"] == 1
    assert events["r1"]["first_true_bar"] == 2
    assert events["r1"]["last_true_bar"] == 2


def test_probe_mode_on_skips_unwrapped_strategy() -> None:
    """An uninstrumented strategy under probe mode still runs cleanly:
    the harness installs the collector but the strategy code never
    calls ``__probe_record__``, so ``events`` ends up empty (and the
    flushed payload is still well-formed). Defends against the
    common bug of forgetting to instrument before opting into probe
    mode."""
    with StreamingHarness(_TWO_RULE_CODE, coverage_probe_mode=True) as harness:
        harness.send_start(config={"initial_capital": 100_000.0})
        _run_bars(harness, closes=[150.0, 25.0])
        harness.send_end()
        payload = harness.probe_events

    assert payload == {"events": [], "truncated": False}


# ---------------------------------------------------------------------------
# Cap behaviour
# ---------------------------------------------------------------------------


def _build_many_rule_strategy(n: int) -> str:
    """Generate a strategy whose on_bar has N independent if-predicates
    so the instrumenter produces N distinct rule_ids — used to drive
    the cap-truncation path without depending on the default cap of
    5000.
    """
    lines = [
        "from contract import OrderSide, OrderType, Strategy",
        "",
        "",
        "class ManyRule(Strategy):",
        "    def on_bar(self, ctx, bar):",
    ]
    for i in range(n):
        # Each rule is "close > <unique_int>": deterministic, indicator-
        # free, and reliably true for a high enough close to populate
        # every leg in a single bar — fastest path to N distinct rule_ids.
        lines.append(f"        if bar.close > {i}:")
        lines.append("            pass")
    return "\n".join(lines) + "\n"


def test_probe_mode_truncated_flag_when_distinct_rule_ids_exceed_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the env-configured rule-id cap, new ``rule_id``s are dropped
    rather than tracked, and the flushed payload sets
    ``truncated: True``. We dial the cap down so the test stays cheap;
    the production default (5000) is exercised via the env var."""
    # Cap the child to 3 distinct rule_ids; the strategy has 5 if-predicates.
    monkeypatch.setenv("STRATLAB_COVERAGE_PROBE_CAP_OVERRIDE_HINT", "3")
    # The harness reads STRATLAB_COVERAGE_PROBE_CAP from the parent env,
    # so we set it directly. (The above is just for human-reading.)
    monkeypatch.setattr(
        "investment_team.trading_service.strategy.streaming_harness.COVERAGE_PROBE_RULE_CAP",
        3,
    )

    code = _build_many_rule_strategy(5)
    instrumented, rule_index = instrument_strategy_code(code)
    assert len(rule_index.rules) == 5

    with StreamingHarness(instrumented, coverage_probe_mode=True) as harness:
        harness.send_start(config={"initial_capital": 100_000.0})
        _run_bars(harness, closes=[1000.0])  # close > i is true for every i
        harness.send_end()
        payload = harness.probe_events

    assert payload is not None
    assert payload["truncated"] is True
    # Only the first 3 distinct rule_ids should have been tracked.
    assert len(payload["events"]) == 3
