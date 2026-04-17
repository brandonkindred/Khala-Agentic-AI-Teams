"""Tests for the Phase 4 cost models."""

from __future__ import annotations

import pytest

from investment_team.execution.cost_model import (
    FlatBpsCostModel,
    MakerTakerCostModel,
    SpreadPlusImpactCostModel,
    build_cost_model,
)

# ---------------------------------------------------------------------------
# FlatBpsCostModel
# ---------------------------------------------------------------------------


def test_flat_bps_symmetric():
    cm = FlatBpsCostModel(transaction_cost_bps=5.0, slippage_bps=2.0)
    e = cm.estimate(symbol="SPY", asset_class="stocks", order_notional=10_000)
    assert e.entry_cost_bps == 7.0
    assert e.exit_cost_bps == 7.0
    assert e.round_trip_cost_bps == 14.0


def test_flat_bps_ignores_volume():
    cm = FlatBpsCostModel()
    e1 = cm.estimate(symbol="X", asset_class="stocks", order_notional=100, avg_daily_volume_usd=1e9)
    e2 = cm.estimate(symbol="X", asset_class="stocks", order_notional=1e8, avg_daily_volume_usd=1e9)
    assert e1.round_trip_cost_bps == e2.round_trip_cost_bps


# ---------------------------------------------------------------------------
# SpreadPlusImpactCostModel
# ---------------------------------------------------------------------------


def test_impact_increases_with_order_size():
    cm = SpreadPlusImpactCostModel()
    small = cm.estimate(
        symbol="AAPL", asset_class="stocks", order_notional=1_000, avg_daily_volume_usd=1e9
    )
    large = cm.estimate(
        symbol="AAPL", asset_class="stocks", order_notional=1e8, avg_daily_volume_usd=1e9
    )
    assert large.round_trip_cost_bps > small.round_trip_cost_bps


def test_impact_zero_without_adv():
    cm = SpreadPlusImpactCostModel()
    e = cm.estimate(symbol="X", asset_class="stocks", order_notional=10_000)
    # Should just be half_spread + venue_fee, no impact term
    assert e.entry_cost_bps == pytest.approx(1.0 + 0.5)  # stocks defaults


def test_crypto_higher_than_stocks():
    cm = SpreadPlusImpactCostModel()
    crypto = cm.estimate(symbol="BTC", asset_class="crypto", order_notional=10_000)
    stocks = cm.estimate(symbol="SPY", asset_class="stocks", order_notional=10_000)
    assert crypto.round_trip_cost_bps > stocks.round_trip_cost_bps


def test_small_adv_crypto_produces_large_impact():
    cm = SpreadPlusImpactCostModel()
    e = cm.estimate(
        symbol="SHIB", asset_class="crypto", order_notional=50_000, avg_daily_volume_usd=100_000
    )
    assert e.round_trip_cost_bps > 50


# ---------------------------------------------------------------------------
# MakerTakerCostModel
# ---------------------------------------------------------------------------


def test_maker_taker_rebate():
    cm = MakerTakerCostModel(maker_rebate_bps=2.0, taker_fee_bps=5.0)
    e = cm.estimate(symbol="X", asset_class="stocks", order_notional=10_000)
    assert e.entry_cost_bps == -2.0  # rebate
    assert e.exit_cost_bps == 5.0
    assert e.round_trip_cost_bps == 3.0


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_build_cost_model_factory():
    assert isinstance(build_cost_model("flat_bps"), FlatBpsCostModel)
    assert isinstance(build_cost_model("maker_taker"), MakerTakerCostModel)
    assert isinstance(build_cost_model("spread_plus_impact"), SpreadPlusImpactCostModel)
    assert isinstance(build_cost_model(), SpreadPlusImpactCostModel)
