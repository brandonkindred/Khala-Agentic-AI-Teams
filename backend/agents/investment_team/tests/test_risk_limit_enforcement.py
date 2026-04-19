"""Phase 3 regression tests.

Covers the two schema-boundary promotions:

* ``StrategySpec.risk_limits`` is now a validated ``RiskLimits`` — dict
  payloads still deserialize (via ``from_legacy_dict``), ``None`` collapses
  to the default, and unknown LLM keys are silently dropped.
* Drawdown circuit-breaker termination is propagated from
  ``TradingServiceResult`` onto ``BacktestResult.terminated_reason`` so the
  API / persisted records carry the signal without peeking at the service
  result.
"""

from __future__ import annotations

import textwrap
from typing import List

import pytest

from investment_team.execution.bar_safety import BarSafetyAssertion
from investment_team.execution.risk_filter import RiskFilter, RiskLimits
from investment_team.market_data_service import OHLCVBar
from investment_team.models import BacktestConfig, StrategySpec
from investment_team.trading_service.data_stream.historical_replay import (
    HistoricalReplayStream,
)
from investment_team.trading_service.engine.fill_simulator import (
    FillSimulator,
    FillSimulatorConfig,
)
from investment_team.trading_service.engine.order_book import OrderBook
from investment_team.trading_service.engine.portfolio import Portfolio
from investment_team.trading_service.modes.backtest import run_backtest
from investment_team.trading_service.service import TradingService
from investment_team.trading_service.strategy.contract import (
    Bar,
    OrderRequest,
    OrderSide,
    OrderType,
    TimeInForce,
)

# ---------------------------------------------------------------------------
# StrategySpec.risk_limits validation
# ---------------------------------------------------------------------------


def _base_spec(**overrides) -> StrategySpec:
    kwargs = dict(
        strategy_id="phase3",
        authored_by="test",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
    )
    kwargs.update(overrides)
    return StrategySpec(**kwargs)


def test_risk_limits_defaults_to_riskLimits_instance() -> None:
    spec = _base_spec()
    assert isinstance(spec.risk_limits, RiskLimits)
    assert spec.risk_limits.max_position_pct == RiskLimits().max_position_pct


def test_risk_limits_accepts_dict_and_normalizes() -> None:
    spec = _base_spec(
        risk_limits={"max_position_pct": 4, "max_gross_leverage": 2.0, "stop_loss_pct": 3}
    )
    assert isinstance(spec.risk_limits, RiskLimits)
    # Known keys survive.
    assert spec.risk_limits.max_position_pct == 4
    assert spec.risk_limits.max_gross_leverage == 2.0
    # Unknown LLM keys are silently dropped.
    assert not hasattr(spec.risk_limits, "stop_loss_pct")


def test_risk_limits_accepts_none() -> None:
    # The legacy API boundary may carry ``None`` from a persisted row that
    # was serialized before risk_limits existed.
    spec = _base_spec(risk_limits=None)
    assert isinstance(spec.risk_limits, RiskLimits)


def test_risk_limits_passes_through_explicit_instance() -> None:
    explicit = RiskLimits(max_position_pct=10, max_open_positions=3)
    spec = _base_spec(risk_limits=explicit)
    assert spec.risk_limits is explicit or spec.risk_limits == explicit
    assert spec.risk_limits.max_open_positions == 3


def test_spec_json_round_trips_through_model_dump() -> None:
    """Ensure persisted StrategySpec rows still deserialize after the schema change."""
    original = _base_spec(risk_limits={"max_position_pct": 7.5, "max_drawdown_pct": 12.0})
    payload = original.model_dump(mode="json")
    revived = StrategySpec.model_validate(payload)
    assert revived.risk_limits.max_position_pct == 7.5
    assert revived.risk_limits.max_drawdown_pct == 12.0


# ---------------------------------------------------------------------------
# TradingService risk_limits acceptance
# ---------------------------------------------------------------------------


def test_trading_service_accepts_risk_limits_instance() -> None:
    service = TradingService(
        strategy_code="from contract import Strategy\n\nclass S(Strategy):\n    pass\n",
        config=BacktestConfig(
            start_date="2024-01-01",
            end_date="2024-12-31",
            initial_capital=100_000.0,
        ),
        risk_limits=RiskLimits(max_position_pct=3, max_open_positions=2),
    )
    assert service._risk.limits.max_position_pct == 3
    assert service._risk.limits.max_open_positions == 2


