"""Tests for the Phase 3 RiskFilter: sizing, entry gates, drawdown breaker."""

from __future__ import annotations

import pytest

from investment_team.execution.risk_filter import RiskFilter, RiskLimits
from investment_team.trade_simulator import OpenPosition, TradeSimulationEngine

from .golden.deterministic_strategies import mean_reversion, sma_crossover
from .golden.synthetic_data import build_fixture_universe

# ---------------------------------------------------------------------------
# RiskLimits schema
# ---------------------------------------------------------------------------


def test_risk_limits_defaults():
    rl = RiskLimits()
    assert rl.max_position_pct == 6.0
    assert rl.max_gross_leverage == 1.0
    assert rl.max_drawdown_pct == 25.0
    assert rl.max_open_positions == 10


def test_risk_limits_from_legacy_dict_ignores_unknown_keys():
    raw = {"max_position_pct": 8.0, "unknown_key": 42}
    rl = RiskLimits.from_legacy_dict(raw)
    assert rl.max_position_pct == 8.0
    assert rl.max_gross_leverage == 1.0


def test_risk_limits_from_empty_dict_matches_defaults():
    assert RiskLimits.from_legacy_dict({}) == RiskLimits()


# ---------------------------------------------------------------------------
# RiskFilter.size()
# ---------------------------------------------------------------------------


def test_size_flat_percentage():
    rf = RiskFilter(RiskLimits(max_position_pct=10.0))
    result = rf.size(price=50.0, equity=100_000.0, recent_closes=[])
    assert result.shares == pytest.approx(200.0, abs=1)  # 10k / 50


def test_size_vol_targeted_reduces_when_volatile():
    closes = [100 + (i % 10) * (1 if i % 2 == 0 else -1) for i in range(30)]
    rf = RiskFilter(RiskLimits(max_position_pct=10.0, target_annual_vol=0.10))
    result = rf.size(price=100.0, equity=100_000.0, recent_closes=closes)
    assert result.shares > 0
    assert result.shares < 100.0  # vol-scaling should reduce below flat 10% = 100


def test_size_returns_zero_for_nonpositive_price():
    rf = RiskFilter(RiskLimits())
    assert rf.size(price=0.0, equity=100_000.0, recent_closes=[]).shares == 0.0


# ---------------------------------------------------------------------------
# RiskFilter.can_enter()
# ---------------------------------------------------------------------------


def test_can_enter_blocks_on_max_open_positions():
    rf = RiskFilter(RiskLimits(max_open_positions=2))
    positions = {
        "A": OpenPosition(
            symbol="A", side="long", entry_date="", entry_price=100, shares=10, position_value=1000
        ),
        "B": OpenPosition(
            symbol="B", side="long", entry_date="", entry_price=50, shares=20, position_value=1000
        ),
    }
    result = rf.can_enter("C", 1000.0, 100_000.0, positions)
    assert not result.allowed
    assert "max_open_positions" in result.reason


def test_can_enter_blocks_on_leverage():
    rf = RiskFilter(RiskLimits(max_gross_leverage=1.0))
    positions = {
        "A": OpenPosition(
            symbol="A",
            side="long",
            entry_date="",
            entry_price=100,
            shares=10,
            position_value=80_000,
        ),
    }
    result = rf.can_enter("B", 30_000.0, 100_000.0, positions)
    assert not result.allowed
    assert "leverage" in result.reason


def test_can_enter_blocks_on_concentration():
    rf = RiskFilter(RiskLimits(max_symbol_concentration_pct=10.0))
    result = rf.can_enter("A", 20_000.0, 100_000.0, {})
    assert not result.allowed
    assert "concentration" in result.reason


def test_can_enter_allows_within_limits():
    rf = RiskFilter(RiskLimits())
    result = rf.can_enter("A", 5_000.0, 100_000.0, {})
    assert result.allowed


# ---------------------------------------------------------------------------
# RiskFilter.check_drawdown()
# ---------------------------------------------------------------------------


def test_drawdown_breaches_on_limit():
    rf = RiskFilter(RiskLimits(max_drawdown_pct=20.0))
    result = rf.check_drawdown(current_equity=78_000.0, peak_equity=100_000.0)
    assert result.breached
    assert result.current_drawdown_pct == pytest.approx(22.0)


def test_drawdown_not_breached():
    rf = RiskFilter(RiskLimits(max_drawdown_pct=20.0))
    result = rf.check_drawdown(current_equity=90_000.0, peak_equity=100_000.0)
    assert not result.breached


# ---------------------------------------------------------------------------
# Integration: engine stops on drawdown breach
# ---------------------------------------------------------------------------


def test_engine_terminates_on_drawdown_breach():
    """Force a tight max drawdown (0.5%) on a losing strategy.

    Mean-reversion on the fixture universe loses ~8%, so a 0.5% DD limit
    guarantees the breaker fires after the first losing trade closes.
    """
    engine = TradeSimulationEngine(
        initial_capital=100_000.0,
        pre_filter_pct=0.0,
        max_evaluations=100_000,
        lookahead_safe=True,
        risk_limits={"max_drawdown_pct": 0.5},
    )
    result = engine.run(build_fixture_universe(), mean_reversion())
    assert result.terminated_reason is not None
    assert "max_drawdown" in result.terminated_reason


