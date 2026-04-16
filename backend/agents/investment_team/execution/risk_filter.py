"""Position-level and portfolio-level risk enforcement (Phase 3).

``RiskLimits`` formalizes the schema that ``StrategySpec.risk_limits`` was
carrying as an unvalidated ``Dict[str, Any]``.  ``RiskFilter`` consumes it at
simulation runtime to:

- vol-target position sizing (replaces the hard-coded ``position_pct = 0.06``),
- enforce per-symbol concentration, gross leverage, and max-open-position caps,
- circuit-break the run when trailing drawdown breaches ``max_drawdown_pct``.

Both the look-ahead-safe engine (Phase 2) and the legacy engine invoke the
filter through the same ``size()`` / ``can_enter()`` / ``check_drawdown()``
methods, so risk limits are tested identically in backtest and live modes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence

from pydantic import BaseModel, Field


class RiskLimits(BaseModel):
    """Validated risk-limits schema.

    Default values are conservative — equivalent to the pre-Phase-3 behavior
    (6% fixed sizing, no caps).  Migration helper ``from_legacy_dict`` injects
    these defaults when reading a ``StrategySpec.risk_limits`` dict that was
    serialized before this schema existed.
    """

    max_gross_leverage: float = Field(default=1.0, ge=0)
    max_position_pct: float = Field(default=6.0, ge=0, le=100)
    max_symbol_concentration_pct: float = Field(default=20.0, ge=0, le=100)
    max_drawdown_pct: float = Field(default=25.0, ge=0, le=100)
    max_open_positions: int = Field(default=10, ge=1)
    target_annual_vol: Optional[float] = Field(
        default=None,
        ge=0,
        description=(
            "When set, position sizing is vol-targeted: "
            "shares = (target_vol / realized_vol_20d) * equity * max_position_pct / 100 / price. "
            "When None, falls back to a flat ``max_position_pct`` fraction."
        ),
    )
    vol_lookback_days: int = Field(default=20, ge=2)

    @classmethod
    def from_legacy_dict(cls, raw: Dict[str, Any]) -> "RiskLimits":
        """Upgrade a raw ``StrategySpec.risk_limits`` dict into the new schema.

        Unknown keys are silently ignored so old specs don't break.
        """
        known_fields = set(cls.model_fields)
        filtered = {k: v for k, v in raw.items() if k in known_fields}
        return cls(**filtered)


@dataclass
class SizingDecision:
    shares: float
    reason: str


@dataclass
class EntryDecision:
    allowed: bool
    reason: str


@dataclass
class DrawdownBreach:
    breached: bool
    current_drawdown_pct: float
    limit_pct: float


class RiskFilter:
    """Stateless risk-limit enforcer consumed by the simulation engine."""

    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits

    # ------------------------------------------------------------------
    # Position sizing
    # ------------------------------------------------------------------

    def size(
        self,
        price: float,
        equity: float,
        recent_closes: Sequence[float],
    ) -> SizingDecision:
        """Compute the number of shares for a new position.

        Uses vol-targeted sizing when ``limits.target_annual_vol`` is set,
        otherwise falls back to a flat ``max_position_pct`` fraction of equity.
        """
        if price <= 0 or equity <= 0:
            return SizingDecision(shares=0.0, reason="non-positive price or equity")

        max_notional = equity * self.limits.max_position_pct / 100.0

        if (
            self.limits.target_annual_vol is not None
            and len(recent_closes) >= self.limits.vol_lookback_days
        ):
            realized_vol = self._realized_vol(recent_closes, self.limits.vol_lookback_days)
            vol_floor = 0.01
            scale = self.limits.target_annual_vol / max(realized_vol, vol_floor)
            notional = min(max_notional * scale, max_notional * 3)
        else:
            notional = max_notional

        notional = min(notional, equity)
        shares = notional / price
        dp = 4 if price < 10 else 2
        shares = round(shares, dp)
        return SizingDecision(
            shares=shares,
            reason=(
                f"vol-target={self.limits.target_annual_vol}"
                if self.limits.target_annual_vol
                else f"flat pct={self.limits.max_position_pct}"
            ),
        )

    # ------------------------------------------------------------------
    # Pre-entry gate
    # ------------------------------------------------------------------

    def can_enter(
        self,
        symbol: str,
        notional: float,
        current_equity: float,
        open_positions: Dict[str, Any],
    ) -> EntryDecision:
        """Check whether opening a new position would breach any limit."""
        if len(open_positions) >= self.limits.max_open_positions:
            return EntryDecision(
                allowed=False,
                reason=f"max_open_positions ({self.limits.max_open_positions}) reached",
            )

        total_notional = (
            sum(getattr(p, "position_value", 0) for p in open_positions.values()) + notional
        )

        if current_equity > 0:
            leverage = total_notional / current_equity
            if leverage > self.limits.max_gross_leverage:
                return EntryDecision(
                    allowed=False,
                    reason=f"gross leverage {leverage:.2f} > limit {self.limits.max_gross_leverage}",
                )

            concentration = notional / current_equity * 100
            if concentration > self.limits.max_symbol_concentration_pct:
                return EntryDecision(
                    allowed=False,
                    reason=(
                        f"symbol concentration {concentration:.1f}% > "
                        f"limit {self.limits.max_symbol_concentration_pct}%"
                    ),
                )

        return EntryDecision(allowed=True, reason="within limits")

    # ------------------------------------------------------------------
    # Drawdown circuit-breaker
    # ------------------------------------------------------------------

    def check_drawdown(
        self,
        current_equity: float,
        peak_equity: float,
    ) -> DrawdownBreach:
        """Return whether trailing drawdown breaches the configured limit."""
        if peak_equity <= 0:
            return DrawdownBreach(
                breached=False, current_drawdown_pct=0.0, limit_pct=self.limits.max_drawdown_pct
            )

        dd_pct = (peak_equity - current_equity) / peak_equity * 100.0
        return DrawdownBreach(
            breached=dd_pct >= self.limits.max_drawdown_pct,
            current_drawdown_pct=round(dd_pct, 2),
            limit_pct=self.limits.max_drawdown_pct,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _realized_vol(closes: Sequence[float], lookback: int) -> float:
        """Annualized realized volatility from daily closes (close-to-close)."""
        window = list(closes[-lookback:])
        if len(window) < 2:
            return 0.0
        log_returns = []
        for i in range(1, len(window)):
            if window[i - 1] > 0 and window[i] > 0:
                log_returns.append(math.log(window[i] / window[i - 1]))
        if len(log_returns) < 2:
            return 0.0
        mean = sum(log_returns) / len(log_returns)
        var = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
        return math.sqrt(var * 252)
