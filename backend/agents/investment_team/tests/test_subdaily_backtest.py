"""Sub-daily backtest end-to-end via provider registry.

PR 2 §4.1 — the provider-driven backtest path should resolve a historical
adapter through the registry and stream bars at the strategy's declared
timeframe (e.g. ``"15m"``), sharing the exact event loop and metrics
computation the daily-bar backtest uses.
"""

from __future__ import annotations

import textwrap
from typing import Iterator, List, Optional

import pytest

from investment_team.models import BacktestConfig, StrategySpec
from investment_team.trading_service.data_stream.protocol import BarEvent
from investment_team.trading_service.data_stream.resampler import NativeEvent
from investment_team.trading_service.modes.backtest import run_backtest
from investment_team.trading_service.providers.base import ProviderCapabilities
from investment_team.trading_service.providers.registry import ProviderRegistry
from investment_team.trading_service.strategy.contract import Bar

_SMA_STRATEGY_CODE = textwrap.dedent("""\
    from contract import OrderSide, OrderType, Strategy


    class SmaCrossover(Strategy):
        WINDOW = 3

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
                    qty=1,
                    order_type=OrderType.MARKET,
                    reason="x",
                )
            elif pos is not None and bar.close < sma:
                ctx.submit_order(
                    symbol=bar.symbol,
                    side=OrderSide.SHORT,
                    qty=pos.qty,
                    order_type=OrderType.MARKET,
                    reason="y",
                )
""")


class _StubHistoricalProvider:
    """Emits a fixed list of BarEvents for any (symbols, range, timeframe)."""

    capabilities = ProviderCapabilities(
        name="stub_hist",
        supports={"crypto"},
        historical_timeframes={"15m", "1h", "1d"},
        live_timeframes=set(),
    )

    def __init__(self, bars: List[BarEvent]) -> None:
        self._bars = bars
        self.last_call: Optional[dict] = None

    def smallest_available(self, asset_class: str, *, live: bool) -> Optional[str]:
        if asset_class != "crypto" or live:
            return None
        return "15m"

    def historical(self, **kwargs) -> Iterator[BarEvent]:
        self.last_call = kwargs
        yield from self._bars

    def live(self, **kwargs) -> Iterator[NativeEvent]:
        raise NotImplementedError("historical-only stub")


def _bar(ts: str, close: float, symbol: str = "BTC", tf: str = "15m") -> BarEvent:
    return BarEvent(
        bar=Bar(
            symbol=symbol,
            timestamp=ts,
            timeframe=tf,
            open=close - 0.1,
            high=close + 0.1,
            low=close - 0.2,
            close=close,
            volume=1_000_000.0,
        )
    )


def _strategy() -> StrategySpec:
    return StrategySpec(
        strategy_id="sub-daily",
        authored_by="test",
        asset_class="crypto",
        hypothesis="h",
        signal_definition="s",
        entry_rules=[],
        exit_rules=[],
        strategy_code=_SMA_STRATEGY_CODE,
    )


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date="2024-05-01",
        end_date="2024-05-02",
        initial_capital=100_000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
        metrics_engine="legacy",
    )


def _registry_with(provider: _StubHistoricalProvider) -> ProviderRegistry:
    reg = ProviderRegistry()
    reg.register(
        lambda p=provider: p,
        provider.capabilities,
        default_for=["crypto"],
    )
    return reg


def test_provider_driven_backtest_15m() -> None:
    """End-to-end 15m backtest with at least one closed trade."""
    bars = [
        # Priming bars for the SMA(3) window, then an up-move that triggers entry,
        # followed by a down-move that triggers exit.
        _bar("2024-05-01T00:15:00Z", 100.0),
        _bar("2024-05-01T00:30:00Z", 100.5),
        _bar("2024-05-01T00:45:00Z", 101.0),
        _bar("2024-05-01T01:00:00Z", 104.0),  # crosses above SMA → entry
        _bar("2024-05-01T01:15:00Z", 106.0),  # fill entry
        _bar("2024-05-01T01:30:00Z", 102.0),
        _bar("2024-05-01T01:45:00Z", 95.0),  # crosses below SMA → exit
        _bar("2024-05-01T02:00:00Z", 92.0),  # fill exit
    ]
    provider = _StubHistoricalProvider(bars)
    run = run_backtest(
        strategy=_strategy(),
        config=_config(),
        symbols=["BTC"],
        asset_class="crypto",
        timeframe="15m",
        registry=_registry_with(provider),
    )
    assert run.service_result.error is None, run.service_result.error
    assert len(run.trades) >= 1
    assert provider.last_call is not None
    assert provider.last_call["timeframe"] == "15m"
    assert provider.last_call["symbols"] == ["BTC"]


def test_explicit_provider_id_routes_to_that_provider() -> None:
    primary = _StubHistoricalProvider([])
    secondary = _StubHistoricalProvider([])
    reg = ProviderRegistry()
    reg.register(lambda: primary, primary.capabilities, default_for=["crypto"])
    reg.register(
        lambda: secondary,
        ProviderCapabilities(
            name="secondary",
            supports={"crypto"},
            historical_timeframes={"15m"},
            live_timeframes=set(),
        ),
    )
    run_backtest(
        strategy=_strategy(),
        config=_config(),
        symbols=["BTC"],
        asset_class="crypto",
        timeframe="15m",
        provider_id="secondary",
        registry=reg,
    )
    # Primary's `last_call` should remain None; secondary should have been used.
    assert primary.last_call is None


def test_neither_data_source_raises() -> None:
    with pytest.raises(ValueError, match="exactly one data source"):
        run_backtest(
            strategy=_strategy(),
            config=_config(),
        )


def test_both_data_sources_raises() -> None:
    with pytest.raises(ValueError, match="exactly one data source"):
        run_backtest(
            strategy=_strategy(),
            config=_config(),
            market_data={},
            symbols=["BTC"],
            asset_class="crypto",
        )


def test_legacy_market_data_path_still_works() -> None:
    """The legacy pre-fetched-dict path should be unchanged from PR 1."""
    from investment_team.market_data_service import OHLCVBar

    market_data = {
        "BTC": [
            OHLCVBar(
                date="2024-05-01",
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.5,
                volume=1_000_000.0,
            )
        ]
    }
    run = run_backtest(
        strategy=_strategy(),
        config=_config(),
        market_data=market_data,
        timeframe="1d",
    )
    assert run.service_result.error is None
