"""Position-level and portfolio-level risk enforcement (Phase 3).

``RiskLimits`` formalizes the schema that ``StrategySpec.risk_limits`` was
carrying as an unvalidated ``Dict[str, Any]``.  ``RiskFilter`` consumes it at
simulation runtime to:

- vol-target position sizing (replaces the hard-coded ``position_pct = 0.06``),
- enforce per-symbol concentration, gross leverage, and max-open-position caps.

There is intentionally **no** drawdown circuit-breaker. Strategy Lab runs are
experiments (backtest / paper trading, no real capital), and a strategy must be
free to lose up to 100% of the account so its true downside is observed rather
than truncated by an arbitrary trailing-loss limit. Realised drawdown is still
*measured* and reported as a performance metric — it is just never a constraint
that halts a run.

Both the look-ahead-safe engine (Phase 2) and the legacy engine invoke the
filter through the same ``size()`` / ``can_enter()`` methods, so risk limits are
tested identically in backtest and live modes.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class RiskLimits(BaseModel):
    """Validated risk-limits schema.

    Default values are conservative — equivalent to the pre-Phase-3 behavior
    (6% fixed sizing, no caps).  Migration helper ``from_legacy_dict`` injects
    these defaults when reading a ``StrategySpec.risk_limits`` dict that was
    serialized before this schema existed.
    """

    max_gross_leverage: float = Field(default=1.0, ge=0)
    max_position_pct: float = Field(
        default=6.0,
        ge=0,
        le=100,
        description=(
            "Maximum capital deployed on a single position as a % of the account. "
            "This is also the most a single trade can lose, because an entered "
            "position can lose up to ~100% of the capital deployed — so the deployed "
            "size IS the per-trade loss budget. ``stop_loss.pct`` is a separate, "
            "optional within-position safeguard (a price move off entry, measured "
            "against the trade) that limits a position's realised loss below a full "
            "wipeout; it is decoupled from sizing and must never be multiplied into "
            "this cap. A short with no effective stop is auto-protected at runtime "
            "with a 100%-adverse-move stop so its modeled worst-case loss is also "
            "bounded by the deployed size."
        ),
    )
    max_symbol_concentration_pct: float = Field(default=20.0, ge=0, le=100)
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

        Unknown keys are silently ignored so old specs don't break. The retired
        ``max_loss_per_trade_pct`` is one such key: it is dropped, and the authored
        ``max_position_pct`` (the deployed-capital cap, which is itself the most a
        single trade can lose) stands. The two are NOT folded together — under the
        old model ``max_loss_per_trade_pct`` was a different quantity (a
        stop-governed realised-loss tolerance, ``fraction × stop``), so importing
        it as a deployed-capital cap would wrongly shrink the position (and a
        legacy ``0``/negative/None value would either silently zero or fail to
        validate the cap). A legacy spec that set ONLY the retired key (no explicit
        ``max_position_pct``) therefore migrates to the default ``max_position_pct``
        (6%); the stop, if any, remains as a decoupled within-position safeguard.
        A WARN is logged whenever the retired key is present (once per migration
        call, not process-deduplicated) so operators loading old specs can confirm
        the migrated cap is intended.

        Preconditions: ``raw`` is a mapping of risk-limit field names to values.
        Postconditions: returns a validated ``RiskLimits``; unknown/retired keys
        are ignored, never folded into another field.
        """
        known_fields = set(cls.model_fields)
        filtered = {k: v for k, v in raw.items() if k in known_fields}
        if "max_loss_per_trade_pct" in raw:
            logger.warning(
                "Dropping retired risk-limit 'max_loss_per_trade_pct'=%r; it is no "
                "longer a separate field. max_position_pct=%r is the deployed-capital "
                "cap (and the per-trade loss cap); stop_loss.pct is a decoupled "
                "within-position safeguard. Verify the migrated cap is intended.",
                raw.get("max_loss_per_trade_pct"),
                filtered.get("max_position_pct", cls.model_fields["max_position_pct"].default),
            )
        return cls(**filtered)