def test_trading_service_accepts_legacy_dict() -> None:
    service = TradingService(
        strategy_code="from contract import Strategy\n\nclass S(Strategy):\n    pass\n",
        config=BacktestConfig(
            start_date="2024-01-01",
            end_date="2024-12-31",
            initial_capital=100_000.0,
        ),
        risk_limits={"max_position_pct": 3.5, "unknown_key": 99},
    )
    assert service._risk.limits.max_position_pct == 3.5


# ---------------------------------------------------------------------------
# Drawdown circuit-breaker → terminated_reason propagation
# ---------------------------------------------------------------------------


_BUY_AND_HOLD_CODE = textwrap.dedent('''\
    """Enter LONG immediately so a crash-mode price series blows past the
    max_drawdown limit on the very first bar after entry."""
    from contract import OrderSide, OrderType, Strategy


    class BuyAndHold(Strategy):
        def on_bar(self, ctx, bar):
            if ctx.position(bar.symbol) is None:
                ctx.submit_order(
                    symbol=bar.symbol,
                    side=OrderSide.LONG,
                    qty=500,
                    order_type=OrderType.MARKET,
                    reason="enter",
                )
''')


def _crash_bars(n: int) -> List[OHLCVBar]:
    """Flat first bar, then a sharp collapse over the next n-1 bars."""
    out: List[OHLCVBar] = []
    for i in range(n):
        # Start at 100, lose 15% per bar after bar 0.
        base = 100.0 * (0.85**i) if i > 0 else 100.0
        out.append(
            OHLCVBar(
                date=f"2024-01-{i + 1:02d}",
                open=base,
                high=base + 0.5,
                low=base - 0.5,
                close=base,
                volume=1_000_000.0,
            )
        )
    return out


def test_drawdown_breach_sets_terminated_reason_on_backtest_result() -> None:
    """A strategy that enters and rides a crash past max_drawdown_pct must
    record ``terminated_reason`` on the persisted ``BacktestResult``."""
    spec = _base_spec(
        strategy_id="dd-crash",
        strategy_code=_BUY_AND_HOLD_CODE,
        risk_limits={
            "max_drawdown_pct": 20.0,
            "max_position_pct": 80.0,
            "max_symbol_concentration_pct": 80.0,
        },
    )
    config = BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-12-31",
        initial_capital=100_000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
    )
    market_data = {"AAA": _crash_bars(10)}
    result = run_backtest(strategy=spec, config=config, market_data=market_data)

    assert result.service_result.terminated_reason is not None, (
        "service_result should carry the termination signal"
    )
    assert "max_drawdown" in result.service_result.terminated_reason

    assert result.result.terminated_reason is not None, (
        "BacktestResult must propagate terminated_reason from TradingServiceResult"
    )
    assert "max_drawdown" in result.result.terminated_reason


def test_clean_run_leaves_terminated_reason_none() -> None:
    """Runs that complete through end-of-stream must NOT populate the field."""
    from .golden.strategies import ROUND_TRIP_CODE

    spec = _base_spec(strategy_id="clean", strategy_code=ROUND_TRIP_CODE)
    config = BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-12-31",
        initial_capital=100_000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
    )
    bars = [
        OHLCVBar(
            date=f"2024-01-{i + 1:02d}",
            open=100.0 + i * 0.1,
            high=100.5 + i * 0.1,
            low=99.5 + i * 0.1,
            close=100.0 + i * 0.1,
            volume=1_000_000.0,
        )
        for i in range(20)
    ]
    result = run_backtest(strategy=spec, config=config, market_data={"AAA": bars})
    assert result.service_result.terminated_reason is None
    assert result.result.terminated_reason is None


# ---------------------------------------------------------------------------
# Risk-filter enforcement through the FillSimulator
# ---------------------------------------------------------------------------


def _bar(ts: str, price: float = 100.0) -> Bar:
    return Bar(
        symbol="AAA",
        timestamp=ts,
        timeframe="1d",
        open=price,
        high=price + 1,
        low=price - 1,
        close=price,
        volume=1000.0,
    )


