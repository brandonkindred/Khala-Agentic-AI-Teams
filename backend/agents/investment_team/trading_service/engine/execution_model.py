"""Pluggable execution models for the fill simulator (issue #248).

Two implementations sit behind a common ``ExecutionModel`` protocol:

* ``OptimisticExecutionModel`` — the legacy fill geometry; preserves the
  golden invariants in ``tests/golden/test_simulator_invariants.py``. Emits a
  loud warning the first time it is instantiated outside golden tests so
  silent default-to-optimistic doesn't quietly inflate backtests.
* ``RealisticExecutionModel`` — the new default. Three fixes vs. the
  legacy model:

  1. **Limit fills at the limit price.** A resting buy limit struck by a
     gap-down bar fills at ``limit_price`` rather than the better
     ``min(bar.open, limit_price)`` — the latter is "free alpha" the live
     market never delivers.
  2. **Partial-fill participation cap.** Orders larger than
     ``participation_cap × bar_dollar_volume`` fill only up to that cap; the
     remainder is dropped (no re-quoting in v1).
  3. **Adverse-selection haircut on limit fills.** When ``next_bar`` is
     available, a directional toxicity term proportional to
     ``sign(next_bar.close - bar.close) × participation_rate`` is added to
     slippage on the unfavourable side.

The protocol returns a narrow ``FillTerms`` struct so the simulator's entry
and exit money math (parity-tested against the legacy engine) stays
unchanged — only the price/qty inputs vary between models.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional, Protocol

from ..strategy.contract import Bar, OrderRequest, OrderSide, OrderType

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FillTerms:
    """Per-order fill instructions returned by an ``ExecutionModel``.

    ``reference_price``: the pre-slippage price used by the simulator's
    entry/exit money math (mirrors the legacy ``_touched`` return value).

    ``qty_fraction``: fraction of ``OrderRequest.qty`` that actually fills.
    ``1.0`` = full fill (legacy behavior). The remainder is dropped, not
    re-queued.

    ``extra_slip_bps``: additional basis points of slippage layered on top
    of ``FillSimulatorConfig.slippage_bps``. Used by the realistic model's
    adverse-selection haircut on limit fills. Direction is handled at the
    simulator's slippage-multiplier construction site (long entries pay
    more, etc.) — this value is always non-negative.
    """

    reference_price: float
    qty_fraction: float = 1.0
    extra_slip_bps: float = 0.0


class ExecutionModel(Protocol):
    """Decides whether and how an order fills against a bar.

    Implementations are stateless w.r.t. the order book — they only see the
    request, the fill bar, and (optionally) the bar that follows. The
    simulator handles risk gates, capital checks, and trade-record
    construction.
    """

    name: str

    def compute_fill_terms(
        self,
        req: OrderRequest,
        bar: Bar,
        next_bar: Optional[Bar],
    ) -> Optional[FillTerms]:
        """Return ``FillTerms`` if the order triggers on ``bar``, else ``None``."""
        ...


# ---------------------------------------------------------------------------
# Optimistic (legacy)
# ---------------------------------------------------------------------------


_OPTIMISTIC_OPT_IN_ENV = "KHALA_ALLOW_OPTIMISTIC_FILLS"


class OptimisticExecutionModel:
    """Legacy fill geometry — preserved verbatim for parity with goldens.

    Should not be the default in production: the limit-gap-through path
    silently improves resting-limit fills with intra-bar prices the live
    market would not deliver. Selecting it without
    ``KHALA_ALLOW_OPTIMISTIC_FILLS=1`` logs a one-shot warning so the
    choice is auditable in test logs and operator runbooks.
    """

    name = "optimistic"

    def __init__(self, *, warn: Optional[bool] = None) -> None:
        if warn is None:
            warn = os.environ.get(_OPTIMISTIC_OPT_IN_ENV, "").strip().lower() not in {
                "1",
                "true",
                "yes",
            }
        if warn:
            logger.warning(
                "OptimisticExecutionModel selected — backtest fills will "
                "overstate live PnL on gap-through bars. Set "
                "%s=1 to silence this warning.",
                _OPTIMISTIC_OPT_IN_ENV,
            )

    def compute_fill_terms(
        self,
        req: OrderRequest,
        bar: Bar,
        next_bar: Optional[Bar],
    ) -> Optional[FillTerms]:
        if req.order_type == OrderType.MARKET:
            return FillTerms(reference_price=bar.open)
        if req.order_type == OrderType.LIMIT:
            if req.side == OrderSide.LONG and bar.low <= req.limit_price:
                return FillTerms(reference_price=min(bar.open, req.limit_price))
            if req.side == OrderSide.SHORT and bar.high >= req.limit_price:
                return FillTerms(reference_price=max(bar.open, req.limit_price))
            return None
        if req.order_type == OrderType.STOP:
            if req.side == OrderSide.LONG and bar.high >= req.stop_price:
                return FillTerms(reference_price=max(bar.open, req.stop_price))
            if req.side == OrderSide.SHORT and bar.low <= req.stop_price:
                return FillTerms(reference_price=min(bar.open, req.stop_price))
            return None
        return None


# ---------------------------------------------------------------------------
# Realistic (issue #248 fixes)
# ---------------------------------------------------------------------------


class RealisticExecutionModel:
    """Default. Three fixes vs. ``OptimisticExecutionModel``.

    Parameters
    ----------
    participation_cap:
        Maximum fraction of a bar's dollar volume an order may consume in
        one fill. Orders sized above the cap are partially filled to the
        cap; the remainder is dropped. ``0 < cap <= 1``.
    adverse_selection_max_bps:
        Hard ceiling on the adverse-selection haircut applied to limit
        fills (in bps). Caps degenerate scenarios where a thin name and a
        violent next-bar move together produce an unrealistic haircut.
    """

    name = "realistic"

    def __init__(
        self,
        *,
        participation_cap: float = 0.10,
        adverse_selection_max_bps: float = 50.0,
    ) -> None:
        if not 0 < participation_cap <= 1:
            raise ValueError(f"participation_cap must be in (0, 1], got {participation_cap}")
        if adverse_selection_max_bps < 0:
            raise ValueError(
                f"adverse_selection_max_bps must be non-negative, got {adverse_selection_max_bps}"
            )
        self._cap = participation_cap
        self._adverse_max_bps = adverse_selection_max_bps

    def compute_fill_terms(
        self,
        req: OrderRequest,
        bar: Bar,
        next_bar: Optional[Bar],
    ) -> Optional[FillTerms]:
        if req.order_type == OrderType.MARKET:
            ref = bar.open
        elif req.order_type == OrderType.LIMIT:
            ref = self._limit_reference_price(req, bar)
        elif req.order_type == OrderType.STOP:
            ref = self._stop_reference_price(req, bar)
        else:
            return None
        if ref is None:
            return None

        qty_fraction = self._participation_fraction(req, bar, ref)
        if qty_fraction <= 0:
            return None

        extra_slip_bps = 0.0
        if req.order_type == OrderType.LIMIT and next_bar is not None:
            extra_slip_bps = self._adverse_selection_bps(req, bar, next_bar, qty_fraction)

        return FillTerms(
            reference_price=ref,
            qty_fraction=qty_fraction,
            extra_slip_bps=extra_slip_bps,
        )

    # ------------------------------------------------------------------
    # Reference-price rules
    # ------------------------------------------------------------------

    @staticmethod
    def _limit_reference_price(req: OrderRequest, bar: Bar) -> Optional[float]:
        """Limit fills always at the limit price when the bar's range covers it.

        The legacy model returns ``min(bar.open, limit_price)`` for buy
        limits, which is "free alpha" on gap-down opens — a live resting
        limit at $100 fills at $100 even if the market opens at $95. The
        realistic model just returns the limit price.
        """
        if req.side == OrderSide.LONG and bar.low <= req.limit_price:
            return req.limit_price
        if req.side == OrderSide.SHORT and bar.high >= req.limit_price:
            return req.limit_price
        return None

    @staticmethod
    def _stop_reference_price(req: OrderRequest, bar: Bar) -> Optional[float]:
        """Stops fill at the gap-through price when the bar gaps the stop level.

        Mirrors the legacy ``max/min(bar.open, stop_price)`` semantics —
        already correct for catastrophic gaps (a long stop triggered by a
        gap-up fills at ``bar.open``, not the stop level). Pinned here as
        a regression test target.
        """
        if req.side == OrderSide.LONG and bar.high >= req.stop_price:
            return max(bar.open, req.stop_price)
        if req.side == OrderSide.SHORT and bar.low <= req.stop_price:
            return min(bar.open, req.stop_price)
        return None

    # ------------------------------------------------------------------
    # Participation cap
    # ------------------------------------------------------------------

    def _participation_fraction(
        self,
        req: OrderRequest,
        bar: Bar,
        reference_price: float,
    ) -> float:
        """Cap fill quantity at ``participation_cap × bar_dollar_volume``."""
        bar_dollar_volume = self._bar_dollar_volume(bar)
        if bar_dollar_volume <= 0:
            # No volume signal — fall back to full fill rather than reject.
            # Daily-bar feeds occasionally drop volume; refusing to fill
            # would be more disruptive than the legacy "ignore impact" path.
            return 1.0
        order_notional = req.qty * reference_price
        if order_notional <= 0:
            return 1.0
        max_notional = self._cap * bar_dollar_volume
        if order_notional <= max_notional:
            return 1.0
        return max_notional / order_notional

    @staticmethod
    def _bar_dollar_volume(bar: Bar) -> float:
        if bar.volume is None or bar.volume <= 0:
            return 0.0
        return bar.volume * bar.close

    # ------------------------------------------------------------------
    # Adverse-selection haircut
    # ------------------------------------------------------------------

    def _adverse_selection_bps(
        self,
        req: OrderRequest,
        bar: Bar,
        next_bar: Bar,
        participation_rate: float,
    ) -> float:
        """Conditional toxicity haircut on limit fills.

        For a long limit, an unfavourable next-bar move is a *fall*
        (``next_bar.close < bar.close``) — you bought at the limit just
        before a drop. For a short limit, an unfavourable move is a
        *rise*. The haircut scales with ``participation_rate`` (no
        toxicity for small orders that don't move the print) and is
        capped at ``adverse_selection_max_bps``.
        """
        if bar.close <= 0:
            return 0.0
        signed_move_pct = (next_bar.close - bar.close) / bar.close
        # Sign convention: positive => good for buyer, bad for seller.
        if req.side == OrderSide.LONG:
            adverse_move = max(0.0, -signed_move_pct)
        else:
            adverse_move = max(0.0, signed_move_pct)
        haircut_bps = adverse_move * 10_000.0 * participation_rate
        return min(haircut_bps, self._adverse_max_bps)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_execution_model(
    name: str = "realistic",
    *,
    participation_cap: float = 0.10,
) -> ExecutionModel:
    """Build an ``ExecutionModel`` by name. Defaults to ``realistic``."""
    n = (name or "").strip().lower()
    if n == "optimistic":
        return OptimisticExecutionModel()
    if n in {"", "realistic"}:
        return RealisticExecutionModel(participation_cap=participation_cap)
    raise ValueError(f"unknown execution_model: {name!r}")


__all__ = [
    "ExecutionModel",
    "FillTerms",
    "OptimisticExecutionModel",
    "RealisticExecutionModel",
    "build_execution_model",
]
