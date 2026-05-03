"""Unit tests for the chunked-bar protocol added in issue #377.

Covers the streaming-harness surface in isolation (subprocess round-trip,
no ``TradingService``):

* Capability negotiation: child advertises ``chunked_bars: true`` in the
  first ready after ``send_start``.
* Per-order ``bar_index`` tagging on chunked round-trips so the parent can
  pin each emitted order to its source bar.
* Default ``Strategy.on_bars`` correctly delegates to ``on_bar`` per bar
  with the right ``bar_index``.
* ``BarSafetyAssertion`` semantics preserved under chunked mode (per-bar
  ``submitted_at`` mapping in ``TradingService._run_chunked``).
* Per-bar protocol still works after chunked-bars handler is added.
"""

from __future__ import annotations

import textwrap

from investment_team.trading_service.strategy.streaming_harness import StreamingHarness

_PASSTHROUGH_CODE = textwrap.dedent('''\
    """Emits one MARKET order per bar so we can verify bar_index tagging."""
    from contract import OrderSide, OrderType, Strategy


    class TaggedEmitter(Strategy):
        def on_bar(self, ctx, bar):
            ctx.submit_order(
                symbol=bar.symbol,
                side=OrderSide.LONG,
                qty=1.0,
                order_type=OrderType.MARKET,
                reason=f"emit-{bar.timestamp}",
            )
''')


_VECTORIZED_OVERRIDE_CODE = textwrap.dedent('''\
    """Strategy that overrides on_bars — the chunked harness must reject
    this so a vectorised override can't peek at later bars and emit
    orders tagged to earlier bar indices (look-ahead bypass).
    """
    from contract import OrderSide, OrderType, Strategy


    class VectorisedOverride(Strategy):
        def on_bars(self, ctx, bars):
            ctx.submit_order(
                symbol=bars[-1].symbol,
                side=OrderSide.LONG,
                qty=1.0,
                order_type=OrderType.MARKET,
                reason="should-be-rejected",
            )
''')


_FORGED_BAR_INDEX_CODE = textwrap.dedent('''\
    """Strategy that *attempts* to forge bar_index by mutating
    context attributes and writing to the emit channel, then emits
    one order per bar. The chunked harness's tagging emit must
    overwrite any forged bar_index with the harness-managed one,
    so the parent always sees correct, monotonic bar_index values
    matching the bar that was actually being processed.
    """
    from contract import OrderSide, OrderType, Strategy


    class Forger(Strategy):
        def on_bar(self, ctx, bar):
            # Try every plausible vector for forging bar_index.
            ctx._current_bar_index = 99  # attribute the old impl honored
            ctx.bar_index = -7  # arbitrary attribute
            ctx.submit_order(
                symbol=bar.symbol,
                side=OrderSide.LONG,
                qty=1.0,
                order_type=OrderType.MARKET,
                reason=f"bar-{bar.timestamp}",
            )
''')


_NOOP_CODE = textwrap.dedent('''\
    """No-op — used to smoke-test capability negotiation without orders."""
    from contract import Strategy


    class Noop(Strategy):
        def on_bar(self, ctx, bar):
            pass
''')


def _bar(ts: str, symbol: str = "AAA") -> dict:
    return {
        "symbol": symbol,
        "timestamp": ts,
        "timeframe": "1d",
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 1000.0,
    }


def _state() -> dict:
    return {"capital": 100_000.0, "equity": 100_000.0, "positions": []}


# ---------------------------------------------------------------------------
# Capability negotiation
# ---------------------------------------------------------------------------


def test_first_ready_advertises_chunked_bars_capability() -> None:
    """The child must advertise ``chunked_bars: true`` so the parent can
    decide whether ``send_bars`` is safe to call. Older child builds
    that don't advertise → parent must fall back to per-bar; the trading
    service does this via ``StreamingHarness.supports_chunked_bars``.
    """
    with StreamingHarness(_NOOP_CODE) as harness:
        resp = harness.send_start(config={"initial_capital": 100_000.0})
        assert resp.capabilities.get("chunked_bars") is True
        assert harness.supports_chunked_bars is True
        harness.send_end()


# ---------------------------------------------------------------------------
# bar_index round-trip
# ---------------------------------------------------------------------------


