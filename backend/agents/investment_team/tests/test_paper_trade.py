"""End-to-end tests for the paper-trade mode.

These tests use a stub provider that emits a canned sequence of historical
warm-up bars followed by live native bars — no network, no real websockets.
The focus is on the orchestration contract:

* warm-up orders are dropped (belt-and-suspenders)
* ``min_fills`` terminates cleanly
* ``StopController`` terminates cleanly
* cutover timestamp is captured
* region-block on primary triggers geo-failover to secondary
* live bars whose timestamp is earlier than cutover are defensively dropped
"""

from __future__ import annotations

import textwrap
from typing import Iterator, List, Optional

import pytest

from investment_team.models import BacktestConfig, StrategySpec
from investment_team.trading_service.data_stream.protocol import BarEvent
from investment_team.trading_service.data_stream.resampler import (
    NativeBar,
    NativeEvent,
)
from investment_team.trading_service.modes.paper_trade import (
    PaperTradeConfig,
    StopController,
    run_paper_trade,
)
from investment_team.trading_service.providers.base import (
    ProviderCapabilities,
    ProviderRegionBlocked,
)
from investment_team.trading_service.providers.registry import ProviderRegistry
from investment_team.trading_service.strategy.contract import Bar

# ---------------------------------------------------------------------------
# A deterministic "trade every bar" strategy — one round-trip per 2 live bars.
# ---------------------------------------------------------------------------


_ALTERNATING_STRATEGY = textwrap.dedent('''\
    """Alternates entries and exits on every bar. Deterministic.

    Respects ctx.is_warmup — no orders emitted during warm-up. Every live bar:
      - If no position: submit LONG MARKET (qty=1)
      - If position: submit SHORT MARKET (qty=pos.qty) to close
    """
    from contract import OrderSide, OrderType, Strategy


    class AlternatingTrader(Strategy):
        def on_bar(self, ctx, bar):
            if ctx.is_warmup:
                return
            pos = ctx.position(bar.symbol)
            if pos is None:
                ctx.submit_order(
                    symbol=bar.symbol,
                    side=OrderSide.LONG,
                    qty=1,
                    order_type=OrderType.MARKET,
                    reason="enter",
                )
            else:
                ctx.submit_order(
                    symbol=bar.symbol,
                    side=OrderSide.SHORT,
                    qty=pos.qty,
                    order_type=OrderType.MARKET,
                    reason="exit",
                )
''')


_WARMUP_SUBMIT_STRATEGY = textwrap.dedent('''\
    """Ignores ctx.is_warmup and always submits — tests the dropping safety net."""
    from contract import OrderSide, OrderType, Strategy


    class IgnoresWarmup(Strategy):
        def on_bar(self, ctx, bar):
            pos = ctx.position(bar.symbol)
            if pos is None:
                ctx.submit_order(
                    symbol=bar.symbol,
                    side=OrderSide.LONG,
                    qty=1,
                    order_type=OrderType.MARKET,
                    reason="ignore_warmup",
                )
''')


# ---------------------------------------------------------------------------
# Stub provider
# ---------------------------------------------------------------------------


class _StubProvider:
    def __init__(
        self,
        *,
        name: str = "stub",
        supports: Optional[set[str]] = None,
        historical_bars: Optional[List[BarEvent]] = None,
        live_events: Optional[List[NativeEvent]] = None,
        live_raises: Optional[Exception] = None,
    ) -> None:
        self.capabilities = ProviderCapabilities(
            name=name,
            supports=supports or {"crypto"},
            historical_timeframes={"1m"},
            live_timeframes={"1m"},
        )
        self._historical_bars = historical_bars or []
        self._live_events = live_events or []
        self._live_raises = live_raises

    def smallest_available(self, asset_class: str, *, live: bool) -> Optional[str]:
        return "1m"

    def historical(self, **kwargs) -> Iterator[BarEvent]:
        yield from self._historical_bars

    def live(self, **kwargs) -> Iterator[NativeEvent]:
        if self._live_raises is not None:
            raise self._live_raises
        yield from self._live_events


def _hist_bar(ts: str, close: float, symbol: str = "BTC") -> BarEvent:
    # ``volume`` must be large enough that the strategy's qty=1 order doesn't
    # hit the realistic execution model's 10% participation cap on either
    # entry or exit. Pre-#386 exits ignored the cap, so an arbitrary
    # placeholder (1.0) worked; post-#386 the cap applies symmetrically and
    # tiny-volume bars clip a qty=1 fill into a qty=0.1 partial.
    return BarEvent(
        bar=Bar(
            symbol=symbol,
            timestamp=ts,
            timeframe="1m",
            open=close,
            high=close,
            low=close,
            close=close,
            volume=1_000_000.0,
        )
    )


