"""Liquidity realism gate.

A backtest's published P&L assumes every order fills at the modelled
slippage. In reality, a trade whose dollar size approaches the symbol's
average daily volume incurs market impact that swamps the modelled
friction: the textbook rule of thumb is "stay below 1% of ADV per fill".
Strategies whose alpha relies on oversized fills look great in backtest
and collapse in production.

This gate verifies that every trade's position value fits inside the
configured liquidity envelope. Oversized trades are re-priced with a
proportional slippage haircut and the strategy's profit factor is
recomputed on the adjusted P&L. Two severities:

* **critical** — the realism-adjusted profit factor drops below ``1.0``.
  The strategy's edge is an artefact of unmodelled market impact.
* **warning** — at least one trade is oversized but the adjusted PF is
  still ``>= 1.0``. The strategy survives the haircut but the operator
  should know it's relying on borderline-illiquid fills.

Wired from
:meth:`StrategyLabOrchestrator._run_realism_gates`. Skipped (info) when
the market data is not in scope at the call site — the orchestrator
threads it through on the verification-phase invocation.
"""

from __future__ import annotations

from typing import ClassVar, Dict, List, Optional

from ....market_data_service import OHLCVBar, compute_adv_from_bars
from ....models import TradeRecord
from ..models import GateResultsMixin, QualityGateResult, StrategyLabPhase

GATE = "liquidity_realism"

# Default 1 % of ADV — the standard "fits comfortably without moving
# the market" threshold for retail-sized orders. Hedge funds use lower
# (10-30 bps) but this gate's job is to catch egregious unrealism, not
# to model institutional-grade impact.
_DEFAULT_LIQUIDITY_ENVELOPE_PCT: float = 0.01

# Extra basis points of slippage applied per multiple-of-envelope of
# oversize. A trade at 5× the envelope (5% of ADV) gets 4 × scale_bps of
# additional friction layered on; 2× gets 1 × scale_bps. The scale is
# loose — the goal is to express "oversized fills cost meaningfully more
# than modelled", not to claim impact-modelling rigour.
_DEFAULT_SLIPPAGE_SCALE_BPS: float = 25.0

# ADV lookback in bars; matches the executor's default for
# :func:`compute_adv_from_bars`.
_DEFAULT_ADV_LOOKBACK: int = 20

# Warning trigger: when ``oversized_fraction >= this``, surface even
# if the adjusted profit factor still clears 1.0.
_OVERSIZED_WARNING_FRACTION: float = 0.05


