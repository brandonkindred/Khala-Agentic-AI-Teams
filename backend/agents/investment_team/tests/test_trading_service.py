"""End-to-end tests for the new streaming Trading Service.

Covers:
* A minimal SMA-crossover strategy produces at least one round-trip trade
  against deterministic synthetic bars.
* A strategy that tries to read future data from a non-existent attribute
  aborts the run with ``lookahead_violation`` rather than silently skipping.
* ``modes.backtest.run_backtest`` raises ``ValueError`` when the strategy
  has no ``strategy_code`` (the LLM-per-bar fallback is intentionally gone).
"""

from __future__ import annotations

import textwrap
from typing import Dict, List

import pytest

from investment_team.market_data_service import OHLCVBar
from investment_team.models import (
    BacktestConfig,
    BacktestExecutionDiagnostics,
    StrategySpec,
)
from investment_team.trading_service.data_stream.historical_replay import (
    HistoricalReplayStream,
)
from investment_team.trading_service.data_stream.protocol import BarEvent, EndOfStreamEvent
from investment_team.trading_service.engine.order_book import OrderBook
from investment_team.trading_service.modes.backtest import run_backtest
from investment_team.trading_service.service import (
    _MAX_ORDER_EVENTS,
    TradingService,
    _increment_rejection,
    _record_event,
)
from investment_team.trading_service.strategy.contract import (
    Bar,
    OrderRequest,
    UnfilledPolicy,
)


def _uptrend_then_down_bars(symbol_bars: Dict[str, List[OHLCVBar]]) -> None:
    """Populate ``symbol_bars`` with a clean up-then-down pattern.

    The shape is deterministic so a simple SMA(5) crossover produces exactly
    one long round-trip trade: uptrend (bars 0-14) triggers the entry, the
    subsequent downturn (bars 15-29) triggers the exit.
    """
    bars: List[OHLCVBar] = []
    # 30 calendar days starting 2024-01-01 (spans a month boundary, fine).
    base = 100.0
    for i in range(15):
        price = base + i * 2.0  # steady +2 per bar
        bars.append(_mkbar(i + 1, price))
    for i in range(15):
        price = (base + 28.0) - (i + 1) * 2.5  # accelerating decline
        bars.append(_mkbar(16 + i, price))
    symbol_bars["AAA"] = bars


def _mkbar(day_of_month: int, close: float) -> OHLCVBar:
    month = 1 if day_of_month <= 31 else 2
    day = day_of_month if month == 1 else day_of_month - 31
    return OHLCVBar(
        date=f"2024-{month:02d}-{day:02d}",
        open=close - 0.2,
        high=close + 0.5,
        low=close - 0.5,
        close=close,
        volume=1_000_000,
    )


_SMA_STRATEGY_CODE = textwrap.dedent('''\
    """Tiny SMA(5) crossover — deterministic, no randomness, no LLM.

    Enters long when the current close crosses above SMA(5) and no position
    is open; exits when the current close crosses below SMA(5).
    """
    from contract import OrderSide, OrderType, Strategy


    class SmaCrossover(Strategy):
        WINDOW = 5

        def on_bar(self, ctx, bar):
            history = ctx.history(bar.symbol, self.WINDOW)
            if len(history) < self.WINDOW:
                return
            sma = sum(b.close for b in history) / self.WINDOW
            pos = ctx.position(bar.symbol)
            if pos is None and bar.close > sma:
                ctx.submit_order(
                    symbol=bar.symbol,
                    side=OrderSide.LONG,
                    qty=10,
                    order_type=OrderType.MARKET,
                    reason="sma_cross_up",
                )
            elif pos is not None and bar.close < sma:
                ctx.submit_order(
                    symbol=bar.symbol,
                    side=OrderSide.SHORT,  # opposite side closes the long
                    qty=pos.qty,
                    order_type=OrderType.MARKET,
                    reason="sma_cross_down",
                )
''')


_LOOKAHEAD_STRATEGY_CODE = textwrap.dedent('''\
    """Red-team strategy that tries to peek at future data."""
    from contract import Strategy


    class Peeker(Strategy):
        def on_bar(self, ctx, bar):
            # Attempting to access a non-existent "future" attribute must
            # surface as a classified lookahead_violation — not be silently
            # ignored. ``Bar`` has no such field.
            _ = bar.next_close  # noqa: F841 — intentional AttributeError
''')