def _native_bar(close_ts: str, close: float, symbol: str = "BTC", tf: str = "1m") -> NativeBar:
    return NativeBar(
        symbol=symbol,
        timestamp=close_ts,
        timeframe=tf,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1_000_000.0,
    )


def _registry_with(
    primary: _StubProvider, fallback: Optional[_StubProvider] = None
) -> ProviderRegistry:
    reg = ProviderRegistry()
    reg.register(
        lambda p=primary: p,
        primary.capabilities,
        default_for=list(primary.capabilities.supports),
    )
    if fallback is not None:
        reg.register(
            lambda f=fallback: f,
            fallback.capabilities,
            secondary_for=list(fallback.capabilities.supports),
        )
    return reg


def _strategy(code: str) -> StrategySpec:
    return StrategySpec(
        strategy_id="stub-strat",
        authored_by="test",
        asset_class="crypto",
        hypothesis="h",
        signal_definition="s",
        entry_rules=[],
        exit_rules=[],
        strategy_code=code,
    )


def _btc_config() -> BacktestConfig:
    return BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-01-02",
        initial_capital=100_000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
        metrics_engine="legacy",
    )


def _paper_config(**overrides) -> PaperTradeConfig:
    kwargs = dict(
        symbols=["BTC"],
        asset_class="crypto",
        strategy_timeframe="1m",
        min_fills=2,
        max_hours=1.0,
        warmup_bars=3,
    )
    kwargs.update(overrides)
    return PaperTradeConfig(**kwargs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_min_fills_terminates_live_phase() -> None:
    # Warm-up: 3 bars, all dropped by the strategy (it honors ctx.is_warmup).
    warmup = [
        _hist_bar("2024-05-01T11:57:00Z", 100.0),
        _hist_bar("2024-05-01T11:58:00Z", 101.0),
        _hist_bar("2024-05-01T11:59:00Z", 102.0),
    ]
    # Live: enough native bars for 2 full round-trips (4 bars = 2 trades).
    # Each native bar equals the target timeframe so passthrough applies.
    live = [
        _native_bar("2024-05-01T12:01:00Z", 103.0),  # strategy enters LONG
        _native_bar("2024-05-01T12:02:00Z", 104.0),  # fill entry + exit submitted
        _native_bar("2024-05-01T12:03:00Z", 105.0),  # fill exit -> trade 1
        _native_bar("2024-05-01T12:04:00Z", 106.0),  # enter again
        _native_bar("2024-05-01T12:05:00Z", 107.0),  # fill entry + exit submitted
        _native_bar("2024-05-01T12:06:00Z", 108.0),  # fill exit -> trade 2
        # Buffer bars in case the service loop needs one more iteration
        _native_bar("2024-05-01T12:07:00Z", 109.0),
        _native_bar("2024-05-01T12:08:00Z", 110.0),
    ]
    provider = _StubProvider(historical_bars=warmup, live_events=live)
    result = run_paper_trade(
        strategy=_strategy(_ALTERNATING_STRATEGY),
        backtest_config=_btc_config(),
        paper_config=_paper_config(min_fills=2),
        registry=_registry_with(provider),
    )

    assert result.terminated_reason == "fill_target_reached"
    assert result.fill_count >= 2
    assert result.cutover_ts == "2024-05-01T12:01:00Z"
    assert result.provider_id == "stub"
    # Warm-up strategy honored ctx.is_warmup -> no dropped-orders counter.
    assert result.service_result.warmup_orders_dropped == 0


def test_warmup_orders_are_dropped() -> None:
    warmup = [_hist_bar("2024-05-01T11:58:00Z", 100.0), _hist_bar("2024-05-01T11:59:00Z", 101.0)]
    live = [_native_bar("2024-05-01T12:01:00Z", 102.0)]
    provider = _StubProvider(historical_bars=warmup, live_events=live)
    result = run_paper_trade(
        strategy=_strategy(_WARMUP_SUBMIT_STRATEGY),  # deliberately submits during warm-up
        backtest_config=_btc_config(),
        paper_config=_paper_config(warmup_bars=2, min_fills=999),
        registry=_registry_with(provider),
    )
    # Strategy tried to submit on every warm-up bar; all were dropped.
    assert result.service_result.warmup_orders_dropped == 2
    # No trade happened because the "entry order" submitted on the one live
    # bar has no *next* bar to fill against in this canned stream.
    assert result.fill_count == 0


def test_user_stop_terminates_cleanly() -> None:
    warmup: List[BarEvent] = []  # skip warmup to simplify
    live = [_native_bar(f"2024-05-01T12:{i:02d}:00Z", 100.0 + i) for i in range(1, 20)]
    provider = _StubProvider(historical_bars=warmup, live_events=live)
    controller = StopController()
    controller.request_stop()  # immediate stop

    result = run_paper_trade(
        strategy=_strategy(_ALTERNATING_STRATEGY),
        backtest_config=_btc_config(),
        paper_config=_paper_config(warmup_bars=0, min_fills=999),
        stop_controller=controller,
        registry=_registry_with(provider),
    )
    assert result.terminated_reason == "user_stop"


def test_cutover_timestamp_is_first_live_bar() -> None:
    live = [
        _native_bar("2024-05-01T12:10:00Z", 100.0),
        _native_bar("2024-05-01T12:11:00Z", 101.0),
    ]
    provider = _StubProvider(historical_bars=[], live_events=live)
    result = run_paper_trade(
        strategy=_strategy(_ALTERNATING_STRATEGY),
        backtest_config=_btc_config(),
        paper_config=_paper_config(warmup_bars=0, min_fills=999),
        stop_controller=StopController(),
        registry=_registry_with(provider),
    )
    assert result.cutover_ts == "2024-05-01T12:10:00Z"


def test_region_blocked_primary_falls_over_to_secondary() -> None:
    primary = _StubProvider(
        name="binance",
        live_raises=ProviderRegionBlocked("binance blocked"),
    )
    secondary_live = [
        _native_bar("2024-05-01T12:01:00Z", 100.0),
        _native_bar("2024-05-01T12:02:00Z", 101.0),
        _native_bar("2024-05-01T12:03:00Z", 102.0),
    ]
    secondary = _StubProvider(name="coinbase", live_events=secondary_live)
    reg = _registry_with(primary, secondary)

    stop = StopController()

    result = run_paper_trade(
        strategy=_strategy(_ALTERNATING_STRATEGY),
        backtest_config=_btc_config(),
        paper_config=_paper_config(warmup_bars=0, min_fills=1),
        stop_controller=stop,
        registry=reg,
    )
    assert result.provider_id == "coinbase"
    assert result.cutover_ts == "2024-05-01T12:01:00Z"


def test_max_hours_wall_clock_guard() -> None:
    live = [_native_bar(f"2024-05-01T12:{i:02d}:00Z", 100.0 + i) for i in range(1, 10)]
    provider = _StubProvider(historical_bars=[], live_events=live)

    clock_state = {"t": 1_000_000.0}

    def _clock() -> float:
        clock_state["t"] += 3_600.0 * 2.0  # each call advances 2h — beyond max_hours
        return clock_state["t"]

    result = run_paper_trade(
        strategy=_strategy(_ALTERNATING_STRATEGY),
        backtest_config=_btc_config(),
        paper_config=_paper_config(warmup_bars=0, min_fills=999, max_hours=1.0),
        stop_controller=StopController(),
        registry=_registry_with(provider),
        clock=_clock,
    )
    assert result.terminated_reason == "max_hours"


def test_min_fills_below_20_emits_warning() -> None:
    provider = _StubProvider(historical_bars=[], live_events=[])
    result = run_paper_trade(
        strategy=_strategy(_ALTERNATING_STRATEGY),
        backtest_config=_btc_config(),
        paper_config=_paper_config(warmup_bars=0, min_fills=5),
        registry=_registry_with(provider),
    )
    assert "min_fills_below_recommended" in result.warnings


def test_stocks_asset_class_routes_to_equities_provider() -> None:
    """Codex P1 on PR #188: Strategy-Lab specs use "stocks"; the registry
    resolves on "equities". ``run_paper_trade`` must normalise before
    asking the registry, and it must also pass the canonical label to
    the adapter so ``smallest_available`` returns a real timeframe rather
    than ``None`` (which would trip LiveStream into a ``no_live_feed``
    error).
    """
    live = [
        _native_bar("2024-05-01T12:01:00Z", 100.0, symbol="AAPL"),
        _native_bar("2024-05-01T12:02:00Z", 101.0, symbol="AAPL"),
    ]
    provider = _StubProvider(
        name="alpaca",
        supports={"equities"},
        historical_bars=[],
        live_events=live,
    )
    reg = ProviderRegistry()
    reg.register(
        lambda p=provider: p,
        provider.capabilities,
        default_for=["equities"],
    )

    strategy = StrategySpec(
        strategy_id="equities-strat",
        authored_by="test",
        asset_class="stocks",  # ← the key: legacy label, not "equities"
        hypothesis="h",
        signal_definition="s",
        entry_rules=[],
        exit_rules=[],
        strategy_code=_ALTERNATING_STRATEGY,
    )

    result = run_paper_trade(
        strategy=strategy,
        backtest_config=_btc_config(),
        paper_config=PaperTradeConfig(
            symbols=["AAPL"],
            asset_class="stocks",  # legacy label flows through paper_config too
            strategy_timeframe="1m",
            min_fills=1,
            max_hours=1.0,
            warmup_bars=0,
        ),
        registry=reg,
    )
    # Must NOT terminate with no_provider — that's the bug the fix prevents.
    assert result.terminated_reason != "no_provider"
    assert result.provider_id == "alpaca"
    assert result.cutover_ts == "2024-05-01T12:01:00Z"


# ---------------------------------------------------------------------------
# Issue #375 — preflight data quality at warm-up + live-gap monitor
# ---------------------------------------------------------------------------


def test_paper_trade_attaches_warmup_data_quality_report() -> None:
    """Clean warm-up bars → ``data_quality_report`` populated with severity 'ok'."""
    warmup = [
        _hist_bar("2024-05-01T11:57:00Z", 100.0),
        _hist_bar("2024-05-01T11:58:00Z", 101.0),
        _hist_bar("2024-05-01T11:59:00Z", 102.0),
    ]
    live = [
        _native_bar("2024-05-01T12:01:00Z", 103.0),
        _native_bar("2024-05-01T12:02:00Z", 104.0),
        _native_bar("2024-05-01T12:03:00Z", 105.0),
    ]
    provider = _StubProvider(historical_bars=warmup, live_events=live)
    result = run_paper_trade(
        strategy=_strategy(_ALTERNATING_STRATEGY),
        backtest_config=_btc_config(),
        paper_config=_paper_config(warmup_bars=3, min_fills=999, max_hours=1.0),
        registry=_registry_with(provider),
    )
    assert result.data_quality_report is not None
    assert result.data_quality_report["severity"] == "ok"


def test_paper_trade_warns_on_warmup_gap() -> None:
    """A long gap inside the warm-up window surfaces on ``warnings``."""
    # 10 minute-bars then a 30-bar gap then 1 more — at 1m frequency that's
    # well above the 5-bar threshold for a fail-class gap.
    warmup: List[BarEvent] = []
    for i in range(10):
        warmup.append(_hist_bar(f"2024-05-01T11:{i:02d}:00Z", 100.0 + i))
    # Skip 30 minutes ⇒ multiple missing 1m bars ⇒ severity == "fail".
    warmup.append(_hist_bar("2024-05-01T11:40:00Z", 110.0))

    live = [
        _native_bar("2024-05-01T12:01:00Z", 111.0),
        _native_bar("2024-05-01T12:02:00Z", 112.0),
        _native_bar("2024-05-01T12:03:00Z", 113.0),
    ]
    provider = _StubProvider(historical_bars=warmup, live_events=live)
    result = run_paper_trade(
        strategy=_strategy(_ALTERNATING_STRATEGY),
        backtest_config=_btc_config(),
        paper_config=_paper_config(warmup_bars=11, min_fills=999, max_hours=1.0),
        registry=_registry_with(provider),
    )
    assert result.data_quality_report is not None
    # Warm-up validation runs in warn mode so the run still proceeds, but
    # the advisory must be on ``warnings`` for ops to act on.
    assert any(w.startswith("data_quality:warmup:") for w in result.warnings)


def test_paper_trade_emits_live_gap_warning() -> None:
    """A live-bar gap >5x the strategy timeframe surfaces a structured warning."""
    # No warm-up. Two live bars 30 minutes apart at 1m frequency.
    live = [
        _native_bar("2024-05-01T12:00:00Z", 100.0),
        _native_bar("2024-05-01T12:30:00Z", 101.0),  # 30x
        _native_bar("2024-05-01T12:31:00Z", 102.0),
    ]
    provider = _StubProvider(historical_bars=[], live_events=live)
    result = run_paper_trade(
        strategy=_strategy(_ALTERNATING_STRATEGY),
        backtest_config=_btc_config(),
        paper_config=_paper_config(warmup_bars=0, min_fills=999, max_hours=1.0),
        registry=_registry_with(provider),
    )
    assert "data_quality:live_gap:BTC" in result.warnings


def test_missing_strategy_code_raises() -> None:
    provider = _StubProvider()
    with pytest.raises(ValueError, match="strategy_code is required"):
        run_paper_trade(
            strategy=StrategySpec(
                strategy_id="x",
                authored_by="test",
                asset_class="crypto",
                hypothesis="h",
                signal_definition="s",
                strategy_code=None,
            ),
            backtest_config=_btc_config(),
            paper_config=_paper_config(),
            registry=_registry_with(provider),
        )
