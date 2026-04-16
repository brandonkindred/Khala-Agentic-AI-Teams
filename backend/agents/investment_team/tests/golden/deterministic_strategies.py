"""Pure-Python evaluate callbacks used by the golden regression tests.

The simulator accepts an ``evaluate_fn(symbol, bar, recent, position, capital)``
— by supplying a deterministic callback we can lock metric output without
touching the LLM path.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from investment_team.market_data_service import OHLCVBar
from investment_team.trade_simulator import OpenPosition


def sma_crossover(window: int = 10):
    """Enter long when close > SMA(window); exit when close < SMA(window)."""

    def _evaluate(
        symbol: str,
        bar: OHLCVBar,
        recent: List[OHLCVBar],
        position: Optional[OpenPosition],
        capital: float,
    ) -> Dict[str, Any]:
        if len(recent) < window:
            return {"action": "hold", "confidence": 0.0, "shares": 0, "reasoning": "warmup"}
        sma = sum(b.close for b in recent[-window:]) / window
        close = bar.close
        if position is None and close > sma * 1.01:
            return {
                "action": "enter_long",
                "confidence": 0.7,
                "shares": 0,
                "reasoning": f"close {close:.2f} > sma {sma:.2f}",
            }
        if position is not None and close < sma * 0.99:
            return {
                "action": "exit",
                "confidence": 0.7,
                "shares": 0,
                "reasoning": f"close {close:.2f} < sma {sma:.2f}",
            }
        return {"action": "hold", "confidence": 0.0, "shares": 0, "reasoning": ""}

    return _evaluate


def mean_reversion(lookback: int = 10, enter_z: float = 1.0, exit_z: float = 0.2):
    """Enter long when close is enter_z std below mean; exit when within exit_z."""

    def _evaluate(
        symbol: str,
        bar: OHLCVBar,
        recent: List[OHLCVBar],
        position: Optional[OpenPosition],
        capital: float,
    ) -> Dict[str, Any]:
        if len(recent) < lookback:
            return {"action": "hold", "confidence": 0.0, "shares": 0, "reasoning": "warmup"}
        closes = [b.close for b in recent[-lookback:]]
        mean = sum(closes) / len(closes)
        var = sum((c - mean) ** 2 for c in closes) / max(1, len(closes) - 1)
        sd = var**0.5
        if sd <= 0:
            return {"action": "hold", "confidence": 0.0, "shares": 0, "reasoning": "no vol"}
        z = (bar.close - mean) / sd
        if position is None and z < -enter_z:
            return {
                "action": "enter_long",
                "confidence": 0.8,
                "shares": 0,
                "reasoning": f"z={z:.2f}",
            }
        if position is not None and z > -exit_z:
            return {
                "action": "exit",
                "confidence": 0.8,
                "shares": 0,
                "reasoning": f"z={z:.2f}",
            }
        return {"action": "hold", "confidence": 0.0, "shares": 0, "reasoning": ""}

    return _evaluate