_NOOP_STRATEGY_CODE = textwrap.dedent('''\
    """Strategy that intentionally emits no orders."""
    from contract import Strategy


    class NoopStrategy(Strategy):
        def on_bar(self, ctx, bar):
            return
''')


_WARMUP_ORDER_STRATEGY_CODE = textwrap.dedent('''\
    """Strategy that submits an order even during warm-up."""
    from contract import OrderSide, OrderType, Strategy


    class WarmupOrderStrategy(Strategy):
        def on_bar(self, ctx, bar):
            ctx.submit_order(
                symbol=bar.symbol,
                side=OrderSide.LONG,
                qty=1,
                order_type=OrderType.MARKET,
                reason="warmup_order",
            )
''')


_BROKEN_START_STRATEGY_CODE = textwrap.dedent('''\
    """Strategy that fails before any bars are processed."""
    from contract import Strategy


    class BrokenStartStrategy(Strategy):
        def on_start(self, ctx):
            raise RuntimeError("boom on start")

        def on_bar(self, ctx, bar):
            return
''')


_BROKEN_BAR_STRATEGY_CODE = textwrap.dedent('''\
    """Strategy that fails while processing a normal bar."""
    from contract import Strategy


    class BrokenBarStrategy(Strategy):
        def on_bar(self, ctx, bar):
            raise RuntimeError("boom on bar")
''')


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-02-15",
        initial_capital=100_000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
        metrics_engine="legacy",
    )


def test_trading_service_runs_sma_strategy_and_produces_trade() -> None:
    """Event-driven Strategy subclass → at least one round-trip trade."""
    market_data: Dict[str, List[OHLCVBar]] = {}
    _uptrend_then_down_bars(market_data)

    strategy = StrategySpec(
        strategy_id="strat-sma-1",
        authored_by="tests",
        asset_class="equity",
        hypothesis="momentum via SMA(5)",
        signal_definition="close vs sma(5)",
        entry_rules=["close > sma(5)"],
        exit_rules=["close < sma(5)"],
        strategy_code=_SMA_STRATEGY_CODE,
    )

    run = run_backtest(
        strategy=strategy,
        config=_config(),
        market_data=market_data,
    )

    assert run.service_result.error is None, run.service_result.error
    assert not run.service_result.lookahead_violation
    assert len(run.trades) >= 1
    trade = run.trades[0]
    assert trade.symbol == "AAA"
    assert trade.side == "long"
    # Entry occurred after the SMA warmup window.
    assert trade.entry_date >= "2024-01-06"
    # Exit happened during the downtrend phase (bars after day 15).
    assert trade.exit_date > trade.entry_date
    diagnostics = run.service_result.execution_diagnostics
    assert diagnostics.zero_trade_category is None
    assert diagnostics.closed_trades == len(run.trades)
    assert diagnostics.bars_processed == run.service_result.bars_processed
    assert diagnostics.warmup_orders_dropped == run.service_result.warmup_orders_dropped
    assert diagnostics.summary


def test_trading_service_surfaces_lookahead_violation() -> None:
    """A strategy touching a non-existent forward field aborts the run cleanly."""
    market_data: Dict[str, List[OHLCVBar]] = {}
    _uptrend_then_down_bars(market_data)

    strategy = StrategySpec(
        strategy_id="strat-peeker-1",
        authored_by="tests",
        asset_class="equity",
        hypothesis="peek at future bars (should fail)",
        signal_definition="future bar access",
        entry_rules=[],
        exit_rules=[],
        strategy_code=_LOOKAHEAD_STRATEGY_CODE,
    )

    run = run_backtest(
        strategy=strategy,
        config=_config(),
        market_data=market_data,
    )

    assert run.service_result.error is not None
    assert run.service_result.lookahead_violation is True
    assert not run.trades
    diagnostics = run.service_result.execution_diagnostics
    assert diagnostics.zero_trade_category == "UNKNOWN_ZERO_TRADE_PATH"
    assert diagnostics.closed_trades == 0
    assert diagnostics.summary


