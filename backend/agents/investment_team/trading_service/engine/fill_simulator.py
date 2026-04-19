"""Fill simulator — decides which pending orders fill on the next bar.

In backtest mode the strategy has already seen bar *t* and submitted orders;
the service advances to bar *t+1* and uses that bar's full OHLC to decide
which orders fill and at what price. The strategy **does not** have access
to bar *t+1* until after its ``on_fill`` events have been delivered.

Microstructure rules (kept deliberately simple; each is noted):

- ``market`` orders fill at the next bar's open, adjusted by slippage.
- ``limit`` long orders fill if ``bar.low <= limit_price``; fill price is
  ``min(open, limit_price)`` adjusted by slippage (if the bar opens below
  the limit, we assume the fill happens at the open, not the limit).
- ``limit`` short orders fill if ``bar.high >= limit_price``; fill price is
  ``max(open, limit_price)`` adjusted by slippage.
- ``stop`` long orders trigger when ``bar.high >= stop_price``; fill price
  is ``max(open, stop_price)`` adjusted by slippage.
- ``stop`` short orders trigger when ``bar.low <= stop_price``; fill price
  is ``min(open, stop_price)`` adjusted by slippage.

Transaction costs and realized P&L on close match the legacy
``TradeSimulationEngine._close_position`` math so parity tests hold.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from ...execution.bar_safety import BarSafetyAssertion
from ...execution.risk_filter import RiskFilter
from ...models import TradeRecord
from ..strategy.contract import Bar, Fill, OrderSide, OrderType
from .order_book import OrderBook, PendingOrder
from .portfolio import Portfolio, Position

logger = logging.getLogger(__name__)


@dataclass
class FillSimulatorConfig:
    slippage_bps: float = 2.0
    transaction_cost_bps: float = 5.0


@dataclass
class FillOutcome:
    """Everything that happened on one fill tick for one symbol."""

    entry_fills: List[Fill]
    exit_fills: List[Fill]
    closed_trades: List[TradeRecord]


class FillSimulator:
    """Match pending orders against a newly-arrived bar."""

    def __init__(
        self,
        *,
        portfolio: Portfolio,
        order_book: OrderBook,
        risk_filter: RiskFilter,
        config: FillSimulatorConfig,
        bar_safety: Optional[BarSafetyAssertion] = None,
    ) -> None:
        self.portfolio = portfolio
        self.order_book = order_book
        self.risk = risk_filter
        self.config = config
        # Defaults to an enabled assertion so any engine refactor that
        # accidentally fills an order against a not-strictly-future bar
        # fails loudly.  Tests that construct pathological traces can pass
        # ``BarSafetyAssertion(enabled=False)`` to suppress it.
        self.bar_safety = bar_safety or BarSafetyAssertion()
        self._trade_num = 0

    # ------------------------------------------------------------------
    # Public entrypoint: process one fill tick for one symbol/bar.
    # ------------------------------------------------------------------

    def process_bar(self, bar: Bar) -> FillOutcome:
        entry_fills: List[Fill] = []
        exit_fills: List[Fill] = []
        closed: List[TradeRecord] = []

        slip_mult_long_entry = 1.0 + self.config.slippage_bps / 10_000.0
        slip_mult_long_exit = 1.0 - self.config.slippage_bps / 10_000.0
        slip_mult_short_entry = 1.0 - self.config.slippage_bps / 10_000.0
        slip_mult_short_exit = 1.0 + self.config.slippage_bps / 10_000.0

        # Work on a snapshot of pending orders for this symbol so cancels /
        # removes inside the loop don't mutate iteration.
        pending = list(self.order_book.pending_for_symbol(bar.symbol))

        for po in pending:
            req = po.request
            # Determine whether this bar triggered the order and at what
            # reference price.
            ref_price = self._touched(req, bar)
            if ref_price is None:
                continue

            # Parent-side look-ahead guard: any triggered order must belong
            # to a strictly-earlier bar than the one we're filling against.
            self.bar_safety.check_fill(
                order_id=po.order_id,
                submitted_at=po.submitted_at,
                fill_bar_timestamp=bar.timestamp,
            )

            has_position = bar.symbol in self.portfolio.positions
            is_entry = (
                not has_position
                and req.side in (OrderSide.LONG, OrderSide.SHORT)
                and (
                    # Treat an order as an entry when there's no open position and
                    # the request direction would create one. Exit requests from
                    # strategies are expressed as an opposite-side order on a
                    # symbol where a position is already open.
                    not has_position
                )
            )

            if is_entry:
                pos = self._fill_entry(
                    po, bar, ref_price, slip_mult_long_entry, slip_mult_short_entry
                )
                if pos is None:
                    continue
                self.portfolio.open(pos)
                fill = self.portfolio.make_entry_fill(pos)
                entry_fills.append(fill)
                self.order_book.remove(po.order_id)
            elif has_position:
                # Exit path: order closes out the open position. We only
                # support full-qty exits in PR 1 (matches legacy behavior at
                # trade_simulator.py:560-565).
                pos = self.portfolio.positions[bar.symbol]
                if req.side == pos.side:
                    # Ignoring same-side add-ons in PR 1 — log and leave.
                    logger.debug(
                        "ignoring same-side order %s for already-open %s position",
                        po.order_id,
                        pos.side.value,
                    )
                    self.order_book.remove(po.order_id)
                    continue
                trade = self._fill_exit(
                    po, bar, ref_price, slip_mult_long_exit, slip_mult_short_exit
                )
                if trade is None:
                    continue
                closed.append(trade)
                exit_fills.append(
                    Fill(
                        order_id=po.order_id,
                        client_order_id=req.client_order_id,
                        symbol=bar.symbol,
                        side=req.side,
                        qty=pos.qty,
                        price=trade.exit_price,
                        timestamp=bar.timestamp,
                        reason="exit",
                    )
                )
                self.order_book.remove(po.order_id)

        return FillOutcome(
            entry_fills=entry_fills,
            exit_fills=exit_fills,
            closed_trades=closed,
        )

    # ------------------------------------------------------------------
    # Trigger logic
    # ------------------------------------------------------------------

    @staticmethod
    def _touched(req, bar: Bar) -> Optional[float]:
        """Return a reference price if the order would trigger on this bar, else None."""
        if req.order_type == OrderType.MARKET:
            return bar.open
        if req.order_type == OrderType.LIMIT:
            if req.side == OrderSide.LONG and bar.low <= req.limit_price:
                return min(bar.open, req.limit_price)
            if req.side == OrderSide.SHORT and bar.high >= req.limit_price:
                return max(bar.open, req.limit_price)
            return None
        if req.order_type == OrderType.STOP:
            if req.side == OrderSide.LONG and bar.high >= req.stop_price:
                return max(bar.open, req.stop_price)
            if req.side == OrderSide.SHORT and bar.low <= req.stop_price:
                return min(bar.open, req.stop_price)
            return None
        return None

    # ------------------------------------------------------------------
    # Entry / exit money math (mirrors legacy engine)
    # ------------------------------------------------------------------

    def _fill_entry(
        self,
        po: PendingOrder,
        bar: Bar,
        ref_price: float,
        slip_long: float,
        slip_short: float,
    ) -> Optional[Position]:
        req = po.request
        qty = req.qty
        # Vol-target sizing is not re-run here — strategies own sizing via
        # ``ctx.submit_order(qty=...)``. The risk filter still caps via
        # ``can_enter``, which mirrors the legacy engine's behavior.
        notional = qty * ref_price
        equity = self.portfolio.mark_to_market()
        gate = self.risk.can_enter(req.symbol, notional, equity, self.portfolio.positions)
        if not gate.allowed:
            logger.info("risk gate rejected entry for %s: %s", req.symbol, gate.reason)
            self.order_book.remove(po.order_id)
            return None
        if self.portfolio.capital < notional:
            logger.info(
                "insufficient capital for %s entry: need %.2f, have %.2f",
                req.symbol,
                notional,
                self.portfolio.capital,
            )
            self.order_book.remove(po.order_id)
            return None

        dp = 4 if ref_price < 10 else 2
        if req.side == OrderSide.LONG:
            fill_price = round(ref_price * slip_long, dp)
        else:
            fill_price = round(ref_price * slip_short, dp)

        return Position(
            symbol=req.symbol,
            side=req.side,
            qty=qty,
            entry_price=fill_price,
            entry_bid_price=round(ref_price, dp),
            entry_timestamp=bar.timestamp,
            entry_order_id=po.order_id,
            entry_client_order_id=req.client_order_id,
            entry_order_type=req.order_type.value,
        )

    def _fill_exit(
        self,
        po: PendingOrder,
        bar: Bar,
        ref_price: float,
        slip_long: float,
        slip_short: float,
    ) -> Optional[TradeRecord]:
        pos = self.portfolio.positions[bar.symbol]
        dp = 4 if ref_price < 10 else 2
        # Apply slippage against the position's original direction.
        if pos.side == OrderSide.LONG:
            exit_price = round(ref_price * slip_long, dp)
        else:
            exit_price = round(ref_price * slip_short, dp)

        # Realized P&L — matches trade_simulator._close_position math.
        cost_mult = self.config.transaction_cost_bps / 10_000.0
        entry_notional = pos.entry_price * pos.qty
        exit_notional = exit_price * pos.qty
        if pos.side == OrderSide.LONG:
            gross = (exit_price - pos.entry_price) * pos.qty
        else:
            gross = (pos.entry_price - exit_price) * pos.qty
        tx_costs = (entry_notional + exit_notional) * cost_mult
        net = round(gross - tx_costs, 2)

        self._trade_num += 1
        self.portfolio.record_pnl(net)
        self.portfolio.close(bar.symbol, exit_price)

        hold_days = _date_diff(pos.entry_timestamp, bar.timestamp)
        record = TradeRecord(
            trade_num=self._trade_num,
            entry_date=pos.entry_timestamp[:10],
            exit_date=bar.timestamp[:10],
            symbol=pos.symbol,
            side=pos.side.value,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            shares=pos.qty,
            position_value=round(entry_notional, 2),
            gross_pnl=round(gross, 2),
            net_pnl=net,
            return_pct=round((net / entry_notional * 100) if entry_notional > 0 else 0.0, 2),
            hold_days=hold_days,
            outcome="win" if net > 0 else "loss",
            cumulative_pnl=self.portfolio.cumulative_pnl,
            entry_bid_price=pos.entry_bid_price,
            entry_fill_price=pos.entry_price,
            exit_bid_price=round(ref_price, dp),
            exit_fill_price=exit_price,
            entry_order_type=pos.entry_order_type,
            exit_order_type=po.request.order_type.value,
        )
        return record


def _date_diff(t1: str, t2: str) -> int:
    from datetime import date as date_cls

    try:
        d1 = date_cls.fromisoformat(t1[:10])
        d2 = date_cls.fromisoformat(t2[:10])
        return max(0, abs((d2 - d1).days))
    except (ValueError, TypeError):
        return 0
