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


def test_zero_trade_result_gets_unknown_diagnostics_until_order_counters_are_instrumented() -> None:
    """A no-op strategy gets a deterministic #408 zero-trade category."""
    market_data: Dict[str, List[OHLCVBar]] = {}
    _uptrend_then_down_bars(market_data)

    strategy = StrategySpec(
        strategy_id="strat-noop-408",
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
    assert diagnostics.zero_trade_category == "UNKNOWN_ZERO_TRADE_PATH"
    assert diagnostics.closed_trades == 0
    assert diagnostics.bars_processed == run.service_result.bars_processed
    assert "not instrumented yet" in diagnostics.summary


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
        monkeypatch.delenv("TRADING_PARTIAL_FILL_DEFAULTS_ENABLED", raising=False)

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


def test_partial_fill_defaults_flag_default_is_off(monkeypatch) -> None:
    """With the env var unset, the helper reports ``False`` (opt-in semantics)."""
    monkeypatch.delenv("TRADING_PARTIAL_FILL_DEFAULTS_ENABLED", raising=False)
    from investment_team.trading_service.service import _partial_fill_defaults_enabled

    assert _partial_fill_defaults_enabled() is False