def test_zero_trade_result_gets_no_orders_emitted_category() -> None:
    """A no-op strategy lands in NO_ORDERS_EMITTED with zeroed counters (#409)."""
    market_data: Dict[str, List[OHLCVBar]] = {}
    _uptrend_then_down_bars(market_data)

    strategy = StrategySpec(
        strategy_id="strat-noop-409",
        authored_by="tests",
        asset_class="equity",
        hypothesis="no-op",
        signal_definition="none",
        entry_rules=[],
        exit_rules=[],
        strategy_code=_NOOP_STRATEGY_CODE,
    )

    run = run_backtest(strategy=strategy, config=_config(), market_data=market_data)

    assert run.service_result.error is None, run.service_result.error
    assert not run.trades
    diagnostics = run.service_result.execution_diagnostics
    assert diagnostics.zero_trade_category == "NO_ORDERS_EMITTED"
    assert diagnostics.closed_trades == 0
    assert diagnostics.orders_emitted == 0
    assert diagnostics.orders_accepted == 0
    assert diagnostics.orders_rejected == 0
    assert diagnostics.orders_unfilled == 0
    assert diagnostics.last_order_events == []
    assert diagnostics.bars_processed == run.service_result.bars_processed


def test_warmup_only_order_result_gets_warmup_diagnostics() -> None:
    """Warm-up order drops are mirrored into finalized diagnostics."""
    service = TradingService(strategy_code=_WARMUP_ORDER_STRATEGY_CODE, config=_config())
    stream = [
        BarEvent(
            bar=Bar(
                symbol="AAA",
                timestamp="2024-01-01",
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1_000_000,
            ),
            is_warmup=True,
        ),
        EndOfStreamEvent(),
    ]

    result = service.run(stream)

    assert result.error is None, result.error
    assert not result.trades
    assert result.bars_processed == 0
    assert result.warmup_orders_dropped == 1
    diagnostics = result.execution_diagnostics
    assert diagnostics.zero_trade_category == "ONLY_WARMUP_ORDERS"
    assert diagnostics.warmup_orders_dropped == result.warmup_orders_dropped
    assert diagnostics.bars_processed == result.bars_processed
    assert diagnostics.closed_trades == 0
    assert diagnostics.summary


def test_startup_error_return_path_includes_finalized_diagnostics() -> None:
    """A failure before the first bar still returns a finalized envelope."""
    service = TradingService(strategy_code=_BROKEN_START_STRATEGY_CODE, config=_config())

    result = service.run([EndOfStreamEvent()])

    assert result.error is not None
    assert not result.trades
    diagnostics = result.execution_diagnostics
    assert diagnostics.zero_trade_category == "UNKNOWN_ZERO_TRADE_PATH"
    assert diagnostics.bars_processed == 0
    assert diagnostics.closed_trades == 0
    assert diagnostics.summary


def test_runtime_error_return_path_includes_finalized_diagnostics() -> None:
    """A regular on_bar runtime failure still returns a finalized envelope."""
    market_data: Dict[str, List[OHLCVBar]] = {}
    _uptrend_then_down_bars(market_data)

    strategy = StrategySpec(
        strategy_id="strat-runtime-error-408",
        authored_by="tests",
        asset_class="equity",
        hypothesis="runtime error",
        signal_definition="raise",
        entry_rules=[],
        exit_rules=[],
        strategy_code=_BROKEN_BAR_STRATEGY_CODE,
    )

    run = run_backtest(strategy=strategy, config=_config(), market_data=market_data)

    assert run.service_result.error is not None
    assert run.service_result.lookahead_violation is False
    assert not run.trades
    diagnostics = run.service_result.execution_diagnostics
    assert diagnostics.zero_trade_category == "UNKNOWN_ZERO_TRADE_PATH"
    assert diagnostics.closed_trades == 0
    assert diagnostics.summary


