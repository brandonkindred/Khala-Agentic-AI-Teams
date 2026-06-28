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
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    FixedFractionSizing,
    IndicatorRef,
    Predicate,
    SignalExitRule,
    StopLossRule,
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
    )


def test_trading_service_runs_sma_strategy_and_produces_trade() -> None:
    """Event-driven Strategy subclass → at least one round-trip trade."""
    market_data: Dict[str, List[OHLCVBar]] = {}
    _uptrend_then_down_bars(market_data)

    strategy = StrategySpec(
        timeframe="1d",
        strategy_id="strat-sma-1",
        authored_by="tests",
        asset_class="equity",
        hypothesis="momentum via SMA(5)",
        signal_definition="close vs sma(5)",
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(
                    lhs="bar.close", op=">", rhs=IndicatorRef(name="sma", params={"period": 5})
                ),
            )
        ],
        exit_rules=[
            SignalExitRule(
                when=Predicate(
                    lhs="bar.close", op="<", rhs=IndicatorRef(name="sma", params={"period": 5})
                )
            )
        ],
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


def _has_full_loss_short_stop(exit_rules) -> bool:
    """Whether ``exit_rules`` carries the auto-injected 100% short-safety stop."""
    return any(
        isinstance(r, StopLossRule) and r.basis == "entry_price" and r.pct == 1.0
        for r in exit_rules
    )


def test_uncovered_short_entry_auto_injects_full_loss_stop() -> None:
    """A short entry with no effective stop gets a 100%-adverse-move stop injected
    so the short's modeled worst-case loss is bounded by the deployed size (a
    short can otherwise lose more than 100% of deployed capital)."""
    service = TradingService(
        strategy_code=_NOOP_STRATEGY_CODE,
        config=_config(),
        entry_rules=[EntryRule(side="short", when=Predicate(lhs="bar.close", op=">", rhs=0.0))],
        sizing=FixedFractionSizing(fraction=0.02),
        exit_rules=[],
    )
    assert _has_full_loss_short_stop(service._exit_rules)


def test_short_with_effective_stop_is_not_auto_injected() -> None:
    """A short that already declares a side-compatible stop is left untouched —
    no redundant 100% backstop is appended."""
    stop = StopLossRule(basis="entry_price", pct=0.05)
    service = TradingService(
        strategy_code=_NOOP_STRATEGY_CODE,
        config=_config(),
        entry_rules=[EntryRule(side="short", when=Predicate(lhs="bar.close", op=">", rhs=0.0))],
        sizing=FixedFractionSizing(fraction=0.02),
        exit_rules=[stop],
    )
    assert service._exit_rules == [stop]


def test_long_only_spec_is_not_auto_injected() -> None:
    """A long-only spec gets no short stop: a long bottoms at zero, so its loss is
    already bounded by the deployed size."""
    service = TradingService(
        strategy_code=_NOOP_STRATEGY_CODE,
        config=_config(),
        entry_rules=[EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=0.0))],
        sizing=FixedFractionSizing(fraction=0.02),
        exit_rules=[],
    )
    assert service._exit_rules == []


def test_custom_code_path_auto_injects_short_stop() -> None:
    """The custom-code path (entry_rules=None) cannot enumerate sides at
    construction, so the short backstop is injected defensively — it is a no-op
    for any long the generated code opens (entry_price/1.0 floors at zero)."""
    service = TradingService(
        strategy_code=_NOOP_STRATEGY_CODE,
        config=_config(),
        entry_rules=None,
        exit_rules=[],
    )
    assert _has_full_loss_short_stop(service._exit_rules)


def test_empty_entry_rules_is_not_auto_injected() -> None:
    """An empty (not None) entry_rules list does NOT trigger injection — it is a
    no-trade engine spec, or a strategy-code-driven spec that should mark itself
    custom-code (which makes the mode layers pass entry_rules=None instead). Not
    injecting keeps _exit_rules empty so the chunked-bar fast path is not
    needlessly disabled for non-shorting engine/test specs. The 'sides unknown'
    signal is entry_rules=None (custom-code), not an empty list."""
    service = TradingService(
        strategy_code=_NOOP_STRATEGY_CODE,
        config=_config(),
        entry_rules=[],
        exit_rules=[],
    )
    assert service._exit_rules == []