def test_send_bars_tags_each_order_with_source_bar_index() -> None:
    """Each emitted order under chunked mode must carry the
    ``bar_index`` of the bar that generated it. The parent uses this to
    pin per-order ``submitted_at`` (preserves bar safety).
    """
    with StreamingHarness(_PASSTHROUGH_CODE) as harness:
        harness.send_start(config={"initial_capital": 100_000.0})
        bars = [
            {"bar": _bar(f"2024-01-{i + 1:02d}"), "state": _state(), "is_warmup": False}
            for i in range(4)
        ]
        resp = harness.send_bars(bars=bars)
        # One order per bar, in order.
        assert len(resp.orders) == 4
        assert resp.order_bar_indices == [0, 1, 2, 3]
        # Reasons threaded through the payload prove the right bar drove
        # the right index.
        for i, o in enumerate(resp.orders):
            assert o["reason"] == f"emit-2024-01-{i + 1:02d}"
        harness.send_end()


def test_send_bars_with_empty_chunk_returns_empty_response() -> None:
    """Edge case: the parent may call send_bars with [] at end-of-stream
    when the buffer is empty; child must not block waiting for content.
    """
    with StreamingHarness(_PASSTHROUGH_CODE) as harness:
        harness.send_start(config={"initial_capital": 100_000.0})
        resp = harness.send_bars(bars=[])
        assert resp.orders == []
        assert resp.order_bar_indices == []
        harness.send_end()


# ---------------------------------------------------------------------------
# on_bars override path
# ---------------------------------------------------------------------------


def test_on_bars_override_is_rejected_under_chunked_protocol() -> None:
    """Overriding ``Strategy.on_bars`` is unsafe under the chunked
    protocol because the override receives the whole chunk before the
    parent replays bars one-by-one — letting a strategy peek at later
    bars and emit orders tagged to earlier ``bar_index`` values, which
    the parent trusts for ``submitted_at``. The harness must reject
    such overrides outright with a ``contract_error`` (PR #425 review).
    """
    import pytest

    from investment_team.trading_service.strategy.streaming_harness import (
        StrategyRuntimeError,
    )

    with StreamingHarness(_VECTORIZED_OVERRIDE_CODE) as harness:
        harness.send_start(config={"initial_capital": 100_000.0})
        bars = [
            {"bar": _bar(f"2024-01-{i + 1:02d}"), "state": _state(), "is_warmup": False}
            for i in range(3)
        ]
        with pytest.raises(StrategyRuntimeError) as excinfo:
            harness.send_bars(bars=bars)
        assert excinfo.value.etype == "contract_error"
        assert "BAR_CHUNK_SIZE=1" in str(excinfo.value)


def test_chunked_bar_index_cannot_be_forged_by_strategy() -> None:
    """Defense for PR #425's second-round review: a strategy that
    mutates ``ctx._current_bar_index`` (or any other context attribute)
    must NOT be able to forge bar_index. The chunked harness wraps
    ``_emit`` with a tagging closure that captures harness-private
    state; emitted ``order``/``cancel`` records always carry the
    bar_index of the bar the harness was actually dispatching.

    Without this defense, a strategy could see bar 5's data inside
    ``on_bar`` and then emit an order tagged for bar 0, backdating the
    decision and bypassing look-ahead safety even with the in-range
    validation in ``_run_chunked``.
    """
    with StreamingHarness(_FORGED_BAR_INDEX_CODE) as harness:
        harness.send_start(config={"initial_capital": 100_000.0})
        bars = [
            {"bar": _bar(f"2024-01-{i + 1:02d}"), "state": _state(), "is_warmup": False}
            for i in range(4)
        ]
        resp = harness.send_bars(bars=bars)
        # Despite the strategy hand-setting ``_current_bar_index = 99``
        # before each emission, every order is tagged with the correct
        # harness-managed bar_index in monotonically increasing order.
        assert len(resp.orders) == 4
        assert resp.order_bar_indices == [0, 1, 2, 3]
        for i, o in enumerate(resp.orders):
            assert o["reason"] == f"bar-2024-01-{i + 1:02d}"
        harness.send_end()


# ---------------------------------------------------------------------------
# Per-bar protocol still works after chunked-bars handler exists
# ---------------------------------------------------------------------------


def test_send_bar_per_bar_path_unchanged() -> None:
    """The legacy per-bar protocol (``send_bar``) must keep working
    untouched — orders carry no ``bar_index`` (parent uses prev_bar's
    timestamp directly).
    """
    with StreamingHarness(_PASSTHROUGH_CODE) as harness:
        harness.send_start(config={"initial_capital": 100_000.0})
        resp = harness.send_bar(
            bar=_bar("2024-01-02"),
            state=_state(),
            is_warmup=False,
        )
        assert len(resp.orders) == 1
        # No bar_index in per-bar mode.
        assert resp.order_bar_indices == [None]
        harness.send_end()