def test_execution_diagnostic_helpers_cap_events_and_count_rejections() -> None:
    """#408 helpers are deterministic even before lifecycle instrumentation uses them."""
    diagnostics = BacktestExecutionDiagnostics()

    for idx in range(25):
        _record_event(diagnostics, "emitted", symbol=f"S{idx}", detail=str(idx))

    assert len(diagnostics.last_order_events) == 20
    assert diagnostics.last_order_events[0].symbol == "S5"
    assert diagnostics.last_order_events[-1].symbol == "S24"

    _increment_rejection(diagnostics, "malformed_request")
    _increment_rejection(diagnostics, "malformed_request")
    _increment_rejection(diagnostics, "")

    assert diagnostics.orders_rejected == 3
    assert diagnostics.orders_rejection_reasons == {
        "malformed_request": 2,
        "unknown": 1,
    }


# ---------------------------------------------------------------------------
# Issue #409 — TradingService order lifecycle diagnostics
# ---------------------------------------------------------------------------


_UNSUPPORTED_FEATURE_STRATEGY_CODE = textwrap.dedent('''\
    """Strategy that bypasses ``submit_order``'s subprocess-side validation
    to exercise the parent's ``UnsupportedOrderFeatureError`` rejection
    path. Sets ``parent_order_id``, which still raises in the parent's
    ``OrderRequest.validate_prices`` until #389 lands.
    """
    from contract import Strategy


    class UnsupportedFeatureStrategy(Strategy):
        _emitted = False

        def on_bar(self, ctx, bar):
            if self._emitted:
                return
            self._emitted = True
            ctx._next_client_order_id += 1
            cid = f"c{ctx._next_client_order_id}"
            ctx._emit({
                "kind": "order",
                "payload": {
                    "client_order_id": cid,
                    "symbol": bar.symbol,
                    "side": "long",
                    "qty": 1.0,
                    "order_type": "market",
                    "tif": "day",
                    "parent_order_id": "fake-parent",
                },
            })
''')


_MALFORMED_ORDER_STRATEGY_CODE = textwrap.dedent('''\
    """Strategy that bypasses ``submit_order`` and emits a payload missing
    required fields, exercising the parent's malformed-request rejection.
    """
    from contract import Strategy


    class MalformedOrderStrategy(Strategy):
        _emitted = False

        def on_bar(self, ctx, bar):
            if self._emitted:
                return
            self._emitted = True
            # Missing client_order_id, symbol, side, qty — Pydantic must reject.
            ctx._emit({"kind": "order", "payload": {"order_type": "market"}})
''')


_UNREACHABLE_LIMIT_STRATEGY_CODE = textwrap.dedent('''\
    """Strategy that submits a single DAY limit order with an unreachable
    price on the first bar so it survives the next bar's fill check and
    is expired on the date roll-over.
    """
    from contract import OrderSide, OrderType, Strategy


    class UnreachableLimitStrategy(Strategy):
        _emitted = False

        def on_bar(self, ctx, bar):
            if self._emitted:
                return
            self._emitted = True
            ctx.submit_order(
                symbol=bar.symbol,
                side=OrderSide.LONG,
                qty=1,
                order_type=OrderType.LIMIT,
                limit_price=0.01,  # far below market — never fills
                tif="day",
                reason="day_expiry_probe",
            )
''')


_LATE_MARKET_ORDER_STRATEGY_CODE = textwrap.dedent('''\
    """Strategy that submits a market order on every bar so the very
    last bar's order has no successor bar to fill against, exercising
    the end-of-stream unfilled path.
    """
    from contract import OrderSide, OrderType, Strategy


    class LateMarketStrategy(Strategy):
        def on_bar(self, ctx, bar):
            if ctx.position(bar.symbol) is not None:
                return
            ctx.submit_order(
                symbol=bar.symbol,
                side=OrderSide.LONG,
                qty=1,
                order_type=OrderType.MARKET,
                reason="late_emit",
            )
''')


_NOISY_ORDER_STRATEGY_CODE = textwrap.dedent('''\
    """Strategy that emits a fresh long order every bar (each bar gets a
    rejected order because there's already a position). Used to overflow
    the 20-event ``last_order_events`` cap.
    """
    from contract import OrderSide, OrderType, Strategy


    class NoisyOrderStrategy(Strategy):
        def on_bar(self, ctx, bar):
            ctx.submit_order(
                symbol=bar.symbol,
                side=OrderSide.LONG,
                qty=1,
                order_type=OrderType.MARKET,
                reason="noise",
            )
''')


