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

            # Stale-continuation guard: a pre-filled order whose target
            # position has vanished — *or has been replaced by a
            # different one on the same symbol* — is stale and must
            # drop. Two failure modes covered:
            #   (a) ``existing_pos is None`` — the position was closed
            #       by another order; falling through to ``_fill_entry``
            #       would open a brand-new opposite-side position.
            #   (b) ``existing_pos.entry_order_id !=
            #       po.working_against_entry_order_id`` — the original
            #       position was closed *and* replaced (e.g. a low-id
            #       stop-entry that triggers later in the same bar's
            #       snapshot, or a manual re-entry between bars). The
            #       stale exit slice would otherwise close shares from
            #       the brand-new, unrelated position.
            # Must fire on EVERY bar (triggered or not), before the
            # TWAP elapsed-bar tick below; otherwise an untriggered bar
            # could keep a stale remainder alive long enough to fire
            # against the wrong position on a later triggered bar.
            # Fresh entries (``cumulative_filled_qty == 0``) and live
            # continuations (target position still open and intact) are
            # unaffected.
            existing_pos = self.portfolio.positions.get(bar.symbol)
            if po.cumulative_filled_qty > 0:
                bound_id = po.working_against_entry_order_id
                if existing_pos is None or (
                    bound_id is not None and existing_pos.entry_order_id != bound_id
                ):
                    self.order_book.remove(po.order_id)
                    continue

            # Determine whether this bar triggered the order and at what
            # terms (price, partial-fill fraction, adverse-selection
            # haircut). The execution model encapsulates the (model-
            # dependent) parts; risk gates and money math are simulator-
            # owned below.
            terms = self.execution_model.compute_fill_terms(req, bar, next_bar)
            if terms is None:
                # ``TWAP_N`` orders consume a slice on every elapsed bar,
                # not only on bars where the execution model triggers a
                # fill. Without this, a ``LIMIT``/``STOP`` TWAP order
                # whose trigger geometry kept it untouched between
                # slices could run past its declared N-bar horizon —
                # making TWAP duration depend on price action rather
                # than wall time. Counter only ticks after seeding
                # (``twap_slices_remaining is not None``); pre-seeded
                # bars (``LIMIT`` waiting for a first cross) don't tick.
                if (
                    req.unfilled_policy == UnfilledPolicy.TWAP_N
                    and po.twap_slices_remaining is not None
                ):
                    new_slices_remaining = po.twap_slices_remaining - 1
                    was_filled = po.cumulative_filled_qty > 0
                    if new_slices_remaining <= 0:
                        self.order_book.remove(po.order_id, was_filled=was_filled)
                    else:
                        self.order_book.requeue(
                            po.order_id,
                            new_remaining_qty=po.remaining_qty,
                            new_submitted_at=bar.timestamp,
                            twap_slices_remaining=new_slices_remaining,
                            was_filled=was_filled,
                        )
                continue

            # Parent-side look-ahead guard: any triggered order must belong
            # to a strictly-earlier bar than the one we're filling against.
            self.bar_safety.check_fill(
                order_id=po.order_id,
                submitted_at=po.submitted_at,
                fill_bar_timestamp=bar.timestamp,
            )

            # ``existing_pos`` already fetched above for the stale-
            # continuation guard; reuse it. Within a single iteration
            # of this loop no other order has run, so portfolio state
            # hasn't changed since the early lookup.

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
        # ``po.remaining_qty`` equals ``req.qty`` on the genuine first slice
        # (set by ``OrderBook.submit``). It can also equal ``req.qty`` on a
        # *re-attempt* after a prior bar's zero-fill rejection requeued the
        # full request — and that requeue may have seeded
        # ``twap_slices_remaining``, in which case TWAP slicing must apply
        # here too (the order is still routed through ``_fill_entry`` rather
        # than ``_continue_entry`` because no position has opened yet).
        # ``_twap_slice_target`` is a no-op for non-TWAP orders and for the
        # genuine first slice (``twap_slices_remaining is None``), so the
        # math is unchanged on those paths.
        target_qty = self._twap_slice_target(po)
        qty_fraction = max(0.0, min(1.0, terms.qty_fraction))
        filled_qty = target_qty * qty_fraction
        # ``unfilled`` is the order-level outstanding (what subsequent
        # bars / handler decisions see). For TWAP intermediate slices it
        # includes intentional deferral (``po.remaining_qty - target_qty``)
        # plus any cap-clip on this bar's slice. ``cap_clipped_qty`` is
        # just the bar-level liquidity failure on this slice's target —
        # used below for the position-level ``participation_clipped`` and
        # ``total_unfilled_qty`` accounting so a normally-progressing TWAP
        # schedule isn't reported as a liquidity shortfall.
        unfilled = po.remaining_qty - filled_qty
        cap_clipped_qty = target_qty - filled_qty
        dp = 4 if ref_price < 10 else 2

        if filled_qty <= 0:
            # No silent drop: emit a REJECTED Fill so the strategy sees the
            # outcome. Route through ``_handle_entry_remainder`` so the
            # order's ``unfilled_policy`` is honored — REQUEUE_NEXT_BAR
            # gives the next bar a chance to recover instead of permanently
            # dropping after a single no-liquidity bar. ``was_filled=False``
            # because no position has opened yet (initial slice rejected),
            # so the parent shouldn't remain registered as bracket-eligible.
            self._handle_entry_remainder(po, bar, po.remaining_qty, was_filled=False)
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
                unfilled_qty=po.remaining_qty,
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
            # ``original_qty`` tracks the cumulative entry-filled qty (the
            # qty we expect to exit to fully close), *not* the strategy's
            # original request. Initialized to this slice's filled qty;
            # ``_continue_entry`` bumps it on each follow-on fill. When the
            # entry order is removed (DROP / full fill) ``original_qty``
            # naturally settles at the actually-held qty so a subsequent
            # exit for that qty hits ``is_closed``.
            original_qty=filled_qty,
            # ``participation_clipped`` and ``total_unfilled_qty`` track
            # bar-level liquidity failures only — the cap-clipped portion
            # of *this slice's target*. Intentional TWAP deferral
            # (``unfilled`` minus ``cap_clipped_qty``) is scheduled work,
            # not a shortfall, and must not pollute these metrics.
            participation_clipped=cap_clipped_qty > 0,
            total_unfilled_qty=cap_clipped_qty,
            # Counts the number of fill events on the entry side: initial
            # fill = 1, every ``REQUEUE_NEXT_BAR`` continuation += 1. Exit
            # slices don't bump this counter (see ``Position.reduce``).
            partial_fill_count=1,
        )
        self.portfolio.open(pos)
        # Bind the order to the position it just opened so subsequent
        # bars can detect "this is a stale continuation against a
        # *different* position with the same symbol" — see the
        # stale-continuation guard in ``process_bar``.
        po.working_against_entry_order_id = po.order_id
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
        # ``target_qty`` is what we *try* to fill on this bar — equal to
        # ``po.remaining_qty`` for non-TWAP orders, sliced to
        # ``remaining/slices_remaining`` on TWAP_N bars. ``filled_qty``
        # always honors ``terms.qty_fraction`` so custom execution
        # models that use it for hard liquidity constraints (rather
        # than just the participation cap) aren't overruled. ``unfilled``
        # is the order-level outstanding (drives the handler's requeue
        # / drop decision); ``cap_clipped_qty`` is just *this slice's*
        # liquidity failure (drives the position-level
        # ``participation_clipped`` / ``total_unfilled_qty`` accounting,
        # so a normal TWAP schedule isn't reported as a shortfall).
        target_qty = self._twap_slice_target(po)
        qty_fraction = max(0.0, min(1.0, terms.qty_fraction))
        filled_qty = target_qty * qty_fraction
        unfilled = po.remaining_qty - filled_qty
        cap_clipped_qty = target_qty - filled_qty
        dp = 4 if ref_price < 10 else 2

        if filled_qty <= 0:
            # Zero-fraction continuation — route through
            # ``_handle_entry_remainder`` so the order's
            # ``unfilled_policy`` is honored (e.g. ``REQUEUE_NEXT_BAR``
            # gives the next bar a chance to recover; previously this
            # branch unconditionally dropped, ignoring the policy).
            # ``was_filled=True`` because the parent's first slice
            # already opened a position — preserves bracket-attachment
            # eligibility on the eventual ``remove`` path.
            self._handle_entry_remainder(po, bar, po.remaining_qty, was_filled=True)
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
                unfilled_qty=po.remaining_qty,
                cumulative_filled_qty=po.cumulative_filled_qty,
            )

        slip_long_entry, _, slip_short_entry, _ = self._slippage_multipliers(terms.extra_slip_bps)
        if req.side == OrderSide.LONG:
            fill_price = round(ref_price * slip_long_entry, dp)
        else:
            fill_price = round(ref_price * slip_short_entry, dp)

        # Re-apply the risk gate on every continuation slice. Exposure can
        # have grown between bars (mark-to-market on other positions, fresh
        # entries on different symbols) so an originally-approved order
        # still needs to fit current limits before adding more shares to the
        # position. Exclude this symbol's existing position from the snapshot
        # so ``max_open_positions`` doesn't trip on our own presence, and
        # pass the post-extend full notional so leverage / concentration
        # checks see the exposure that *will* exist after this fill.
        existing_pos = self.portfolio.positions[req.symbol]
        post_extend_notional = (existing_pos.qty + filled_qty) * fill_price
        positions_excluding_self = {
            s: p for s, p in self.portfolio.positions.items() if s != req.symbol
        }
        equity = self.portfolio.mark_to_market()
        gate = self.risk.can_enter(
            req.symbol, post_extend_notional, equity, positions_excluding_self
        )
        if not gate.allowed:
            logger.info("risk gate rejected entry continuation for %s: %s", req.symbol, gate.reason)
            # ``was_filled=True`` — the parent already opened a position on
            # the first slice (we wouldn't be in ``_continue_entry`` otherwise).
            # Removing with the default ``False`` would evict the parent id
            # from ``OrderBook``'s eligible-parent set and break any later
            # ``submit_attached`` call against this parent.
            self.order_book.remove(po.order_id, was_filled=True)
            return None

        # Capital check against the *additional* notional only — the existing
        # position's capital is already deducted.
        additional_notional = filled_qty * fill_price
        if self.portfolio.capital < additional_notional:
            logger.info(
                "insufficient capital for %s entry continuation: need %.2f, have %.2f",
                req.symbol,
                additional_notional,
                self.portfolio.capital,
            )
            # Same reasoning as the risk-gate branch above — preserve the
            # parent's eligible-parent registration since the first slice
            # already filled.
            self.order_book.remove(po.order_id, was_filled=True)
            return None

        pos = self.portfolio.extend(req.symbol, filled_qty, fill_price, ref_price)
        # ``original_qty`` mirrors the cumulative entry-filled qty; bump it
        # so ``is_closed`` (compares ``cumulative_exit_qty`` to it) and
        # ``TradeRecord.shares`` reflect the actually-held position.
        pos.original_qty += filled_qty
        # ``is_partial`` (order-level) drives the Fill's ``fill_kind``
        # below; ``cap_clipped_qty`` (bar-level) drives the position
        # metadata so an intentional TWAP under-fill doesn't get
        # reported as a liquidity shortfall.
        is_partial = unfilled > 0
        if cap_clipped_qty > 0:
            pos.participation_clipped = True
        pos.total_unfilled_qty += cap_clipped_qty
        pos.partial_fill_count += 1

        # Capture the cumulative-fill total *before* the remainder handler
        # runs: a requeue (REQUEUE_NEXT_BAR or TWAP_N) updates
        # ``po.cumulative_filled_qty`` to ``original_qty - new_remaining_qty``
        # — i.e. the post-fill cumulative. Reading ``po.cumulative_filled_qty
        # + filled_qty`` after the requeue would double-count this slice.
        fill_cumulative_qty = po.cumulative_filled_qty + filled_qty
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
            # Per-order cumulative entry fills (monotonic across all slices
            # of *this* order).
            cumulative_filled_qty=fill_cumulative_qty,
        )

    @staticmethod
    def _twap_slice_target(po: PendingOrder) -> float:
        """Return this bar's target qty for an entry/exit slice.

        Non-TWAP orders return ``po.remaining_qty`` so the per-bar math
        is unchanged. For ``TWAP_N``: the first slice (counter still
        ``None``) targets the full request so the participation cap
        drives a natural first-bar partial; intermediate bars target
        ``remaining / slices_remaining``; the terminal slice
        (``slices_remaining <= 1``) targets the full remainder.
        ``terms.qty_fraction`` is always honored on top — the issue
        offers two terminal-slice options ("force-flush regardless of
        cap" vs. "clip to cap and re-route") and we pick the latter so
        custom execution models that use ``qty_fraction`` for hard
        liquidity constraints (rather than just the participation cap)
        aren't overruled. Any residual the model couldn't fill on the
        terminal slice drops cleanly via the ``slices_remaining <= 0``
        branch in ``_handle_entry_remainder`` /
        ``_handle_exit_remainder``.
        """
        req = po.request
        if req.unfilled_policy != UnfilledPolicy.TWAP_N:
            return po.remaining_qty
        sr = po.twap_slices_remaining
        if sr is None or sr <= 1:
            return po.remaining_qty
        return po.remaining_qty / sr

    def _handle_entry_remainder(
        self,
        po: PendingOrder,
        bar: Bar,
        unfilled: float,
        *,
        was_filled: bool = True,
    ) -> None:
        """Decide whether to requeue or remove an entry order's remainder.

        ``was_filled`` controls whether the parent stays registered in
        ``OrderBook``'s eligible-parent set (only consequential when the
        order was submitted with ``expect_brackets=True``). The default
        ``True`` is correct for the typical path where at least one slice
        already filled — including the no-liquidity *continuation* case,
        because the partial entry that triggered the continuation has
        already opened a position. For the no-liquidity *initial* case
        (``_fill_entry`` with ``filled_qty <= 0``) the caller passes
        ``False`` since no position has opened.
        """
        policy = po.request.unfilled_policy or UnfilledPolicy.DROP
        if unfilled > 0 and policy == UnfilledPolicy.REQUEUE_NEXT_BAR:
            self.order_book.requeue(
                po.order_id,
                new_remaining_qty=unfilled,
                new_submitted_at=bar.timestamp,
                was_filled=was_filled,
            )
            return
        if unfilled > 0 and policy == UnfilledPolicy.TWAP_N:
            # Seed on the first fill, decrement on every continuation. The
            # final slice (``slices_remaining == 1`` entering the bar) is
            # force-flushed in ``_continue_entry``, so reaching this branch
            # with ``new_slices_remaining <= 0`` means even the cap-bypass
            # couldn't clear the order (e.g. zero-volume bar). Drop cleanly
            # so the order doesn't linger past its TWAP horizon.
            if po.twap_slices_remaining is None:
                new_slices_remaining = (po.request.twap_slices or 0) - 1
            else:
                new_slices_remaining = po.twap_slices_remaining - 1
            if new_slices_remaining <= 0:
                self.order_book.remove(po.order_id, was_filled=was_filled)
                return
            self.order_book.requeue(
                po.order_id,
                new_remaining_qty=unfilled,
                new_submitted_at=bar.timestamp,
                twap_slices_remaining=new_slices_remaining,
                was_filled=was_filled,
            )
            return
        self.order_book.remove(po.order_id, was_filled=was_filled)

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
        # Bind the order to the position it's targeting on the first
        # slice (before any cap-clip / portfolio mutation) so subsequent
        # bars can detect "this position has been closed and replaced by
        # a different one" — see the stale-continuation guard in
        # ``process_bar``. Idempotent if already set.
        if po.working_against_entry_order_id is None:
            po.working_against_entry_order_id = pos.entry_order_id
        ref_price = terms.reference_price
        dp = 4 if ref_price < 10 else 2
        _, slip_long_exit, _, slip_short_exit = self._slippage_multipliers(terms.extra_slip_bps)
        if pos.side == OrderSide.LONG:
            exit_price = round(ref_price * slip_long_exit, dp)
        else:
            exit_price = round(ref_price * slip_short_exit, dp)

        # Bound the exit fill by the strategy's requested qty (or the
        # TWAP slice target on a sliced exit), and by the position's
        # currently-open qty. The position cap matters when an entry
        # continuation runs earlier in the same bar and grows the
        # position past what the strategy saw at submission time —
        # without this min() the exit could close newly-added shares
        # the strategy never intended to unwind. ``_twap_slice_target``
        # is a no-op for non-TWAP exits (returns ``po.remaining_qty``).
        # ``terms.qty_fraction`` is always honored (no force-flush)
        # so custom execution models that use it for hard liquidity
        # constraints aren't overruled — see ``_twap_slice_target``.
        target_qty = self._twap_slice_target(po)
        fillable_qty = min(target_qty, pos.qty)
        qty_fraction = max(0.0, min(1.0, terms.qty_fraction))
        filled_qty = fillable_qty * qty_fraction
        # Compute ``unfilled`` against the *order's* remaining request, not
        # against ``fillable_qty``. If the strategy asked for more shares
        # than are currently open (e.g. after a dropped partial entry), the
        # not-fillable portion is real unfilled work from the strategy's
        # perspective — reporting it as ``unfilled_qty=0`` would mislabel a
        # truncated execution as fully complete and break resubmission /
        # exposure-reconciliation logic. For TWAP_N, ``unfilled`` also
        # includes the intentional under-fill from this bar's slice target,
        # which the requeue machinery carries forward to subsequent slices.
        unfilled = po.remaining_qty - filled_qty

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
                unfilled_qty=po.remaining_qty,
                cumulative_filled_qty=po.cumulative_filled_qty,
            )
            return rejected, None

        self.portfolio.partial_close(bar.symbol, filled_qty, exit_price, ref_price)
        # Only the participation-cap-clipped portion (``fillable_qty -
        # filled_qty``) accumulates into ``pos.total_unfilled_qty`` — the
        # over-ask portion (``po.remaining_qty - fillable_qty``) is a
        # property of the strategy's request size relative to the live
        # position, not a bar-level liquidity event, and the same ghost
        # over-ask would re-appear on every requeued slice. Adding it
        # bar-after-bar inflated ``TradeRecord.total_unfilled_qty`` past
        # the original_qty (e.g. a 2000-share over-ask against a
        # 1000-share position could report > 2000 unfilled).
        cap_clipped = fillable_qty - filled_qty
        if cap_clipped > 0:
            pos.participation_clipped = True
            pos.total_unfilled_qty += cap_clipped

        is_closed = pos.is_closed
        # ``fill_kind`` reports completeness of *this exit order*, not
        # closure of the position: a strategy that intentionally requests a
        # partial unwind (req.qty < pos.qty) gets a ``FULL`` exit Fill
        # while the position keeps residual exposure. Conflating the two
        # would mislabel partial-unwind fills as ``PARTIAL`` even when the
        # order filled to its requested size.
        exit_fill = Fill(
            order_id=po.order_id,
            client_order_id=req.client_order_id,
            symbol=pos.symbol,
            side=req.side,
            qty=filled_qty,
            price=exit_price,
            timestamp=bar.timestamp,
            reason="exit",
            fill_kind=FillKind.FULL if unfilled <= 0 else FillKind.PARTIAL,
            unfilled_qty=unfilled,
            # Per-order cumulative exit fills (monotonic across all slices
            # of *this* exit order). ``pos.cumulative_exit_qty`` is
            # position-wide across multiple exit orders, which would let an
            # independent later exit emit a value larger than its own
            # requested qty — breaking per-order fill accounting.
            cumulative_filled_qty=po.cumulative_filled_qty + filled_qty,
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
            # Use the qty-weighted exit bid across all partial-exit slices
            # so the reference price stays coherent with the weighted
            # ``exit_price``. For single-bar full closes this collapses to
            # the bar's own ref price (matches legacy behavior).
            exit_bid_price=round(pos.weighted_avg_exit_bid_price, dp),
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
        docstring at ``order_book.OrderBook.requeue``. Mirrors the
        entry-side handler's TWAP_N branch: contract-level
        ``unfilled_policy`` is order-agnostic (entry vs exit is determined
        at fill-time, not submission), so accepting TWAP_N for any order
        means honoring it on both sides.
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
        if unfilled > 0 and policy == UnfilledPolicy.TWAP_N:
            if po.twap_slices_remaining is None:
                new_slices_remaining = (po.request.twap_slices or 0) - 1
            else:
                new_slices_remaining = po.twap_slices_remaining - 1
            if new_slices_remaining <= 0:
                self.order_book.remove(po.order_id)
                return
            self.order_book.requeue(
                po.order_id,
                new_remaining_qty=unfilled,
                new_submitted_at=bar.timestamp,
                twap_slices_remaining=new_slices_remaining,
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
