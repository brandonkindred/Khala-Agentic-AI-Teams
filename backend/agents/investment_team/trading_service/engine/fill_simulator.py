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
from ..strategy.contract import Bar, Fill, FillKind, OrderSide, UnfilledPolicy
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
            # The snapshot can go stale mid-loop: e.g. a parent rejected via
            # the risk-gate or insufficient-capital paths cascade-cancels its
            # bracket children, which may already be in this snapshot. Skip
            # any order that's no longer in the book so cascade-removed
            # children can't slip through and fill on the same bar.
            if po.order_id not in self.order_book:
                continue
            # Pre-armed bracket children (submitted while the parent is still
            # pending) sit in the book with ``armed=False`` until the bracket
            # materializer (#389) flips them on after the parent fills.
            # Skipping them here keeps protective legs from firing as
            # standalone orders before the entry has actually opened.
            if not po.armed:
                continue
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

            existing_pos = self.portfolio.positions.get(bar.symbol)
            # Partial-entry continuation (#386): a requeued partial entry has
            # ``cumulative_filled_qty > 0`` and an existing position whose
            # ``entry_order_id`` matches this pending order. Without this
            # branch the requeued order would hit the same-side-as-position
            # guard below and get silently removed.
            is_partial_entry_continuation = (
                existing_pos is not None
                and existing_pos.entry_order_id == po.order_id
                and po.cumulative_filled_qty > 0
            )
            is_entry = (
                not is_partial_entry_continuation
                and existing_pos is None
                and req.side in (OrderSide.LONG, OrderSide.SHORT)
            )

            if is_partial_entry_continuation:
                fill = self._continue_entry(po, bar, terms)
                if fill is not None:
                    entry_fills.append(fill)
            elif is_entry:
                fill = self._fill_entry(po, bar, terms)
                if fill is not None:
                    entry_fills.append(fill)
            else:
                # Has open position. Either an exit (opposite side) or a
                # same-side add-on we currently don't support.
                pos = existing_pos
                if req.side == pos.side:
                    logger.debug(
                        "ignoring same-side order %s for already-open %s position",
                        po.order_id,
                        pos.side.value,
                    )
                    self.order_book.remove(po.order_id)
                    continue
                exit_fill, trade = self._fill_exit(po, bar, terms)
                if exit_fill is not None:
                    exit_fills.append(exit_fill)
                if trade is not None:
                    closed.append(trade)

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
    ) -> Optional[Fill]:
        """First fill against an entry order. Opens the Position.

        Returns the entry ``Fill`` (full or partial), or ``None`` when the
        order was rejected (risk gate, insufficient capital, zero qty).
        Side effect: drives ``portfolio.open`` and either ``order_book.remove``
        or ``order_book.requeue`` based on the order's ``unfilled_policy``.
        """
        req = po.request
        ref_price = terms.reference_price
        # ``po.remaining_qty`` is the same as ``req.qty`` on first fill
        # (set by ``OrderBook.submit``). Reading from the pending order keeps
        # the path uniform with ``_continue_entry``.
        requested_qty = po.remaining_qty
        qty_fraction = max(0.0, min(1.0, terms.qty_fraction))
        filled_qty = requested_qty * qty_fraction
        unfilled = requested_qty - filled_qty
        dp = 4 if ref_price < 10 else 2

        if filled_qty <= 0:
            # No silent drop: emit a REJECTED Fill so the strategy sees the
            # outcome, then remove the order.
            self.order_book.remove(po.order_id)
            return Fill(
                order_id=po.order_id,
                client_order_id=req.client_order_id,
                symbol=req.symbol,
                side=req.side,
                qty=0.0,
                price=round(ref_price, dp),
                timestamp=bar.timestamp,
                reason="rejected_no_liquidity",
                fill_kind=FillKind.REJECTED,
                unfilled_qty=requested_qty,
                cumulative_filled_qty=po.cumulative_filled_qty,
            )

        notional = filled_qty * ref_price
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
        if req.side == OrderSide.LONG:
            fill_price = round(ref_price * slip_long_entry, dp)
        else:
            fill_price = round(ref_price * slip_short_entry, dp)

        is_partial = unfilled > 0
        pos = Position(
            symbol=req.symbol,
            side=req.side,
            qty=filled_qty,
            entry_price=fill_price,
            entry_bid_price=round(ref_price, dp),
            entry_timestamp=bar.timestamp,
            entry_order_id=po.order_id,
            entry_client_order_id=req.client_order_id,
            entry_order_type=req.order_type.value,
            original_qty=req.qty,
            participation_clipped=is_partial,
            total_unfilled_qty=unfilled,
            # Counts the number of fill events on the entry side: initial
            # fill = 1, every ``REQUEUE_NEXT_BAR`` continuation += 1. Exit
            # slices don't bump this counter (see ``Position.reduce``).
            partial_fill_count=1,
        )
        self.portfolio.open(pos)
        self._handle_entry_remainder(po, bar, unfilled)

        return Fill(
            order_id=po.order_id,
            client_order_id=req.client_order_id,
            symbol=pos.symbol,
            side=pos.side,
            qty=filled_qty,
            price=fill_price,
            timestamp=bar.timestamp,
            reason="entry",
            fill_kind=FillKind.PARTIAL if is_partial else FillKind.FULL,
            unfilled_qty=unfilled,
            cumulative_filled_qty=filled_qty,
        )

    def _continue_entry(
        self,
        po: PendingOrder,
        bar: Bar,
        terms: FillTerms,
    ) -> Optional[Fill]:
        """Apply a follow-on entry fill against an already-open position.

        Used when ``REQUEUE_NEXT_BAR`` requeued an entry's partial-fill
        remainder and the next bar's terms now allow more of it through.
        """
        req = po.request
        ref_price = terms.reference_price
        requested_qty = po.remaining_qty
        qty_fraction = max(0.0, min(1.0, terms.qty_fraction))
        filled_qty = requested_qty * qty_fraction
        unfilled = requested_qty - filled_qty
        dp = 4 if ref_price < 10 else 2

        if filled_qty <= 0:
            # Zero-fraction continuation — drop the order; further requeue
            # would just defer the same outcome. Emit a REJECTED Fill on the
            # remainder so the strategy sees the abandonment.
            self.order_book.remove(po.order_id)
            return Fill(
                order_id=po.order_id,
                client_order_id=req.client_order_id,
                symbol=req.symbol,
                side=req.side,
                qty=0.0,
                price=round(ref_price, dp),
                timestamp=bar.timestamp,
                reason="rejected_no_liquidity",
                fill_kind=FillKind.REJECTED,
                unfilled_qty=requested_qty,
                cumulative_filled_qty=po.cumulative_filled_qty,
            )

        slip_long_entry, _, slip_short_entry, _ = self._slippage_multipliers(terms.extra_slip_bps)
        if req.side == OrderSide.LONG:
            fill_price = round(ref_price * slip_long_entry, dp)
        else:
            fill_price = round(ref_price * slip_short_entry, dp)

        # Capital and risk checks against the *additional* notional only.
        additional_notional = filled_qty * fill_price
        if self.portfolio.capital < additional_notional:
            logger.info(
                "insufficient capital for %s entry continuation: need %.2f, have %.2f",
                req.symbol,
                additional_notional,
                self.portfolio.capital,
            )
            self.order_book.remove(po.order_id)
            return None

        pos = self.portfolio.extend(req.symbol, filled_qty, fill_price)
        is_partial = unfilled > 0
        if is_partial:
            pos.participation_clipped = True
        pos.total_unfilled_qty += unfilled
        pos.partial_fill_count += 1

        self._handle_entry_remainder(po, bar, unfilled)

        return Fill(
            order_id=po.order_id,
            client_order_id=req.client_order_id,
            symbol=pos.symbol,
            side=pos.side,
            qty=filled_qty,
            price=fill_price,
            timestamp=bar.timestamp,
            reason="entry",
            fill_kind=FillKind.PARTIAL if is_partial else FillKind.FULL,
            unfilled_qty=unfilled,
            cumulative_filled_qty=pos.qty,
        )

    def _handle_entry_remainder(
        self,
        po: PendingOrder,
        bar: Bar,
        unfilled: float,
    ) -> None:
        """Decide whether to requeue or remove an entry order's remainder."""
        policy = po.request.unfilled_policy or UnfilledPolicy.DROP
        if unfilled > 0 and policy == UnfilledPolicy.REQUEUE_NEXT_BAR:
            self.order_book.requeue(
                po.order_id,
                new_remaining_qty=unfilled,
                new_submitted_at=bar.timestamp,
                was_filled=True,
            )
            return
        # TWAP_N is wired in #387; until then it falls through to DROP.
        # Entries that fully fill or get DROPped both end here. ``was_filled=True``
        # keeps the parent id eligible for bracket activation (#389).
        self.order_book.remove(po.order_id, was_filled=True)

    def _fill_exit(
        self,
        po: PendingOrder,
        bar: Bar,
        terms: FillTerms,
    ) -> tuple[Optional[Fill], Optional[TradeRecord]]:
        """Close (or partially close) the open position.

        Returns ``(exit_fill, trade_record)``. ``trade_record`` is ``None``
        until the position is fully closed (after partial exits, every
        intermediate bar still emits an exit ``Fill`` but no record).
        """
        req = po.request
        pos = self.portfolio.positions[bar.symbol]
        ref_price = terms.reference_price
        dp = 4 if ref_price < 10 else 2
        _, slip_long_exit, _, slip_short_exit = self._slippage_multipliers(terms.extra_slip_bps)
        if pos.side == OrderSide.LONG:
            exit_price = round(ref_price * slip_long_exit, dp)
        else:
            exit_price = round(ref_price * slip_short_exit, dp)

        # Bound the exit fill by both the strategy's requested qty
        # (``po.remaining_qty``, which OrderBook.submit pins to ``req.qty``
        # on first slice and the requeue path shrinks on partial slices) and
        # the position's currently-open qty. The position cap matters when
        # an entry continuation runs earlier in the same bar and grows the
        # position past what the strategy saw at submission time — without
        # this min() the exit could close newly-added shares the strategy
        # never intended to unwind.
        requested_exit_qty = min(po.remaining_qty, pos.qty)
        qty_fraction = max(0.0, min(1.0, terms.qty_fraction))
        filled_qty = requested_exit_qty * qty_fraction
        unfilled = requested_exit_qty - filled_qty

        if filled_qty <= 0:
            # No liquidity for any exit slice on this bar. Decide whether to
            # try again next bar or abandon the order; emit a REJECTED Fill
            # either way so the strategy sees the outcome.
            self._handle_exit_remainder(po, bar, unfilled)
            rejected = Fill(
                order_id=po.order_id,
                client_order_id=req.client_order_id,
                symbol=pos.symbol,
                side=req.side,
                qty=0.0,
                price=round(ref_price, dp),
                timestamp=bar.timestamp,
                reason="rejected_no_liquidity",
                fill_kind=FillKind.REJECTED,
                unfilled_qty=requested_exit_qty,
                cumulative_filled_qty=pos.cumulative_exit_qty,
            )
            return rejected, None

        self.portfolio.partial_close(bar.symbol, filled_qty, exit_price)
        if unfilled > 0:
            pos.participation_clipped = True
            pos.total_unfilled_qty += unfilled

        is_closed = pos.is_closed
        exit_fill = Fill(
            order_id=po.order_id,
            client_order_id=req.client_order_id,
            symbol=pos.symbol,
            side=req.side,
            qty=filled_qty,
            price=exit_price,
            timestamp=bar.timestamp,
            reason="exit",
            fill_kind=FillKind.FULL if is_closed else FillKind.PARTIAL,
            unfilled_qty=unfilled,
            cumulative_filled_qty=pos.cumulative_exit_qty,
        )

        if not is_closed:
            self._handle_exit_remainder(po, bar, unfilled)
            return exit_fill, None

        # Position fully closed — build the TradeRecord using the qty-weighted
        # avg exit price across all partial exits so cumulative P&L is honest.
        final_exit_price = pos.weighted_avg_exit_price
        cost_mult = self.config.transaction_cost_bps / 10_000.0
        entry_notional = pos.entry_price * pos.original_qty
        exit_notional = final_exit_price * pos.original_qty
        if pos.side == OrderSide.LONG:
            gross = (final_exit_price - pos.entry_price) * pos.original_qty
        else:
            gross = (pos.entry_price - final_exit_price) * pos.original_qty
        tx_costs = (entry_notional + exit_notional) * cost_mult
        net = round(gross - tx_costs, 2)

        self._trade_num += 1
        self.portfolio.record_pnl(net)
        # ``partial_close`` already credited cash for every slice; this terminal
        # ``close`` just pops the position (pos.qty is ~0 by now).
        self.portfolio.close(bar.symbol, final_exit_price)
        self.order_book.remove(po.order_id)

        hold_days = _date_diff(pos.entry_timestamp, bar.timestamp)
        record = TradeRecord(
            trade_num=self._trade_num,
            entry_date=pos.entry_timestamp[:10],
            exit_date=bar.timestamp[:10],
            symbol=pos.symbol,
            side=pos.side.value,
            entry_price=pos.entry_price,
            exit_price=final_exit_price,
            shares=pos.original_qty,
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
            exit_fill_price=final_exit_price,
            entry_order_type=pos.entry_order_type,
            exit_order_type=po.request.order_type.value,
            participation_clipped=pos.participation_clipped,
            partial_fill_count=pos.partial_fill_count,
            total_unfilled_qty=pos.total_unfilled_qty,
        )
        return exit_fill, record

    def _handle_exit_remainder(
        self,
        po: PendingOrder,
        bar: Bar,
        unfilled: float,
    ) -> None:
        """Decide whether to requeue or remove an exit order's remainder.

        Exit orders are never bracket parents (they don't open positions),
        so ``was_filled=False`` is correct for both branches — see the
        docstring at ``order_book.OrderBook.requeue``.
        """
        policy = po.request.unfilled_policy or UnfilledPolicy.DROP
        if unfilled > 0 and policy == UnfilledPolicy.REQUEUE_NEXT_BAR:
            self.order_book.requeue(
                po.order_id,
                new_remaining_qty=unfilled,
                new_submitted_at=bar.timestamp,
                was_filled=False,
            )
            return
        self.order_book.remove(po.order_id)


def _date_diff(t1: str, t2: str) -> int:
    from datetime import date as date_cls

    try:
        d1 = date_cls.fromisoformat(t1[:10])
        d2 = date_cls.fromisoformat(t2[:10])
        return max(0, abs((d2 - d1).days))
    except (ValueError, TypeError):
        return 0