def _two_day_bars() -> List[BarEvent]:
    """Two bars on consecutive trading dates, both well above any limit at $0.01.

    Used by the DAY-expiry test: bar 0 lets the strategy emit; bar 1 is on
    the same day so the limit order fails to fill; bar 2 is on day 2 and
    fires ``expire_day_orders``.
    """
    return [
        BarEvent(
            bar=Bar(
                symbol="AAA",
                timestamp="2024-01-02",
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1_000_000,
            ),
            is_warmup=False,
        ),
        BarEvent(
            bar=Bar(
                symbol="AAA",
                timestamp="2024-01-02T16:00:00",
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1_000_000,
            ),
            is_warmup=False,
        ),
        BarEvent(
            bar=Bar(
                symbol="AAA",
                timestamp="2024-01-03",
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1_000_000,
            ),
            is_warmup=False,
        ),
        EndOfStreamEvent(),
    ]


def test_diagnostics_emitted_and_accepted_counts_round_trip() -> None:
    """SMA round-trip: 2 emits, 2 accepts, 1 exit-emit, no rejections."""
    market_data: Dict[str, List[OHLCVBar]] = {}
    _uptrend_then_down_bars(market_data)

    strategy = StrategySpec(
        strategy_id="strat-409-sma",
        authored_by="tests",
        asset_class="equity",
        hypothesis="round-trip",
        signal_definition="sma",
        entry_rules=["close > sma(5)"],
        exit_rules=["close < sma(5)"],
        strategy_code=_SMA_STRATEGY_CODE,
    )

    run = run_backtest(strategy=strategy, config=_config(), market_data=market_data)
    diagnostics = run.service_result.execution_diagnostics

    assert run.service_result.error is None, run.service_result.error
    # SMA(5) crossover emits one long entry then one opposite-side close.
    assert diagnostics.orders_emitted == 2
    assert diagnostics.orders_accepted == 2
    assert diagnostics.orders_rejected == 0
    assert diagnostics.orders_unfilled == 0
    assert diagnostics.exits_emitted == 1
    # ``entries_filled`` is owned by #410 (FillSimulator) and must stay 0
    # at this layer until that issue lands.
    assert diagnostics.entries_filled == 0
    # Capped well under the limit for this fixture.
    assert len(diagnostics.last_order_events) <= _MAX_ORDER_EVENTS
    event_types = [e.event_type for e in diagnostics.last_order_events]
    assert "emitted" in event_types
    assert "accepted" in event_types


def test_diagnostics_unsupported_feature_records_rejection() -> None:
    """``parent_order_id`` is gated until #389; counts as unsupported_feature."""
    market_data: Dict[str, List[OHLCVBar]] = {}
    _uptrend_then_down_bars(market_data)

    strategy = StrategySpec(
        strategy_id="strat-409-unsupported",
        authored_by="tests",
        asset_class="equity",
        hypothesis="unsupported feature",
        signal_definition="parent_order_id",
        entry_rules=[],
        exit_rules=[],
        strategy_code=_UNSUPPORTED_FEATURE_STRATEGY_CODE,
    )

    run = run_backtest(strategy=strategy, config=_config(), market_data=market_data)
    diagnostics = run.service_result.execution_diagnostics

    # The unsupported feature aborts the run via StrategyRuntimeError, so
    # error is set and the unknown category preserves the abort signal.
    assert run.service_result.error is not None
    assert "unsupported" in run.service_result.error.lower()
    assert diagnostics.zero_trade_category == "UNKNOWN_ZERO_TRADE_PATH"
    # Counters captured before the abort still record the rejection.
    assert diagnostics.orders_emitted == 1
    assert diagnostics.orders_rejected == 1
    assert diagnostics.orders_rejection_reasons == {"unsupported_feature": 1}
    rejected_events = [e for e in diagnostics.last_order_events if e.event_type == "rejected"]
    assert len(rejected_events) == 1
    assert rejected_events[0].reason == "unsupported_feature"