# ---------------------------------------------------------------------------
# Service-level integration: chunked path preserves per-order timestamps
# (i.e. BarSafetyAssertion semantics under issue #377).
# ---------------------------------------------------------------------------


def test_service_chunked_path_runs_round_trip_strategy_without_lookahead() -> None:
    """End-to-end: under chunked mode (BAR_CHUNK_SIZE=4), the service must
    replay each bar's pre-/post-bar logic in order, route orders to
    their source bar's timestamp via ``bar_index``, and complete
    without firing ``BarSafetyAssertion``.

    Uses the existing ROUND_TRIP_CODE strategy (issues an entry every
    HOLD bars and exits HOLD bars later) so we get real closed trades
    on top of the no-lookahead invariant. The strategy is stateful
    (checks ``ctx.position()``) but its decisions live within a single
    chunk only when chunk_size > 2*HOLD; here we keep chunk_size=4 and
    HOLD=3 so the round-trip straddles a chunk boundary, exercising
    pending_for_prev carry-over between chunks.
    """
    import os

    from investment_team.market_data_service import OHLCVBar
    from investment_team.models import BacktestConfig, StrategySpec
    from investment_team.trading_service.modes.backtest import run_backtest

    from .golden.strategies import ROUND_TRIP_CODE

    bars = [
        OHLCVBar(
            date=f"2024-01-{i + 1:02d}",
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=10_000.0,
        )
        for i in range(20)
    ]
    spec = StrategySpec(
        strategy_id="chunked-roundtrip",
        authored_by="377-test",
        asset_class="stocks",
        hypothesis="invariant",
        signal_definition="invariant",
        strategy_code=ROUND_TRIP_CODE,
    )
    config = BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-12-31",
        initial_capital=100_000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
    )

    prev = os.environ.get("BAR_CHUNK_SIZE")
    os.environ["BAR_CHUNK_SIZE"] = "4"
    try:
        result = run_backtest(strategy=spec, config=config, market_data={"AAA": bars})
    finally:
        if prev is None:
            os.environ.pop("BAR_CHUNK_SIZE", None)
        else:
            os.environ["BAR_CHUNK_SIZE"] = prev

    # Defining invariants: no look-ahead, no error, all bars processed.
    assert not result.service_result.lookahead_violation, result.service_result.error
    assert result.service_result.error is None
    assert result.service_result.bars_processed == 20
    # The strategy emitted at least one order and got accepted via the
    # chunked path's pending_for_prev replay.
    diag = result.service_result.execution_diagnostics
    assert diag.orders_emitted > 0
    assert diag.orders_accepted > 0


# ---------------------------------------------------------------------------
# Self-review E1: TradingService.__init__ validates bar_chunk_size
# ---------------------------------------------------------------------------


def test_trading_service_rejects_invalid_bar_chunk_size() -> None:
    """``bar_chunk_size`` must be ``None`` or a positive int. Without
    validation, ``bar_chunk_size=0`` would silently fall back to per-bar
    mode (because ``chunk_size > 1`` is False) and ``bar_chunk_size=-5``
    would do the same — both hide caller bugs.
    """
    import pytest

    from investment_team.models import BacktestConfig
    from investment_team.trading_service.service import TradingService

    config = BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-12-31",
        initial_capital=100_000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
    )

    with pytest.raises(ValueError, match=r"bar_chunk_size must be >= 1"):
        TradingService(strategy_code="x", config=config, bar_chunk_size=0)
    with pytest.raises(ValueError, match=r"bar_chunk_size must be >= 1"):
        TradingService(strategy_code="x", config=config, bar_chunk_size=-3)
    with pytest.raises(TypeError, match=r"bar_chunk_size must be"):
        TradingService(strategy_code="x", config=config, bar_chunk_size=True)
    with pytest.raises(TypeError, match=r"bar_chunk_size must be"):
        TradingService(strategy_code="x", config=config, bar_chunk_size="256")  # type: ignore[arg-type]
    # Valid values must not raise.
    TradingService(strategy_code="x", config=config, bar_chunk_size=None)
    TradingService(strategy_code="x", config=config, bar_chunk_size=1)
    TradingService(strategy_code="x", config=config, bar_chunk_size=256)
