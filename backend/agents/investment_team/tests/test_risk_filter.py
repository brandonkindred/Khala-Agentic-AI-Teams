"""Tests for the Phase 3 RiskFilter: sizing, entry gates, drawdown breaker."""

from __future__ import annotations

import pytest

from investment_team.execution.risk_filter import RiskFilter, RiskLimits
from investment_team.trade_simulator import OpenPosition

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