def test_diagnostics_malformed_order_records_rejection_and_continues() -> None:
    """A bad payload is rejected as malformed_request without aborting the run."""
    market_data: Dict[str, List[OHLCVBar]] = {}
    _uptrend_then_down_bars(market_data)

    strategy = StrategySpec(
        strategy_id="strat-409-malformed",
        authored_by="tests",
        asset_class="equity",
        hypothesis="malformed payload",
        signal_definition="missing fields",
        entry_rules=[],
        exit_rules=[],
        strategy_code=_MALFORMED_ORDER_STRATEGY_CODE,
    )

    run = run_backtest(strategy=strategy, config=_config(), market_data=market_data)
    diagnostics = run.service_result.execution_diagnostics

    # Run completes despite the malformed order (matching pre-#409 behavior).
    assert run.service_result.error is None, run.service_result.error
    assert diagnostics.orders_emitted == 1
    assert diagnostics.orders_accepted == 0
    assert diagnostics.orders_rejected == 1
    assert diagnostics.orders_rejection_reasons == {"malformed_request": 1}
    assert diagnostics.zero_trade_category == "ORDERS_REJECTED"
    rejected_events = [e for e in diagnostics.last_order_events if e.event_type == "rejected"]
    assert len(rejected_events) == 1
    assert rejected_events[0].reason == "malformed_request"


def test_diagnostics_day_order_expiry_records_unfilled() -> None:
    """A DAY limit at an unreachable price expires on the next-day bar."""
    service = TradingService(strategy_code=_UNREACHABLE_LIMIT_STRATEGY_CODE, config=_config())

    result = service.run(_two_day_bars())
    diagnostics = result.execution_diagnostics

    assert result.error is None, result.error
    assert diagnostics.orders_emitted == 1
    assert diagnostics.orders_accepted == 1
    assert diagnostics.orders_unfilled >= 1
    unfilled_events = [e for e in diagnostics.last_order_events if e.event_type == "unfilled"]
    reasons = {e.reason for e in unfilled_events}
    assert "day_expired" in reasons
    assert diagnostics.zero_trade_category == "ORDERS_UNFILLED"


def test_diagnostics_end_of_stream_unfilled_recorded() -> None:
    """An order emitted on the last bar has no next bar; counts as unfilled."""
    market_data: Dict[str, List[OHLCVBar]] = {}
    # 5 bars is enough; LateMarketStrategy emits on every bar but only
    # the *first* survives unmatched to the end (subsequent emissions are
    # short-circuited once a position opens). With no future bar after
    # the last bar, the queued order falls into the end-of-stream sink.
    _uptrend_then_down_bars(market_data)
    market_data["AAA"] = market_data["AAA"][:1]

    strategy = StrategySpec(
        strategy_id="strat-409-eos",
        authored_by="tests",
        asset_class="equity",
        hypothesis="end-of-stream",
        signal_definition="late market",
        entry_rules=[],
        exit_rules=[],
        strategy_code=_LATE_MARKET_ORDER_STRATEGY_CODE,
    )

    run = run_backtest(strategy=strategy, config=_config(), market_data=market_data)
    diagnostics = run.service_result.execution_diagnostics

    assert run.service_result.error is None, run.service_result.error
    assert diagnostics.orders_emitted >= 1
    assert diagnostics.orders_unfilled >= 1
    unfilled_events = [e for e in diagnostics.last_order_events if e.event_type == "unfilled"]
    reasons = {e.reason for e in unfilled_events}
    assert "end_of_stream" in reasons


def test_diagnostics_warmup_dropped_emits_lifecycle_event() -> None:
    """Warm-up orders increment the counter *and* surface a lifecycle event."""
    service = TradingService(strategy_code=_WARMUP_ORDER_STRATEGY_CODE, config=_config())
    stream = [
        BarEvent(
            bar=Bar(
                symbol="AAA",
                timestamp="2024-01-01",
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1_000_000,
            ),
            is_warmup=True,
        ),
        EndOfStreamEvent(),
    ]

    result = service.run(stream)
    diagnostics = result.execution_diagnostics

    assert result.error is None, result.error
    assert diagnostics.warmup_orders_dropped == 1
    warmup_events = [e for e in diagnostics.last_order_events if e.event_type == "warmup_dropped"]
    assert len(warmup_events) == 1
    assert warmup_events[0].symbol == "AAA"
    # Warm-up emissions never reach the post-warmup ``orders_emitted`` counter.
    assert diagnostics.orders_emitted == 0


