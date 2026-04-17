"""Transaction cost models (Phase 4).

Three implementations behind a common ``CostModel`` interface:

- ``FlatBpsCostModel`` — current behavior (retained for unit tests and as a
  fallback).
- ``SpreadPlusImpactCostModel`` — half-spread + non-linear market-impact
  term scaled by ``order_notional / ADV``, plus a venue-fee floor.
- ``MakerTakerCostModel`` — for limit-order strategies; applies a rebate on
  the entry side and a taker fee on the exit.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class CostEstimate:
    entry_cost_bps: float
    exit_cost_bps: float
    round_trip_cost_bps: float
    model_name: str


class CostModel(ABC):
    @abstractmethod
    def estimate(
        self,
        *,
        symbol: str,
        asset_class: str,
        order_notional: float,
        avg_daily_volume_usd: Optional[float] = None,
    ) -> CostEstimate: ...


# ---------------------------------------------------------------------------
# 1. Flat BPS (legacy)
# ---------------------------------------------------------------------------


class FlatBpsCostModel(CostModel):
    """Applies a symmetric flat fee + slippage on both legs."""

    def __init__(self, transaction_cost_bps: float = 5.0, slippage_bps: float = 2.0):
        self.tx_bps = transaction_cost_bps
        self.slip_bps = slippage_bps

    def estimate(
        self,
        *,
        symbol: str,
        asset_class: str,
        order_notional: float,
        avg_daily_volume_usd: Optional[float] = None,
    ) -> CostEstimate:
        per_leg = self.tx_bps + self.slip_bps
        return CostEstimate(
            entry_cost_bps=per_leg,
            exit_cost_bps=per_leg,
            round_trip_cost_bps=per_leg * 2,
            model_name="flat_bps",
        )


# ---------------------------------------------------------------------------
# 2. Spread + Impact (recommended default)
# ---------------------------------------------------------------------------

_DEFAULT_HALF_SPREAD: Dict[str, float] = {
    "stocks": 1.0,
    "crypto": 5.0,
    "forex": 2.0,
    "futures": 2.0,
    "commodities": 3.0,
    "options": 5.0,
}

_DEFAULT_VENUE_FEE: Dict[str, float] = {
    "stocks": 0.5,
    "crypto": 10.0,
    "forex": 1.0,
    "futures": 2.0,
    "commodities": 2.0,
    "options": 3.0,
}


class SpreadPlusImpactCostModel(CostModel):
    """``cost_bps = half_spread + k * (notional / ADV)^impact_exponent + venue_fee``.

    When ``avg_daily_volume_usd`` is not available, falls back to the flat
    half-spread + venue-fee without the impact term.
    """

    def __init__(
        self,
        *,
        impact_coefficient: float = 50.0,
        impact_exponent: float = 0.6,
        half_spread_overrides: Optional[Dict[str, float]] = None,
        venue_fee_overrides: Optional[Dict[str, float]] = None,
    ):
        self.k = impact_coefficient
        self.exp = impact_exponent
        self._half_spread = {**_DEFAULT_HALF_SPREAD, **(half_spread_overrides or {})}
        self._venue_fee = {**_DEFAULT_VENUE_FEE, **(venue_fee_overrides or {})}

    def estimate(
        self,
        *,
        symbol: str,
        asset_class: str,
        order_notional: float,
        avg_daily_volume_usd: Optional[float] = None,
    ) -> CostEstimate:
        ac = asset_class.lower()
        half_spread = self._half_spread.get(ac, 2.0)
        venue_fee = self._venue_fee.get(ac, 2.0)

        if avg_daily_volume_usd and avg_daily_volume_usd > 0 and order_notional > 0:
            ratio = order_notional / avg_daily_volume_usd
            impact = self.k * math.pow(ratio, self.exp)
        else:
            impact = 0.0

        per_leg = half_spread + impact + venue_fee
        return CostEstimate(
            entry_cost_bps=round(per_leg, 2),
            exit_cost_bps=round(per_leg, 2),
            round_trip_cost_bps=round(per_leg * 2, 2),
            model_name="spread_plus_impact",
        )


# ---------------------------------------------------------------------------
# 3. Maker / Taker (for limit-order strategies)
# ---------------------------------------------------------------------------


class MakerTakerCostModel(CostModel):
    """Rebate on the maker (limit) side, fee on the taker (market) side."""

    def __init__(self, maker_rebate_bps: float = 1.0, taker_fee_bps: float = 3.0):
        self.maker_rebate = maker_rebate_bps
        self.taker_fee = taker_fee_bps

    def estimate(
        self,
        *,
        symbol: str,
        asset_class: str,
        order_notional: float,
        avg_daily_volume_usd: Optional[float] = None,
    ) -> CostEstimate:
        return CostEstimate(
            entry_cost_bps=-self.maker_rebate,
            exit_cost_bps=self.taker_fee,
            round_trip_cost_bps=round(self.taker_fee - self.maker_rebate, 2),
            model_name="maker_taker",
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_cost_model(
    model_name: str = "spread_plus_impact",
    **kwargs,
) -> CostModel:
    """Build a cost model by name."""
    if model_name == "flat_bps":
        return FlatBpsCostModel(**kwargs)
    if model_name == "maker_taker":
        return MakerTakerCostModel(**kwargs)
    return SpreadPlusImpactCostModel(**kwargs)