# Per-field tighten-direction map for the refinement carve-out (#543).
# "lower"  → proposed value must be ``<= current`` to count as tightening.
# "higher" → proposed value must be ``>= current`` to count as tightening.
# None     → field is immutable from refinement; any change is discarded
#            with a warning but does NOT raise ``SpecImplementabilityError``
#            (cosmetic knobs only).
#
# ``target_annual_vol`` is "lower" because lowering the vol target shrinks
# per-position size. ``None → value`` transitions fundamentally change the
# sizing model and are treated as loosening (raises rather than mutates).
_RISK_LIMIT_TIGHTEN_DIRECTION: Dict[str, Optional[str]] = {
    "max_gross_leverage": "lower",
    "max_position_pct": "lower",
    "max_symbol_concentration_pct": "lower",
    "max_open_positions": "lower",
    "target_annual_vol": "lower",
    "vol_lookback_days": None,
}


@dataclass
class SizingDecision:
    shares: float
    reason: str


@dataclass
class EntryDecision:
    allowed: bool
    reason: str


#: Relative epsilon absorbing float noise so an order clamped exactly to
#: ``max_position_pct`` is not rejected by the choke-point gate.
_POSITION_CAP_REL_EPS = 1e-9


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
        *,
        enforce_position_cap: bool = True,
    ) -> EntryDecision:
        """Check whether opening a new position would breach any limit.

        ``enforce_position_cap`` controls the ``max_position_pct`` check only.
        It is ``False`` for orders the engine dispatcher already clamped to the
        cap at the sizing price (``OrderRequest.risk_presized``): re-checking
        those at the fill price would falsely reject an order the dispatcher
        sized correctly whenever the fill gaps above the sizing price (the cap is
        a sizing-time bound, and post-fill notional drift on already-committed
        shares is normal holding behaviour — there is no drawdown backstop, by
        design). It is ``True`` for custom-code orders, which bypass the
        dispatcher, so this gate is their sole position-cap enforcement point.
        The leverage and concentration checks always run on ``notional``.
        """
        if len(open_positions) >= self.limits.max_open_positions:
            return EntryDecision(
                allowed=False,
                reason=f"max_open_positions ({self.limits.max_open_positions}) reached",
            )

        # A non-positive-equity (ruined) account can never safely add exposure:
        # a percent-of-equity cap admits no positive position there, and the
        # leverage/concentration ratios below are undefined. Reject
        # UNCONDITIONALLY — including for presized orders, whose sizing was
        # decided on an earlier bar when equity was still positive; that stale
        # decision does not make a now-bankrupt account safe to fill into.
        # Without this, a presized order (enforce_position_cap=False) would skip
        # this guard AND the equity>0-gated ratio checks and fall through to
        # allowed=True.
        if current_equity <= 0:
            return EntryDecision(
                allowed=False,
                reason=f"non-positive equity ({current_equity:.2f}); cannot open a position",
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

            # max_position_pct is the per-position deployment cap, enforced here
            # only for orders NOT already presized by the dispatcher (i.e.
            # custom-code orders, which bypass the dispatcher's clamp — this gate
            # is their sole enforcement point). Dispatcher-presized orders skip
            # this check: the dispatcher clamped them to the cap at the sizing
            # price, and re-checking at the fill price would falsely reject them
            # on a gap-up. Strict comparison with a float-noise epsilon. The
            # reason string carries enough precision (and the raw notional /
            # equity) to show the real breach — a coarser ``%`` rounding can make
            # a genuine sub-0.05-point overshoot look identical to the cap.
            if enforce_position_cap:
                position_pct = notional / current_equity * 100
                if position_pct > self.limits.max_position_pct * (1.0 + _POSITION_CAP_REL_EPS):
                    return EntryDecision(
                        allowed=False,
                        reason=(
                            f"position {position_pct:.4f}% of equity > "
                            f"max_position_pct {self.limits.max_position_pct}% "
                            f"(notional={notional:.2f}, equity={current_equity:.2f})"
                        ),
                    )

            concentration = notional / current_equity * 100
            if concentration > self.limits.max_symbol_concentration_pct:
                return EntryDecision(
                    allowed=False,
                    reason=(
                        f"symbol concentration {concentration:.4f}% > "
                        f"limit {self.limits.max_symbol_concentration_pct}% "
                        f"(notional={notional:.2f}, equity={current_equity:.2f})"
                    ),
                )

        return EntryDecision(allowed=True, reason="within limits")

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