def test_diagnostics_last_order_events_capped_at_twenty() -> None:
    """A noisy strategy emits >20 orders; ``last_order_events`` stays at 20."""
    market_data: Dict[str, List[OHLCVBar]] = {}
    _uptrend_then_down_bars(market_data)

    strategy = StrategySpec(
        strategy_id="strat-409-noisy",
        authored_by="tests",
        asset_class="equity",
        hypothesis="noisy",
        signal_definition="emit every bar",
        entry_rules=[],
        exit_rules=[],
        strategy_code=_NOISY_ORDER_STRATEGY_CODE,
    )

    run = run_backtest(strategy=strategy, config=_config(), market_data=market_data)
    diagnostics = run.service_result.execution_diagnostics

    # 30 bars of fixture × at least one event per bar > 20.
    assert diagnostics.orders_emitted >= 25
    assert len(diagnostics.last_order_events) == _MAX_ORDER_EVENTS


def test_run_backtest_without_strategy_code_raises() -> None:
    """The LLM-per-bar fallback is removed; no strategy_code must fail fast."""
    strategy = StrategySpec(
        strategy_id="strat-no-code",
        authored_by="legacy",
        asset_class="equity",
        hypothesis="h",
        signal_definition="s",
        strategy_code=None,
    )
    with pytest.raises(ValueError, match="strategy_code is required"):
        run_backtest(strategy=strategy, config=_config(), market_data={})


# ---------------------------------------------------------------------------
# Issue #375 — preflight data integrity gate
# ---------------------------------------------------------------------------


def test_run_backtest_attaches_data_quality_report() -> None:
    """Happy path: clean market data → report present, severity == 'ok'."""
    market_data: Dict[str, List[OHLCVBar]] = {}
    _uptrend_then_down_bars(market_data)

    strategy = StrategySpec(
        strategy_id="strat-sma-dq-1",
        authored_by="tests",
        asset_class="equity",
        hypothesis="momentum via SMA(5)",
        signal_definition="close vs sma(5)",
        entry_rules=["close > sma(5)"],
        exit_rules=["close < sma(5)"],
        strategy_code=_SMA_STRATEGY_CODE,
    )

    run = run_backtest(strategy=strategy, config=_config(), market_data=market_data)
    assert run.result.data_quality_report is not None
    assert run.result.data_quality_report["severity"] == "ok"
    assert "AAA" in run.result.data_quality_report["per_symbol"]


def test_run_backtest_strict_fails_on_ohlc_violation() -> None:
    """A bar with high < open trips the gate before TradingService runs."""
    from investment_team.execution.data_quality import DataIntegrityError

    market_data: Dict[str, List[OHLCVBar]] = {}
    _uptrend_then_down_bars(market_data)
    bars = market_data["AAA"]
    # Corrupt one bar so high < max(open, close).
    bars[10] = bars[10].model_copy(update={"high": bars[10].open - 5.0})

    strategy = StrategySpec(
        strategy_id="strat-dq-fail",
        authored_by="tests",
        asset_class="equity",
        hypothesis="h",
        signal_definition="s",
        strategy_code=_SMA_STRATEGY_CODE,
    )

    with pytest.raises(DataIntegrityError) as excinfo:
        run_backtest(strategy=strategy, config=_config(), market_data=market_data)
    assert excinfo.value.report.severity == "fail"
    assert excinfo.value.report.per_symbol["AAA"].ohlc_violations == 1


# ---------------------------------------------------------------------------
# Issue #385 — default_unfilled_policy plumbing (gated feature flag)
# ---------------------------------------------------------------------------


def _capture_submitted_orders(monkeypatch) -> List[OrderRequest]:
    """Wrap ``OrderBook.submit`` to capture every request handed to the book.

    Used to assert what ``unfilled_policy`` value reaches the order book
    after the parent-side mutation in ``TradingService``.
    """
    captured: List[OrderRequest] = []
    real_submit = OrderBook.submit

    def capturing_submit(self, request, **kwargs):
        captured.append(request)
        return real_submit(self, request, **kwargs)

    monkeypatch.setattr(OrderBook, "submit", capturing_submit)
    return captured