def test_engine_passes_risk_limits_through():
    """With default limits, the engine should still produce trades."""
    engine = TradeSimulationEngine(
        initial_capital=100_000.0,
        pre_filter_pct=0.0,
        max_evaluations=100_000,
        lookahead_safe=True,
        risk_limits={},
    )
    result = engine.run(build_fixture_universe(), sma_crossover())
    assert len(result.trades) > 0


def test_drawdown_uses_mtm_not_frozen_position_value():
    """The circuit-breaker must mark open positions to market.

    We use a strategy that enters and never exits (``always_long``). With a
    tight DD limit the breaker should fire from the unrealized loss on the
    open position — not wait for an exit trade to realize the loss.
    """
    from investment_team.market_data_service import OHLCVBar

    # Stable price for 5 bars (satisfies min_history_bars), then a 20% gap down.
    bars = [
        OHLCVBar(date="2023-01-02", open=100, high=101, low=99, close=100, volume=1e6),
        OHLCVBar(date="2023-01-03", open=100, high=101, low=99, close=100, volume=1e6),
        OHLCVBar(date="2023-01-04", open=100, high=101, low=99, close=100, volume=1e6),
        OHLCVBar(date="2023-01-05", open=100, high=101, low=99, close=100, volume=1e6),
        OHLCVBar(date="2023-01-06", open=100, high=101, low=99, close=100, volume=1e6),
        # Entry decision made on bar 5 → fills on bar 6's open (100)
        OHLCVBar(date="2023-01-09", open=100, high=100, low=99, close=100, volume=1e6),
        # Gap down: close drops to 80 → 20% unrealized loss on long
        OHLCVBar(date="2023-01-10", open=80, high=82, low=78, close=80, volume=1e6),
        OHLCVBar(date="2023-01-11", open=78, high=80, low=75, close=75, volume=1e6),
    ]
    market_data = {"GAP": bars}

    def always_long(symbol, bar, recent, position, capital):
        if position is None:
            return {"action": "enter_long", "confidence": 1.0, "shares": 0, "reasoning": ""}
        return {"action": "hold", "confidence": 0.0, "shares": 0, "reasoning": ""}

    engine = TradeSimulationEngine(
        initial_capital=100_000.0,
        pre_filter_pct=0.0,
        max_evaluations=100_000,
        lookahead_safe=True,
        risk_limits={
            "max_drawdown_pct": 5.0,
            "max_position_pct": 50.0,
            "max_symbol_concentration_pct": 100.0,
        },
    )
    result = engine.run(market_data, always_long)
    assert result.terminated_reason is not None
    assert "max_drawdown" in result.terminated_reason


def test_drawdown_evaluated_after_all_symbols_for_date():
    """Drawdown must use a same-date cross-section of all symbol prices.

    Two symbols with equal-weight positions: A gaps down 8% and B gaps up
    8% on the same date. Net equity is roughly flat. A tight 5% DD limit
    must NOT fire — but it would if drawdown were checked after processing
    A's bar alone (before B's price update).
    """
    from investment_team.market_data_service import OHLCVBar

    def _bars(symbol, prices):
        bars = []
        prev = prices[0][1]
        for i, (d, p) in enumerate(prices):
            bars.append(
                OHLCVBar(
                    date=d, open=prev, high=max(p, prev), low=min(p, prev), close=p, volume=1e6
                )
            )
            prev = p
        return bars

    dates = [
        "2023-01-02",
        "2023-01-03",
        "2023-01-04",
        "2023-01-05",
        "2023-01-06",
        "2023-01-09",
        "2023-01-10",
    ]
    # Both symbols stable for 5 bars, then A drops 8% and B rises 8%.
    a_prices = [(d, 100.0) for d in dates[:6]] + [(dates[6], 92.0)]
    b_prices = [(d, 100.0) for d in dates[:6]] + [(dates[6], 108.0)]

    market_data = {"A": _bars("A", a_prices), "B": _bars("B", b_prices)}

    def always_long(symbol, bar, recent, position, capital):
        if position is None:
            return {"action": "enter_long", "confidence": 1.0, "shares": 0, "reasoning": ""}
        return {"action": "hold", "confidence": 0.0, "shares": 0, "reasoning": ""}

    engine = TradeSimulationEngine(
        initial_capital=100_000.0,
        pre_filter_pct=0.0,
        max_evaluations=100_000,
        lookahead_safe=True,
        risk_limits={
            "max_drawdown_pct": 5.0,
            "max_position_pct": 40.0,
            "max_symbol_concentration_pct": 100.0,
        },
    )
    result = engine.run(market_data, always_long)
    assert result.terminated_reason is None, (
        f"False termination: {result.terminated_reason}. "
        "Drawdown was evaluated on a partial-date snapshot."
    )