def test_trading_service_surfaces_lookahead_violation() -> None:
    """A strategy touching a non-existent forward field aborts the run cleanly."""
    market_data: Dict[str, List[OHLCVBar]] = {}
    _uptrend_then_down_bars(market_data)

    strategy = StrategySpec(
        timeframe="1d",
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
        timeframe="1d",
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
        timeframe="1d",
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
    path. Sets ``parent_order_id`` — still engine-internal and refused at
    the strategy-side gate in ``OrderRequest.validate_prices``.
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
                    "parent_order_id": "engine-internal-id",
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
        timeframe="1d",
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(
                    lhs="bar.close", op=">", rhs=IndicatorRef(name="sma", params={"period": 5})
                ),
            )
        ],
        exit_rules=[
            SignalExitRule(
                when=Predicate(
                    lhs="bar.close", op="<", rhs=IndicatorRef(name="sma", params={"period": 5})
                )
            )
        ],
        strategy_code=_SMA_STRATEGY_CODE,
    )

    run = run_backtest(strategy=strategy, config=_config(), market_data=market_data)
    diagnostics = run.service_result.execution_diagnostics

    assert run.service_result.error is None, run.service_result.error
    # SMA(5) crossover: strategy emits 1 entry + 1 exit. The engine's
    # signal-exit dispatcher also emits a close when the SignalExitRule
    # predicate fires on the same bar (dedup at fill time ensures only
    # one close fills). Total emits = 3 (1 entry + 1 strategy exit +
    # 1 engine signal-exit).
    assert diagnostics.orders_emitted == 3
    assert diagnostics.orders_accepted == 3
    assert diagnostics.orders_rejected == 0
    assert diagnostics.orders_unfilled == 0
    assert diagnostics.exits_emitted == 2
    # #410: FillSimulator now reports entry/exit fill lifecycle events.
    # The SMA round-trip lands one FULL entry and one FULL exit.
    assert diagnostics.entries_filled == 1
    # Capped well under the limit for this fixture.
    assert len(diagnostics.last_order_events) <= _MAX_ORDER_EVENTS
    event_types = [e.event_type for e in diagnostics.last_order_events]
    assert "emitted" in event_types
    assert "accepted" in event_types
    assert "entry_filled" in event_types
    assert "exit_filled" in event_types


def test_diagnostics_unsupported_feature_records_rejection() -> None:
    """``trailing_stop`` is gated until #390; counts as unsupported_feature."""
    market_data: Dict[str, List[OHLCVBar]] = {}
    _uptrend_then_down_bars(market_data)

    strategy = StrategySpec(
        strategy_id="strat-409-unsupported",
        authored_by="tests",
        asset_class="equity",
        hypothesis="unsupported feature",
        signal_definition="parent_order_id",
        timeframe="1d",
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
        timeframe="1d",
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
        timeframe="1d",
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
        timeframe="1d",
        entry_rules=[],
        exit_rules=[],
        strategy_code=_NOISY_ORDER_STRATEGY_CODE,
    )

    run = run_backtest(strategy=strategy, config=_config(), market_data=market_data)
    diagnostics = run.service_result.execution_diagnostics

    # 30 bars of fixture × at least one event per bar > 20.
    assert diagnostics.orders_emitted >= 25
    assert len(diagnostics.last_order_events) == _MAX_ORDER_EVENTS


def test_diagnostics_entry_filled_no_exit_classified() -> None:
    """An entry that fills but never sees a matching exit lands in ENTRY_WITH_NO_EXIT.

    Closes the last AC for #404: covers the fourth required category alongside
    NO_ORDERS_EMITTED, ORDERS_REJECTED, and ORDERS_UNFILLED.
    """
    market_data: Dict[str, List[OHLCVBar]] = {}
    _uptrend_then_down_bars(market_data)

    # LateMarketStrategy emits a market BUY on bar 0 (no position yet); once
    # the order fills on bar 1, every subsequent on_bar returns early. With
    # an empty ``exit_rules`` list the engine injects no synthetic closes, so
    # the position stays open through end-of-stream — exactly the scenario
    # the ENTRY_WITH_NO_EXIT category is meant to classify.
    strategy = StrategySpec(
        strategy_id="strat-404-entry-no-exit",
        authored_by="tests",
        asset_class="equity",
        hypothesis="entry without exit",
        signal_definition="buy once, never sell",
        timeframe="1d",
        entry_rules=[],
        exit_rules=[],
        strategy_code=_LATE_MARKET_ORDER_STRATEGY_CODE,
    )

    run = run_backtest(strategy=strategy, config=_config(), market_data=market_data)
    diagnostics = run.service_result.execution_diagnostics

    assert run.service_result.error is None, run.service_result.error
    assert not run.trades
    assert diagnostics.entries_filled >= 1
    assert diagnostics.exits_emitted == 0
    assert diagnostics.closed_trades == 0
    assert diagnostics.orders_unfilled == 0
    assert diagnostics.zero_trade_category == "ENTRY_WITH_NO_EXIT"
    assert "filled but the strategy never emitted an exit" in diagnostics.summary
    entry_events = [e for e in diagnostics.last_order_events if e.event_type == "entry_filled"]
    assert entry_events, f"expected an entry_filled event, got {diagnostics.last_order_events}"