@pytest.mark.parametrize(
    ("mode_default", "flag_on", "expected_policy"),
    [
        # Backtest mode default = REQUEUE_NEXT_BAR; flag off → unchanged (None).
        (UnfilledPolicy.REQUEUE_NEXT_BAR, False, None),
        # Backtest mode default = REQUEUE_NEXT_BAR; flag on → applied.
        (UnfilledPolicy.REQUEUE_NEXT_BAR, True, UnfilledPolicy.REQUEUE_NEXT_BAR),
        # Paper mode default = DROP; flag off → unchanged (None).
        (UnfilledPolicy.DROP, False, None),
        # Paper mode default = DROP; flag on → applied.
        (UnfilledPolicy.DROP, True, UnfilledPolicy.DROP),
    ],
    ids=[
        "backtest_flag_off",
        "backtest_flag_on",
        "paper_flag_off",
        "paper_flag_on",
    ],
)
def test_default_unfilled_policy_gated_by_flag(
    monkeypatch, mode_default, flag_on, expected_policy
) -> None:
    """Parent-side default applies only when the feature flag is on.

    When the flag is off, behavior matches today exactly: ``unfilled_policy``
    stays ``None`` on the request submitted to the order book regardless of
    what the mode passed to ``TradingService``. When the flag is on, requests
    that the strategy did not annotate get the mode default.
    """
    if flag_on:
        monkeypatch.setenv("TRADING_PARTIAL_FILL_DEFAULTS_ENABLED", "true")
    else:
        # Step 4 (#386) flipped the default to ``true`` once the engine
        # started consuming the policy. Pin to ``false`` explicitly so the
        # flag-off semantics stay testable without depending on the default.
        monkeypatch.setenv("TRADING_PARTIAL_FILL_DEFAULTS_ENABLED", "false")

    captured = _capture_submitted_orders(monkeypatch)

    market_data: Dict[str, List[OHLCVBar]] = {}
    _uptrend_then_down_bars(market_data)

    service = TradingService(
        strategy_code=_SMA_STRATEGY_CODE,
        config=_config(),
        default_unfilled_policy=mode_default,
    )
    stream = HistoricalReplayStream(market_data, timeframe="1d")
    result = service.run(stream)

    assert result.error is None, result.error
    assert captured, "expected the SMA strategy to submit at least one order"
    for req in captured:
        assert req.unfilled_policy == expected_policy, (
            f"flag_on={flag_on} mode_default={mode_default} "
            f"expected unfilled_policy={expected_policy} but saw {req.unfilled_policy}"
        )


def test_run_backtest_passes_requeue_default_to_service(monkeypatch) -> None:
    """``modes.backtest.run_backtest`` constructs the service with REQUEUE_NEXT_BAR."""
    monkeypatch.setenv("TRADING_PARTIAL_FILL_DEFAULTS_ENABLED", "true")
    captured = _capture_submitted_orders(monkeypatch)

    market_data: Dict[str, List[OHLCVBar]] = {}
    _uptrend_then_down_bars(market_data)

    strategy = StrategySpec(
        strategy_id="strat-sma-385-backtest",
        authored_by="tests",
        asset_class="equity",
        hypothesis="momentum via SMA(5)",
        signal_definition="close vs sma(5)",
        entry_rules=["close > sma(5)"],
        exit_rules=["close < sma(5)"],
        strategy_code=_SMA_STRATEGY_CODE,
    )

    run = run_backtest(strategy=strategy, config=_config(), market_data=market_data)

    assert run.service_result.error is None, run.service_result.error
    assert captured, "expected the SMA strategy to submit at least one order"
    for req in captured:
        assert req.unfilled_policy == UnfilledPolicy.REQUEUE_NEXT_BAR


def test_partial_fill_defaults_flag_default_is_on(monkeypatch) -> None:
    """With the env var unset, the helper reports ``True`` (Step 4 default)."""
    monkeypatch.delenv("TRADING_PARTIAL_FILL_DEFAULTS_ENABLED", raising=False)
    from investment_team.trading_service.service import _partial_fill_defaults_enabled

    assert _partial_fill_defaults_enabled() is True
