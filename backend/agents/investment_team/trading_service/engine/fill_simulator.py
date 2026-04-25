"""Fill simulator — decides which pending orders fill on the next bar.

In backtest mode the strategy has already seen bar *t* and submitted orders;
the service advances to bar *t+1* and uses that bar's full OHLC to decide
which orders fill and at what price. The strategy **does not** have access
to bar *t+1* until after its ``on_fill`` events have been delivered.

The trigger geometry and the (price, qty, slippage) triple per order live
behind a pluggable ``ExecutionModel`` (issue #248). Two implementations
ship: ``OptimisticExecutionModel`` (legacy, used by golden parity tests)
and ``RealisticExecutionModel`` (default; limit fills at limit price,
participation-capped partial fills, adverse-selection haircut).

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
from ..strategy.contract import Bar, Fill, OrderSide
from .execution_model import ExecutionModel, FillTerms, OptimisticExecutionModel
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
        execution_model: Optional[ExecutionModel] = None,
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
        # Default to the optimistic (legacy) model with the warning
        # suppressed — preserves byte-equal behavior for callers that
        # haven't migrated to the realistic default exposed via
        # ``BacktestConfig.execution_model`` (issue #248).
        self.execution_model = execution_model or OptimisticExecutionModel(warn=False)
        self._trade_num = 0

    # ------------------------------------------------------------------
    # Public entrypoint: process one fill tick for one symbol/bar.
    # ------------------------------------------------------------------

    def process_bar(self, bar: Bar, next_bar: Optional[Bar] = None) -> FillOutcome:
        entry_fills: List[Fill] = []
        exit_fills: List[Fill] = []
        closed: List[TradeRecord] = []

        # Work on a snapshot of pending orders for this symbol so cancels /
        # removes inside the loop don't mutate iteration.
        pending = list(self.order_book.pending_for_symbol(bar.symbol))

        for po in pending:
            req = po.request
            # Determine whether this bar triggered the order and at what
            # terms (price, partial-fill fraction, adverse-selection
            # haircut). The execution model encapsulates the (model-
            # dependent) parts; risk gates and money math are simulator-
            # owned below.
            terms = self.execution_model.compute_fill_terms(req, bar, next_bar)
            if terms is None:
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
                pos = self._fill_entry(po, bar, terms)
                if pos is None:
                    continue
                self.portfolio.open(pos)
                fill = self.portfolio.make_entry_fill(pos)
                entry_fills.append(fill)
                self.order_book.remove(po.order_id)
            elif has_position:
                # Exit path: order closes out the open position. We only
                # support full-qty exits in PR 1 (matches legacy behavior at
                # trade_simulator.py:560-565). Partial-fill caps from the
                # execution model apply only to entries — exits always
                # close the position fully.
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
                trade = self._fill_exit(po, bar, terms)
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
    # Slippage helpers
    # ------------------------------------------------------------------

    def _slippage_multipliers(self, extra_slip_bps: float) -> tuple[float, float, float, float]:
        """Return (long_entry, long_exit, short_entry, short_exit) multipliers.

        ``extra_slip_bps`` widens the band on both legs symmetrically so
        the realistic model's adverse-selection haircut shows up as a
        worse fill price regardless of whether the fill is an entry or
        exit on either side.
        """
        slip_bps = self.config.slippage_bps + max(0.0, extra_slip_bps)
        s = slip_bps / 10_000.0
        return (
            1.0 + s,  # long entry: pay more
            1.0 - s,  # long exit: receive less
            1.0 - s,  # short entry: receive less
            1.0 + s,  # short exit: pay more
        )

    # ------------------------------------------------------------------
    # Entry / exit money math (mirrors legacy engine)
    # ------------------------------------------------------------------

    def _fill_entry(
        self,
        po: PendingOrder,
        bar: Bar,
        terms: FillTerms,
    ) -> Optional[Position]:
        req = po.request
        ref_price = terms.reference_price
        # Apply the execution model's partial-fill cap (1.0 = full).
        # Fractions below 1.0 mean the bar's dollar volume couldn't absorb
        # the full request; the remainder is dropped, not re-quoted.
        qty = req.qty * max(0.0, min(1.0, terms.qty_fraction))
        if qty <= 0:
            self.order_book.remove(po.order_id)
            return None
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

        slip_long_entry, _, slip_short_entry, _ = self._slippage_multipliers(terms.extra_slip_bps)
        dp = 4 if ref_price < 10 else 2
        if req.side == OrderSide.LONG:
            fill_price = round(ref_price * slip_long_entry, dp)
        else:
            fill_price = round(ref_price * slip_short_entry, dp)

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
        terms: FillTerms,
    ) -> Optional[TradeRecord]:
        pos = self.portfolio.positions[bar.symbol]
        ref_price = terms.reference_price
        dp = 4 if ref_price < 10 else 2
        _, slip_long_exit, _, slip_short_exit = self._slippage_multipliers(terms.extra_slip_bps)
        # Apply slippage against the position's original direction.
        if pos.side == OrderSide.LONG:
            exit_price = round(ref_price * slip_long_exit, dp)
        else:
            exit_price = round(ref_price * slip_short_exit, dp)

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