def test_run_backtest_without_strategy_code_raises() -> None:
    """The LLM-per-bar fallback is removed; no strategy_code must fail fast."""
    strategy = StrategySpec(
        strategy_id="strat-no-code",
        authored_by="legacy",
        asset_class="equity",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
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
        timeframe="1d",
        strategy_id="strat-sma-dq-1",
        authored_by="tests",
        asset_class="equity",
        hypothesis="momentum via SMA(5)",
        signal_definition="close vs sma(5)",
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(
                    lhs="bar.close", op=">", rhs=IndicatorRef(name="sma", params={"period": 5})
                ),
            )
        ],
        exit_rules=[
            SignalExitRule(
                when=Predicate(
                    lhs="bar.close", op="<", rhs=IndicatorRef(name="sma", params={"period": 5})
                )
            )
        ],
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
        timeframe="1d",
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
        timeframe="1d",
        strategy_id="strat-sma-385-backtest",
        authored_by="tests",
        asset_class="equity",
        hypothesis="momentum via SMA(5)",
        signal_definition="close vs sma(5)",
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(
                    lhs="bar.close", op=">", rhs=IndicatorRef(name="sma", params={"period": 5})
                ),
            )
        ],
        exit_rules=[
            SignalExitRule(
                when=Predicate(
                    lhs="bar.close", op="<", rhs=IndicatorRef(name="sma", params={"period": 5})
                )
            )
        ],
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


def test_historical_replay_canonicalizes_nonfinite_volume() -> None:
    """The replay stream feeds the strategy/predicates, so a non-finite volume
    in the market-data dict must be coerced to 0.0 — the same value the dataset
    fingerprint collapses NaN/inf to — so a backtest can't behave differently
    for a NaN-volume dataset than for the 0.0 it shares a fingerprint with.
    """
    import math

    market_data: Dict[str, List[OHLCVBar]] = {
        "AAA": [
            OHLCVBar(
                date="2024-01-01", open=10.0, high=11.0, low=9.0, close=10.5, volume=float("nan")
            ),
            OHLCVBar(date="2024-01-02", open=10.5, high=11.5, low=9.5, close=11.0, volume=1234.0),
        ]
    }
    bars = [
        e.bar
        for e in HistoricalReplayStream(market_data, timeframe="1d")
        if isinstance(e, BarEvent)
    ]
    assert [b.volume for b in bars] == [0.0, 1234.0]
    assert all(math.isfinite(b.volume) for b in bars)
    # OHLC is untouched.
    assert (bars[0].open, bars[0].close) == (10.0, 10.5)


def test_compiled_src_helper_canonicalizes_nonfinite_volume() -> None:
    """The generated ``_src`` helper that strategies use to read ``bar.volume``
    must coerce a non-finite volume to 0.0 so a predicate over volume can't
    diverge from the 0.0 dataset it shares a fingerprint with."""
    import math
    from types import SimpleNamespace

    from investment_team.strategy_lab.synthesis.compiler import _emit_source_helper

    ns: dict = {}
    # The helper is a method body; exec it into a namespace and bind to a stub.
    exec(_emit_source_helper(), ns)  # noqa: S102 — exercising generated helper
    src = ns["_src"]
    stub = SimpleNamespace()

    nan_bar = SimpleNamespace(open=1.0, high=1.0, low=1.0, close=1.0, volume=float("nan"))
    inf_bar = SimpleNamespace(open=1.0, high=1.0, low=1.0, close=1.0, volume=float("inf"))
    good_bar = SimpleNamespace(open=1.0, high=1.0, low=1.0, close=1.0, volume=4321.0)

    assert src(stub, nan_bar, "volume") == 0.0
    assert src(stub, inf_bar, "volume") == 0.0
    assert src(stub, good_bar, "volume") == 4321.0
    assert math.isfinite(src(stub, nan_bar, "volume"))
    # Non-volume sources are unaffected.
    assert src(stub, good_bar, "close") == 1.0