class LiquidityRealismGate(GateResultsMixin):
    """Verification-phase gate over per-trade liquidity headroom.

    Contract:
      Pre: ``trades`` is a list of :class:`TradeRecord`; ``market_data``
      is either ``None`` (gate self-skips) or a mapping from symbol to
      a list of :class:`OHLCVBar` covering at least the trade window.
      Post: returns one or more :class:`QualityGateResult`s tagged with
      the caller's ``phase``. ``critical`` results indicate a
      publication veto; ``warning`` indicates a soft alert.
      Invariants: deterministic over its inputs; never returns an empty
      list; never mutates ``trades`` or ``market_data``.
    """

    GATE: ClassVar[str] = GATE

    def __init__(
        self,
        *,
        liquidity_envelope_pct: float = _DEFAULT_LIQUIDITY_ENVELOPE_PCT,
        slippage_scale_bps: float = _DEFAULT_SLIPPAGE_SCALE_BPS,
        adv_lookback: int = _DEFAULT_ADV_LOOKBACK,
    ) -> None:
        if liquidity_envelope_pct <= 0:
            raise ValueError("liquidity_envelope_pct must be > 0")
        if slippage_scale_bps < 0:
            raise ValueError("slippage_scale_bps must be >= 0")
        if adv_lookback <= 0:
            raise ValueError("adv_lookback must be > 0")
        self._envelope = liquidity_envelope_pct
        self._slippage_scale_bps = slippage_scale_bps
        self._adv_lookback = adv_lookback

    def check(
        self,
        trades: List[TradeRecord],
        market_data: Optional[Dict[str, List[OHLCVBar]]],
        *,
        phase: StrategyLabPhase = "verification",
    ) -> List[QualityGateResult]:
        with self._using_phase(phase):
            if not trades:
                return [self._info("Liquidity check skipped: trade ledger is empty.")]
            if market_data is None:
                return [
                    self._info(
                        "Liquidity check skipped: market_data not threaded to "
                        "the gate (caller-side limitation)."
                    )
                ]

            oversized_count = 0
            adjusted_gross_profit = 0.0
            adjusted_gross_loss = 0.0
            unresolvable_count = 0
            for trade in trades:
                adv = _adv_as_of_trade(
                    market_data.get(trade.symbol),
                    trade.entry_date,
                    lookback=self._adv_lookback,
                )
                envelope_capacity = self._envelope * adv if adv is not None and adv > 0 else None
                if envelope_capacity is None:
                    # No usable ADV at this trade's timestamp — keep its
                    # reported P&L (no haircut, no signal either way) and
                    # bump the unresolvable counter so the verdict surfaces
                    # it.
                    unresolvable_count += 1
                    adjusted_pnl = trade.net_pnl
                else:
                    oversize_ratio = trade.position_value / envelope_capacity
                    if oversize_ratio > 1.0:
                        oversized_count += 1
                        extra_bps = self._slippage_scale_bps * (oversize_ratio - 1.0)
                        haircut = extra_bps / 10_000.0 * trade.position_value
                        adjusted_pnl = trade.net_pnl - haircut
                    else:
                        adjusted_pnl = trade.net_pnl
                if adjusted_pnl > 0:
                    adjusted_gross_profit += adjusted_pnl
                elif adjusted_pnl < 0:
                    adjusted_gross_loss += -adjusted_pnl

            adjusted_pf = _profit_factor(adjusted_gross_profit, adjusted_gross_loss)
            oversized_fraction = oversized_count / len(trades)

            return [
                self._verdict(
                    oversized_count=oversized_count,
                    oversized_fraction=oversized_fraction,
                    adjusted_pf=adjusted_pf,
                    unresolvable_count=unresolvable_count,
                    total_trades=len(trades),
                )
            ]

    def _verdict(
        self,
        *,
        oversized_count: int,
        oversized_fraction: float,
        adjusted_pf: float,
        unresolvable_count: int,
        total_trades: int,
    ) -> QualityGateResult:
        # Critical attribution requires both signals: PF must collapse AND
        # the collapse must be caused by oversized trades. Without an
        # oversize signal, a sub-1.0 PF reflects an ordinary losing
        # strategy, not a liquidity failure — vetoing here would mislabel
        # the cause and pre-empt other gates (acceptance, anomaly) that
        # are responsible for "the strategy lost money".
        if oversized_count > 0 and adjusted_pf < 1.0:
            return self._critical(
                f"Realism-adjusted profit factor {adjusted_pf:.2f} < 1.0 after "
                f"slippage haircut on {oversized_count} oversized trade(s) "
                f"({oversized_fraction:.0%} of {total_trades}). Strategy's "
                "edge collapses once trades are sized within "
                f"{self._envelope:.0%} of ADV."
            )
        if oversized_fraction >= _OVERSIZED_WARNING_FRACTION:
            return self._warning(
                f"{oversized_count} of {total_trades} trades "
                f"({oversized_fraction:.0%}) exceeded the "
                f"{self._envelope:.0%}-of-ADV liquidity envelope; "
                f"realism-adjusted profit factor {adjusted_pf:.2f} still "
                ">= 1.0 but the strategy relies on borderline-illiquid fills."
            )
        if unresolvable_count > 0 and oversized_count == 0:
            return self._info(
                f"Liquidity check: {oversized_count} oversized trades; ADV "
                f"unresolvable for {unresolvable_count} of {total_trades} "
                "(insufficient bars or zero-volume window)."
            )
        return self._info(
            f"Liquidity check clean: {oversized_count} of {total_trades} "
            f"trades exceeded {self._envelope:.0%}-of-ADV."
        )


def _adv_as_of_trade(
    bars: Optional[List[OHLCVBar]], entry_date: str, *, lookback: int
) -> Optional[float]:
    """Trailing-N-bar dollar ADV computed from bars BEFORE the trade's
    entry date.

    Preconditions:
      - ``bars`` is either ``None`` (unknown symbol) or a list of
        :class:`OHLCVBar`.
      - ``entry_date`` is the trade's ``YYYY-MM-DD`` (or full ISO
        timestamp) string. Bars are filtered on calendar-date prefix so
        intraday bar timestamps are tolerated.
      - ``lookback`` is the desired window size in bars; > 0.
    Postconditions:
      - Returns the trailing dollar ADV over the last ``lookback`` bars
        whose ``date[:10] < entry_date[:10]``. Volume from the entry
        day itself is excluded — at order-submission time on day D, the
        D-day volume isn't known yet, so including it would be a
        look-ahead leak.
      - Returns ``None`` when ``bars`` is ``None``/empty, fewer than
        ``lookback`` prior bars are available, or every prior bar has
        zero volume.
    Invariants:
      - Linear scan over ``bars`` (no sort assumption is required, but
        the typical caller passes chronologically-ordered bars from the
        market-data fetcher). The lookback is the most recent
        ``lookback`` bars in input order whose date precedes the trade.
    """
    if not bars or lookback <= 0 or not entry_date:
        return None
    cutoff = entry_date[:10]
    prior_bars = [b for b in bars if b.date[:10] < cutoff]
    if len(prior_bars) < lookback:
        return None
    return compute_adv_from_bars(prior_bars, lookback=lookback)


def _profit_factor(gross_profit: float, gross_loss: float) -> float:
    """Return ``gross_profit / gross_loss``; treat zero-loss as infinity.

    Preconditions:
      - ``gross_profit`` and ``gross_loss`` are non-negative.
    Postconditions:
      - Returns ``float('inf')`` when ``gross_loss == 0`` and
        ``gross_profit > 0``.
      - Returns ``0.0`` when both sides are zero — a strategy that
        neither won nor lost is not profitable.
    """
    if gross_loss > 0:
        return gross_profit / gross_loss
    return float("inf") if gross_profit > 0 else 0.0


__all__ = ["GATE", "LiquidityRealismGate"]