def test_risk_filter_rejects_entry_when_max_open_positions_reached() -> None:
    portfolio = Portfolio(initial_capital=100_000.0)
    # Pretend one position is already open.
    from investment_team.trading_service.engine.portfolio import Position

    portfolio.open(
        Position(
            symbol="BBB",
            side=OrderSide.LONG,
            qty=10,
            entry_price=50.0,
            entry_bid_price=50.0,
            entry_timestamp="2024-01-01",
            entry_order_id="pre-seed",
            entry_client_order_id="pre-seed-c",
        )
    )
    order_book = OrderBook()
    sim = FillSimulator(
        portfolio=portfolio,
        order_book=order_book,
        risk_filter=RiskFilter(RiskLimits(max_open_positions=1)),
        config=FillSimulatorConfig(slippage_bps=0.0, transaction_cost_bps=0.0),
        bar_safety=BarSafetyAssertion(enabled=False),
    )
    order_book.submit(
        OrderRequest(
            client_order_id="c1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=1,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
        ),
        submitted_at="2024-01-01",
        submitted_equity=100_000.0,
    )

    outcome = sim.process_bar(_bar("2024-01-02"))
    assert len(outcome.entry_fills) == 0, "max_open_positions should reject the entry"


def test_risk_filter_rejects_entry_above_gross_leverage() -> None:
    portfolio = Portfolio(initial_capital=100_000.0)
    order_book = OrderBook()
    sim = FillSimulator(
        portfolio=portfolio,
        order_book=order_book,
        # Very tight leverage cap: any order above 50% notional fails.
        risk_filter=RiskFilter(RiskLimits(max_gross_leverage=0.5, max_position_pct=100)),
        config=FillSimulatorConfig(slippage_bps=0.0, transaction_cost_bps=0.0),
        bar_safety=BarSafetyAssertion(enabled=False),
    )
    # 1000 shares * $100 = $100k = 100% of equity > 50% cap.
    order_book.submit(
        OrderRequest(
            client_order_id="c1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=1000,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
        ),
        submitted_at="2024-01-01",
        submitted_equity=100_000.0,
    )

    outcome = sim.process_bar(_bar("2024-01-02"))
    assert len(outcome.entry_fills) == 0, "gross leverage cap should reject the entry"


def test_risk_filter_allows_entry_under_all_limits() -> None:
    portfolio = Portfolio(initial_capital=100_000.0)
    order_book = OrderBook()
    sim = FillSimulator(
        portfolio=portfolio,
        order_book=order_book,
        risk_filter=RiskFilter(RiskLimits()),
        config=FillSimulatorConfig(slippage_bps=0.0, transaction_cost_bps=0.0),
        bar_safety=BarSafetyAssertion(enabled=False),
    )
    order_book.submit(
        OrderRequest(
            client_order_id="c1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=5,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
        ),
        submitted_at="2024-01-01",
        submitted_equity=100_000.0,
    )

    outcome = sim.process_bar(_bar("2024-01-02"))
    assert len(outcome.entry_fills) == 1


# ---------------------------------------------------------------------------
# End-to-end — default spec still runs unchanged under the typed schema
# ---------------------------------------------------------------------------


def test_default_risk_limits_spec_executes_cleanly() -> None:
    """Regression check: a StrategySpec with no explicit risk_limits must
    still drive a TradingService run to completion using defaults."""
    from .golden.strategies import ROUND_TRIP_CODE

    spec = _base_spec(strategy_id="default-rl", strategy_code=ROUND_TRIP_CODE)
    config = BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-12-31",
        initial_capital=100_000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
    )
    bars = [
        OHLCVBar(
            date=f"2024-01-{i + 1:02d}",
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=1_000_000.0,
        )
        for i in range(20)
    ]
    stream = HistoricalReplayStream({"AAA": bars}, timeframe="1d")
    service = TradingService(
        strategy_code=spec.strategy_code,
        config=config,
        risk_limits=spec.risk_limits,
    )
    outcome = service.run(stream)
    assert outcome.error is None
    assert not outcome.lookahead_violation


@pytest.mark.parametrize(
    "legacy_dict",
    [
        {"max_position_pct": 5, "stop_loss_pct": 3},
        {"max_position_pct": 5, "max_drawdown_pct": 20.0},
        {},
    ],
)
def test_strategy_spec_round_trips_common_llm_payloads(legacy_dict: dict) -> None:
    """Sanity: every risk_limits payload the ideation prompt asks for parses cleanly."""
    spec = _base_spec(risk_limits=legacy_dict)
    assert isinstance(spec.risk_limits, RiskLimits)