# ---------------------------------------------------------------------------
# Direct unit tests for the helpers extracted from the per-bar event loop.
# These methods do not depend on instance state, so they are exercised on a
# bare instance (``__new__``) with lightweight fakes; the full per-bar loop is
# additionally covered end-to-end by the golden snapshot + simulator-invariant
# suites and the integration tests above.
# ---------------------------------------------------------------------------


def _bare_service() -> "TradingService":
    return TradingService.__new__(TradingService)


def test_drain_unfilled_at_eos_empty_queue_is_noop() -> None:
    """An empty pending queue records nothing and leaves diagnostics untouched."""
    from investment_team.trading_service.service import TradingServiceResult

    result = TradingServiceResult()
    _bare_service()._drain_unfilled_at_eos([], None, result)
    assert result.execution_diagnostics.orders_unfilled == 0
    assert result.execution_diagnostics.last_order_events == []


def test_drain_unfilled_at_eos_records_end_of_stream() -> None:
    """Each still-pending order is counted and recorded as an end_of_stream unfilled event."""
    from types import SimpleNamespace

    from investment_team.trading_service.service import TradingServiceResult

    result = TradingServiceResult()
    req = SimpleNamespace(
        symbol="AAPL",
        side=SimpleNamespace(value="buy"),
        order_type=SimpleNamespace(value="market"),
    )
    prev_bar = SimpleNamespace(timestamp="2024-01-05T00:00:00")
    _bare_service()._drain_unfilled_at_eos([req, req], prev_bar, result)
    assert result.execution_diagnostics.orders_unfilled == 2
    events = result.execution_diagnostics.last_order_events
    assert len(events) == 2
    assert all(e.event_type == "unfilled" and e.reason == "end_of_stream" for e in events)
    assert events[-1].timestamp == "2024-01-05T00:00:00"


def test_finalize_result_success_records_open_positions() -> None:
    """On the success path (fill_sim supplied) open-position entry reasons are recorded."""
    from types import SimpleNamespace

    from investment_team.trading_service.service import TradingServiceResult

    result = TradingServiceResult()
    eod_buffer = SimpleNamespace(materialize=lambda: ["curve"])
    harness = SimpleNamespace(probe_events=["probe"])
    fill_sim = SimpleNamespace(
        portfolio=SimpleNamespace(
            positions={
                "AAPL": SimpleNamespace(entry_reason="breakout"),
                "MSFT": SimpleNamespace(entry_reason=""),  # falsy → skipped
            }
        )
    )
    out = _bare_service()._finalize_result(result, eod_buffer, harness, fill_sim=fill_sim)
    assert out is result
    assert result.streaming_equity_curve == ["curve"]
    assert result.probe_events == ["probe"]
    assert result.open_position_entry_reasons == ["breakout"]


def test_finalize_result_abort_omits_open_positions() -> None:
    """Without fill_sim (abort path) the open-position list is left untouched."""
    from types import SimpleNamespace

    from investment_team.trading_service.service import TradingServiceResult

    result = TradingServiceResult()
    eod_buffer = SimpleNamespace(materialize=lambda: [])
    harness = SimpleNamespace(probe_events=[])
    _bare_service()._finalize_result(result, eod_buffer, harness)
    assert result.open_position_entry_reasons == []


def test_abort_result_classifies_lookahead_violations() -> None:
    """LookAheadError and a lookahead StrategyRuntimeError both set lookahead_violation."""
    from types import SimpleNamespace

    from investment_team.execution.bar_safety import LookAheadError
    from investment_team.trading_service.service import TradingServiceResult
    from investment_team.trading_service.strategy.streaming_harness import StrategyRuntimeError

    eod_buffer = SimpleNamespace(materialize=lambda: [])
    harness = SimpleNamespace(probe_events=[])
    svc = _bare_service()

    look_ahead = LookAheadError(
        order_id="o1", submitted_at="2024-01-02", fill_bar_timestamp="2024-01-02"
    )
    r1 = svc._abort_result(TradingServiceResult(), look_ahead, eod_buffer, harness)
    assert r1.error == str(look_ahead)
    assert r1.lookahead_violation is True

    r2 = svc._abort_result(
        TradingServiceResult(),
        StrategyRuntimeError("future read", etype="lookahead_violation"),
        eod_buffer,
        harness,
    )
    assert r2.lookahead_violation is True

    r3 = svc._abort_result(
        TradingServiceResult(),
        StrategyRuntimeError("boom", etype="runtime_error"),
        eod_buffer,
        harness,
    )
    assert r3.error == "boom"
    assert r3.lookahead_violation is False


def test_process_one_bar_warmup_skips_fills_and_does_not_count() -> None:
    """A warm-up bar skips fills/MTM (bars_processed unchanged), still appends the bar
    and applies the strategy response fetched via the thunk."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from investment_team.trading_service.service import TradingServiceResult

    svc = _bare_service()
    result = TradingServiceResult()
    pending_in = [SimpleNamespace(symbol="AAPL")]
    cur_bar = SimpleNamespace(symbol="AAPL", timestamp="2024-01-02T00:00:00", close=10.0)
    fetched: dict = {}

    def _fetch() -> tuple:
        fetched["called"] = True
        return ([], [])

    with (
        patch.object(svc, "_append_streaming_bar") as append_mock,
        patch.object(svc, "_process_bar_strategy_response") as resp_mock,
    ):
        out_pending = svc._process_one_bar(
            cur_bar=cur_bar,
            next_bar=None,
            prev_bar=None,
            is_warmup=True,
            fetch_response=_fetch,
            pending_for_prev=pending_in,
            portfolio=None,
            order_book=None,
            fill_sim=None,
            harness=None,
            on_trade=None,
            result=result,
            eod_buffer=None,
            position_tracker={},
            engine_exits=None,
            engine_entries=None,
            streaming_views={},
        )

    # Warm-up bars are not counted and the fill path (which would need the
    # portfolio/fill_sim fakes) is skipped entirely.
    assert result.bars_processed == 0
    assert fetched.get("called") is True
    # The current bar is appended to the streaming views, and the strategy
    # response from the thunk is forwarded verbatim with is_warmup=True.
    append_mock.assert_called_once_with({}, cur_bar)
    resp_mock.assert_called_once()
    resp_kwargs = resp_mock.call_args.kwargs
    assert resp_kwargs["cur_bar"] is cur_bar
    assert resp_kwargs["bar_orders"] == []
    assert resp_kwargs["bar_cancels"] == []
    assert resp_kwargs["is_warmup"] is True
    # The pending queue passes through unchanged on warm-up bars.
    assert out_pending is pending_in


def test_process_one_bar_normal_path_processes_fills_and_counts() -> None:
    """A post-warm-up bar processes fills, marks to market, stamps EOD equity, and
    increments bars_processed; the thunk's orders/cancels are forwarded."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from investment_team.trading_service.service import TradingServiceResult

    svc = _bare_service()
    svc._exit_rules = []  # skip the engine position-tracker update
    result = TradingServiceResult()
    cur_bar = SimpleNamespace(symbol="AAPL", timestamp="2024-01-03T00:00:00", close=12.0)
    outcome = SimpleNamespace(entry_fills=[], exit_fills=[], closed_trades=[], diagnostic_events=[])
    fill_sim = SimpleNamespace(process_bar=lambda _cur, next_bar=None: outcome)
    mtm_calls: list = []
    eod_records: list = []
    portfolio = SimpleNamespace(
        update_last_price=lambda sym, px: mtm_calls.append((sym, px)),
        mark_to_market=lambda: 100_000.0,
    )
    eod_buffer = SimpleNamespace(record=lambda ts, eq: eod_records.append((ts, eq)))

    def _fetch() -> tuple:
        return ([{"order": 1}], [{"cancel": 2}])

    with (
        patch.object(svc, "_append_streaming_bar") as append_mock,
        patch.object(svc, "_process_bar_strategy_response") as resp_mock,
    ):
        out_pending = svc._process_one_bar(
            cur_bar=cur_bar,
            next_bar=None,
            prev_bar=None,  # no prior bar → date-change expiry skipped
            is_warmup=False,
            fetch_response=_fetch,
            pending_for_prev=[],  # empty → pending-submit block skipped
            portfolio=portfolio,
            order_book=None,
            fill_sim=fill_sim,
            harness=None,
            on_trade=None,
            result=result,
            eod_buffer=eod_buffer,
            position_tracker={},
            engine_exits=None,
            engine_entries=None,
            streaming_views={},
        )

    assert result.bars_processed == 1  # counted after fetch_response on the non-warmup path
    assert mtm_calls == [("AAPL", 12.0)]
    assert eod_records == [("2024-01-03T00:00:00", 100_000.0)]
    append_mock.assert_called_once_with({}, cur_bar)
    resp_kwargs = resp_mock.call_args.kwargs
    assert resp_kwargs["bar_orders"] == [{"order": 1}]
    assert resp_kwargs["bar_cancels"] == [{"cancel": 2}]
    assert resp_kwargs["is_warmup"] is False
    assert out_pending == []
