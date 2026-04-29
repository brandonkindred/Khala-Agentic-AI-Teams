"""Pending-order book.

The ``OrderBook`` is the authoritative record of *accepted* strategy orders.
Strategy-emitted ``OrderRequest`` records come in through
:meth:`OrderBook.submit`, get gated by the existing
:class:`~investment_team.execution.risk_filter.RiskFilter`, receive an engine
``order_id``, and sit here until the :class:`FillSimulator` decides to fill
them (or they expire by TIF, or the strategy cancels them).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, NamedTuple, Optional

from ..strategy.contract import OrderRequest, OrderSide, OrderType, TimeInForce


class _TopLevelMeta(NamedTuple):
    """Metadata cached for an eligible bracket-parent id: its symbol and side
    so ``submit_attached`` can validate that bracket children share the
    parent's symbol and take the opposite side (protective legs close out
    the parent's position).
    """

    symbol: str
    side: OrderSide


# Relative tolerance for the float-math residual clamp in
# ``OrderBook.requeue``. A remainder smaller than
# ``po.original_qty * FILL_QTY_REL_TOL`` is treated as exactly 0 because
# that's well below ULP-level error for the upstream sizing operands
# (machine epsilon ≈ 2.2e-16; even after dozens of sequential subtractions
# on operands of size N, accumulated ULP stays well under N * 1e-13).
# A *relative* threshold avoids over-clamping legitimately small orders
# (fractional-token / per-pip-quote venues, etc.) — a sub-epsilon-sized
# absolute residual that's a meaningful fraction of an even smaller order
# is real, not noise.
FILL_QTY_REL_TOL = 1e-12


@dataclass
class PendingOrder:
    order_id: str
    request: OrderRequest
    submitted_at: str  # bar timestamp anchor for the look-ahead guard; advanced
    # by ``OrderBook.requeue`` so the simulator doesn't re-fill on the same bar.
    submitted_equity: float  # for audit / risk-recheck if needed
    original_submitted_at: str = ""  # immutable original submission timestamp
    # — used by ``expire_day_orders`` so a partially-filled-and-requeued DAY
    # order still expires at the end of its original session, not the day after
    # the last partial fill. Set in ``submit`` / ``submit_attached``.
    original_qty: float = 0.0  # immutable, set at submit (overridden by submit())
    rejection_reason: Optional[str] = None  # set if the book rejects at submit-time
    cumulative_filled_qty: float = 0.0
    remaining_qty: float = 0.0  # initialized to request.qty in submit()
    twap_slices_remaining: Optional[int] = None
    effective_stop_price: Optional[float] = None  # live trailing stop (Step 8 / #390)
    trailing_water: Optional[float] = None  # running high (LONG) / low (SHORT); Step 8
    armed: bool = True


@dataclass
class OrderBook:
    _next_id: int = 0
    _pending: Dict[str, PendingOrder] = field(default_factory=dict)
    # Parallel index by symbol/side so fill_simulator doesn't have to scan.
    _by_symbol: Dict[str, List[str]] = field(default_factory=dict)
    # Map of *eligible-parent* top-level order id → (symbol, side). Used by
    # ``submit_attached`` to reject children whose ``parent_order_id`` is not
    # a real top-level order eligible to carry brackets, to verify the child's
    # symbol matches, and to verify the child takes the opposite side.
    # Lifecycle:
    #
    # - Inserted by ``submit()`` *only when the caller passes*
    #   ``expect_brackets=True`` (strategies that don't use brackets pay no
    #   memory cost — non-bracket entries never enter this set).
    # - Discarded on ``cancel()`` and ``expire_day_orders()`` for top-level
    #   orders (cancelled / expired parents never opened, so attaching
    #   protective children to them would be a bug).
    # - Preserved on ``remove(was_filled=True)`` (the terminal-fill path
    #   used by ``requeue(... new_remaining_qty=0)`` and the simulator's
    #   entry-fill path): a filled entry is the normal case where bracket
    #   children are activated *after* the entry fills and is removed.
    # - Auto-evicted by ``_maybe_evict_resolved_parent`` once a filled
    #   parent has no remaining pending children.
    #
    # ``submit_attached`` is never inserted here, so an attached child can
    # never be promoted into a parent (multi-level bracket trees rejected).
    _known_top_level_order_ids: Dict[str, _TopLevelMeta] = field(default_factory=dict)

    # ------------------------------------------------------------------

    def submit(
        self,
        request: OrderRequest,
        *,
        submitted_at: str,
        submitted_equity: float,
        expect_brackets: bool = False,
    ) -> PendingOrder:
        """Submit a top-level order to the book.

        ``expect_brackets`` (default ``False``) is the strategy's declaration
        that one or more bracket / OCO children may be attached against this
        order's id later via :meth:`submit_attached`. Only when ``True`` does
        the order's id register in ``_known_top_level_order_ids`` — the
        eligible-parent set ``submit_attached`` validates against. Non-
        bracket strategies (the majority) get *zero* memory overhead from
        the eligible-parent tracking; bracket strategies opt in explicitly,
        which also makes intent visible at the call site.
        """
        # Defense in depth — strategy-side ``validate_prices()`` already rejects
        # both fields, but ``submit()`` is the gateway that registers an id as
        # an *eligible bracket parent*. Refusing child-shaped requests here
        # prevents an internal caller that bypasses ``validate_prices`` from
        # smuggling an attached order into the eligible-parent set and creating
        # multi-level bracket trees.
        if request.parent_order_id is not None or request.oco_group_id is not None:
            raise ValueError(
                f"submit() must not receive a request with parent_order_id or "
                f"oco_group_id set (got parent_order_id={request.parent_order_id!r}, "
                f"oco_group_id={request.oco_group_id!r}); use submit_attached() "
                f"for bracket / OCO children"
            )
        self._next_id += 1
        order_id = f"o{self._next_id}"
        po = PendingOrder(
            order_id=order_id,
            request=request,
            submitted_at=submitted_at,
            submitted_equity=submitted_equity,
            original_submitted_at=submitted_at,
            original_qty=request.qty,
            remaining_qty=request.qty,
        )
        self._pending[order_id] = po
        if expect_brackets:
            self._known_top_level_order_ids[order_id] = _TopLevelMeta(
                symbol=request.symbol, side=request.side
            )
        self._by_symbol.setdefault(request.symbol, []).append(order_id)
        return po

    def submit_attached(
        self,
        request: OrderRequest,
        *,
        submitted_at: str,
        submitted_equity: float,
        parent_order_id: str,
        oco_group_id: str,
    ) -> PendingOrder:
        """Submit a bracket / OCO child.

        The point of this entry is to allow ``parent_order_id`` /
        ``oco_group_id`` to be set on the queued order — the strategy-side
        ``validate_prices()`` gate refuses both fields outright (they are
        engine-internal). All *other* runtime-support gates and shape-consistency
        checks (LIMIT requires ``limit_price``, STOP requires ``stop_price``,
        no TRAILING_STOP / IOC / FOK / unfilled_policy until their respective
        steps ship, no nested attachments) still apply; we re-run
        ``validate_prices`` on a clone with parent/OCO cleared so we don't
        re-fire the very gates this method is meant to bypass.
        """
        if not isinstance(parent_order_id, str) or not parent_order_id:
            raise TypeError(
                f"submit_attached parent_order_id must be a non-empty str, got {parent_order_id!r}"
            )
        if not isinstance(oco_group_id, str) or not oco_group_id:
            raise TypeError(
                f"submit_attached oco_group_id must be a non-empty str, got {oco_group_id!r}"
            )
        # Reject orphan children whose parent_order_id was never allocated as
        # a *top-level* order by this book. We check the all-time set rather
        # than ``_pending`` so a child submitted after the parent fills (the
        # typical bracket activation flow) still works. Children of children
        # are forbidden so the API stays as flat parent/child legs — multi-
        # level bracket trees would break ``children_of`` traversal and OCO
        # cancellation semantics.
        parent_meta = self._known_top_level_order_ids.get(parent_order_id)
        if parent_meta is None:
            raise ValueError(
                f"submit_attached parent_order_id {parent_order_id!r} is not a known "
                f"top-level order id; submit the parent through OrderBook.submit() first "
                f"(attached children may not themselves be parents of further children)"
            )
        # Bracket children must trade the same symbol as the parent. A typo
        # (parent on AAA, child on BBB) would otherwise be accepted and then
        # routed under the child's symbol while still tagged with the parent /
        # OCO ids — corrupting bracket scoping and producing unrelated fills.
        if request.symbol != parent_meta.symbol:
            raise ValueError(
                f"submit_attached child symbol {request.symbol!r} does not match parent "
                f"{parent_order_id!r} symbol {parent_meta.symbol!r}"
            )
        # Protective legs close out the parent's position, so they must take
        # the *opposite* side. Otherwise the simulator's same-side path would
        # silently drop the order at fill time (treating it as an attempted
        # add-on), leaving the bracket leg unprotected and the position
        # exposed to runaway moves.
        if request.side == parent_meta.side:
            raise ValueError(
                f"submit_attached child side {request.side.value!r} must be opposite "
                f"parent {parent_order_id!r} side {parent_meta.side.value!r} — "
                f"protective legs close out the parent's position"
            )
        # Bracket / OCO children are protective legs. ``MARKET`` order types
        # would fire on the very next bar and execute as a standalone position
        # change rather than acting as a take-profit / stop-loss leg, which
        # breaks bracket semantics. Forbid them here so callers get a clear
        # error rather than an unintended fill.
        if request.order_type == OrderType.MARKET:
            raise ValueError(
                "submit_attached child must be LIMIT or STOP, not MARKET — market "
                "children would fire immediately on the next bar instead of acting as "
                "protective legs (TRAILING_STOP is gated until #390 / Step 8 lands)"
            )
        attached_request = request.model_copy(
            update={"parent_order_id": parent_order_id, "oco_group_id": oco_group_id}
        )
        attached_request.model_copy(
            update={"parent_order_id": None, "oco_group_id": None}
        ).validate_prices()

        # Pre-armed bracket children: a child submitted while its parent is
        # still pending must not fire before the entry actually opens.
        # ``armed=False`` keeps it in the book but invisible to the simulator's
        # fill loop. Children submitted after the parent has filled (post-fill
        # bracket activation, the typical flow) are armed immediately. The
        # transition False → True for pre-armed children when the parent
        # eventually fills is the bracket materializer's job (#389).
        armed = parent_order_id not in self._pending

        self._next_id += 1
        order_id = f"o{self._next_id}"
        po = PendingOrder(
            order_id=order_id,
            request=attached_request,
            submitted_at=submitted_at,
            submitted_equity=submitted_equity,
            original_submitted_at=submitted_at,
            original_qty=attached_request.qty,
            remaining_qty=attached_request.qty,
            armed=armed,
        )
        self._pending[order_id] = po
        # Note: do NOT add ``order_id`` to ``_known_top_level_order_ids`` —
        # attached children must not be eligible as future parents.
        self._by_symbol.setdefault(attached_request.symbol, []).append(order_id)
        return po

    def cancel(self, order_id: str) -> bool:
        po = self._pending.pop(order_id, None)
        if po is None:
            return False
        ids = self._by_symbol.get(po.request.symbol)
        if ids and order_id in ids:
            ids.remove(order_id)
        if po.request.parent_order_id is None:
            # Cancelled top-level: ineligible to parent further children +
            # cascade to any pending TP / SL legs.
            self._known_top_level_order_ids.pop(order_id, None)
            self._cascade_cancel_children(order_id)
        else:
            # Cancelled child: the parent's bracket may now be fully
            # resolved (no pending children, parent already filled), in
            # which case the parent's id can be reclaimed from the
            # eligible-parent set.
            self._maybe_evict_resolved_parent(po.request.parent_order_id)
        return True

    def remove(self, order_id: str, *, was_filled: bool = False) -> Optional[PendingOrder]:
        """Drop an order from the book.

        Pass ``was_filled=True`` *only* when the removal represents a fill of
        an order that may carry bracket children — i.e. an *entry* fill
        (terminal-fill ``requeue(... new_remaining_qty=0)`` or
        ``FillSimulator``'s entry path). In that case the id is preserved in
        ``_known_top_level_order_ids`` so children can be activated against
        the now-filled parent. *Exit* fills don't qualify as bracket parents
        and should leave ``was_filled`` at its default.

        The default ``was_filled=False`` is the safe choice for every other
        removal path (risk-gate rejection, insufficient capital, sibling
        cancellation, expiry cascade, manual removal, exit fills). It evicts
        a top-level order's id from the eligible-parent set *and*
        cascade-cancels any pending children so non-eligible removals don't
        leave orphan bracket legs in the book.

        For child removals, both branches notify
        ``_maybe_evict_resolved_parent`` so a filled parent's id is reclaimed
        from the eligible-parent set automatically once its bracket is
        fully resolved (no pending entry, no pending children).
        """
        po = self._pending.pop(order_id, None)
        if po is None:
            return None
        ids = self._by_symbol.get(po.request.symbol)
        if ids and order_id in ids:
            ids.remove(order_id)
        if po.request.parent_order_id is None:
            if not was_filled:
                self._known_top_level_order_ids.pop(order_id, None)
                self._cascade_cancel_children(order_id)
        else:
            # Removed child: a previously-filled parent may now have no
            # remaining live legs and can be evicted automatically.
            self._maybe_evict_resolved_parent(po.request.parent_order_id)
        return po

    def _cascade_cancel_children(self, parent_id: str) -> None:
        """Remove every pending child of ``parent_id`` (non-recursive).

        Called by ``cancel`` and by ``remove(was_filled=False)`` on top-level
        orders to keep orphan protective legs (TP / SL) out of the book when
        the entry never opens. ``children_of`` only returns *direct*
        children, so ``submit_attached``'s flat-bracket invariant means we
        don't recurse.
        """
        for child in self.children_of(parent_id):
            self.remove(child.order_id)

    def _maybe_evict_resolved_parent(self, parent_id: str) -> None:
        """Auto-evict a filled-parent id from ``_known_top_level_order_ids``
        once its bracket is fully resolved.

        A filled parent stays in the eligible-parent set only because future
        ``submit_attached`` calls might still add protective children to it.
        Once the parent itself is no longer pending *and* none of its
        children remain pending either, the bracket is done — keeping the id
        around just leaks memory and lets stale ids get reused. This hook
        runs whenever a child is removed (via ``cancel`` or ``remove``) and
        is a no-op when the parent is still pending or still has live
        children.
        """
        if parent_id in self._pending:
            return
        if any(po.request.parent_order_id == parent_id for po in self._pending.values()):
            return
        self._known_top_level_order_ids.pop(parent_id, None)

    def pending_for_symbol(self, symbol: str) -> List[PendingOrder]:
        return [self._pending[oid] for oid in self._by_symbol.get(symbol, [])]

    def all_pending(self) -> List[PendingOrder]:
        return list(self._pending.values())

    def __contains__(self, order_id: object) -> bool:
        """``order_id in book`` — true iff the id is currently pending.

        Useful in iteration patterns (e.g. ``FillSimulator.process_bar``)
        that snapshot ``pending_for_symbol`` and need to skip orders that
        have been cascade-cancelled or otherwise removed mid-loop.
        """
        return order_id in self._pending

    # ------------------------------------------------------------------
    # Partial-fill requeue + bracket / OCO traversal (Trading 5/5 Step 2).
    # No engine or service caller wires these up in Step 2 — they exist for
    # Steps 4 / 7 / 8 (#386 / #389 / #390) to call.
    # ------------------------------------------------------------------

    def requeue(
        self,
        order_id: str,
        *,
        new_remaining_qty: float,
        new_submitted_at: str,
        twap_slices_remaining: Optional[int] = None,
        was_filled: bool = True,
    ) -> PendingOrder:
        """Single mutation point for a partial-fill remainder. Refreshes
        ``submitted_at`` so the look-ahead guard in
        ``execution/bar_safety.py`` still holds on the next bar, and keeps
        ``cumulative_filled_qty`` consistent with
        ``original_qty - remaining_qty`` so downstream readers (Fill records,
        backtest analytics) see correct partial-fill progress.

        Bounds: ``new_remaining_qty`` must be in ``[0, current remaining_qty]``.
        A partial fill can only shrink the remainder; growing it would imply a
        negative fill, and a negative remainder has no execution semantics.

        ``was_filled`` (default ``True``) is forwarded to ``remove()`` when the
        remainder collapses to zero (terminal fill). Default reflects the
        primary use case — a partial-fill remainder being completed on a
        top-level entry, which should preserve the parent's id in the
        eligible-parent set for bracket activation. Future callers (e.g.
        the partial-fill simulator in #386) that handle non-entry
        partial-fills (such as exits) should pass ``was_filled=False`` so
        those ids are *not* registered as eligible bracket parents.
        """
        po = self._pending.get(order_id)
        if po is None:
            # Explicit raise (not silent no-op) — partial-fill flow should never
            # call requeue on an order that's already been removed/cancelled, so
            # surface the bug rather than masking it. Unlike ``cancel``/``remove``,
            # which are idempotent removals, ``requeue`` represents an active fill
            # update against an order the caller believes is still pending.
            raise KeyError(f"requeue: order_id {order_id!r} is not in the book")
        # ``bool`` is a subclass of ``int`` (and therefore numeric) in Python,
        # so ``True``/``False`` would otherwise pass the finite + range checks
        # and silently land in ``remaining_qty`` as 1/0. That corrupts fill
        # accounting downstream, so reject explicitly.
        if isinstance(new_remaining_qty, bool):
            raise ValueError(
                f"requeue new_remaining_qty must be a real number, got bool {new_remaining_qty!r}"
            )
        # Type-check before ``math.isfinite`` so non-numeric inputs (e.g. a
        # malformed caller passing a string) raise a structured ``TypeError``
        # with a clear message instead of the raw ``TypeError`` ``math``
        # produces from inside its C implementation.
        if not isinstance(new_remaining_qty, (int, float)):
            raise TypeError(
                f"requeue new_remaining_qty must be int or float, "
                f"got {type(new_remaining_qty).__name__} {new_remaining_qty!r}"
            )
        if not math.isfinite(new_remaining_qty):
            raise ValueError(f"requeue new_remaining_qty must be finite, got {new_remaining_qty!r}")
        if not isinstance(new_submitted_at, str):
            raise TypeError(
                f"requeue new_submitted_at must be a str, "
                f"got {type(new_submitted_at).__name__} {new_submitted_at!r}"
            )
        # Compare both sides as parsed datetimes when possible, falling back
        # to normalised string compare when one side isn't ISO 8601. Otherwise
        # equivalent ISO 8601 instants in different timezone offsets (e.g.
        # ``+05:30`` vs ``+00:00``) would falsely reject as a regression.
        if _ts_lt(new_submitted_at, po.submitted_at):
            raise ValueError(
                f"requeue submitted_at must not regress: "
                f"old={po.submitted_at!r} new={new_submitted_at!r}"
            )
        # Clamp sub-epsilon residuals (positive *or* negative) to exactly
        # zero before the bounds checks so that a tiny remainder produced by
        # float math (e.g. ``prev - filled`` accumulating ULP error) is
        # treated as a terminal fill rather than lingering in the book. The
        # threshold is *relative* to the order's own size so legitimately
        # small orders (e.g. fractional-token venues where every quantity
        # is sub-epsilon in absolute terms) don't have real partial-fill
        # remainders silently swallowed. ``original_qty <= 0`` falls into
        # the ``threshold == 0`` branch and never clamps — we never produce
        # a zero-qty top-level order in normal flow, but be defensive.
        clamp_threshold = po.original_qty * FILL_QTY_REL_TOL
        if clamp_threshold > 0 and abs(new_remaining_qty) < clamp_threshold:
            new_remaining_qty = 0.0
        if new_remaining_qty < 0:
            raise ValueError(f"requeue new_remaining_qty must be >= 0, got {new_remaining_qty!r}")
        if new_remaining_qty > po.remaining_qty:
            raise ValueError(
                f"requeue new_remaining_qty must not exceed current remaining_qty: "
                f"current={po.remaining_qty!r} new={new_remaining_qty!r}"
            )
        # TWAP slice counters are discrete and non-negative. ``bool`` is a
        # subclass of ``int`` in Python, so we reject it explicitly to avoid
        # ``True``/``False`` being silently accepted as 1/0 — TWAP scheduling
        # treats slice counts as integers and a bool there indicates caller
        # confusion.
        if twap_slices_remaining is not None and (
            isinstance(twap_slices_remaining, bool)
            or not isinstance(twap_slices_remaining, int)
            or twap_slices_remaining < 0
        ):
            raise ValueError(
                f"requeue twap_slices_remaining must be a non-negative int or None, "
                f"got {twap_slices_remaining!r}"
            )
        # Store ``new_submitted_at`` verbatim. The downstream look-ahead
        # guard in ``execution/bar_safety.py`` does its own chronological
        # parse-and-compare, so we don't need to canonicalise here —
        # canonicalising would create a different format from the bar
        # timestamps the guard compares against (``Z``-suffixed bars vs
        # canonical ``+00:00`` storage), reintroducing the same lexicographic
        # mis-comparison this PR's chronological-compare path is designed to
        # fix.
        po.submitted_at = new_submitted_at
        po.remaining_qty = new_remaining_qty
        po.cumulative_filled_qty = po.original_qty - new_remaining_qty
        po.twap_slices_remaining = twap_slices_remaining
        if new_remaining_qty == 0:
            # Fully filled — drop from the book so downstream consumers (notably
            # FillSimulator, which still sizes from req.qty) can't re-fill it.
            # ``was_filled`` is forwarded so an entry-style terminal fill
            # (default ``True``) preserves the parent's id for bracket
            # activation, while exit-style partial fills (caller passes
            # ``False``) correctly evict from the eligible-parent set.
            self.remove(order_id, was_filled=was_filled)
        return po

    def oco_cancel_siblings(
        self,
        oco_group_id: str,
        *,
        except_order_id: str,
        parent_order_id: str,
    ) -> List[str]:
        """Remove every pending order tagged with ``oco_group_id`` whose
        ``parent_order_id`` matches the surviving leg's parent, other than
        ``except_order_id``. Returns the list of cancelled order ids.

        Scoping by ``parent_order_id`` prevents two independent brackets that
        happen to reuse the same ``oco_group_id`` from cross-cancelling each
        other's protective legs.

        All three id arguments must be non-empty strings. ``OrderRequest``
        stores ``oco_group_id`` and ``parent_order_id`` as ``Optional[str]``
        with default ``None``, so calling with ``None`` here would
        equality-match every ordinary parent order in the book and nuke
        unrelated orders. Reject defensively.

        Ordering contract: the caller must pass ``except_order_id`` *while
        the surviving leg is still in the book*, before finalizing it
        (e.g., before a terminal-fill ``requeue(... new_remaining_qty=0)``
        auto-removes it). The method validates that ``except_order_id`` is
        currently pending and matches the ``(oco_group_id, parent_order_id)``
        tuple — otherwise a stale or mistyped value would slip through and
        cancel *every* leg in the group, leaving the bracket unprotected.
        """
        for name, value in (
            ("oco_group_id", oco_group_id),
            ("except_order_id", except_order_id),
            ("parent_order_id", parent_order_id),
        ):
            if not isinstance(value, str) or not value:
                raise TypeError(
                    f"oco_cancel_siblings {name} must be a non-empty str, got {value!r}"
                )
        survivor = self._pending.get(except_order_id)
        if survivor is None:
            raise ValueError(
                f"oco_cancel_siblings except_order_id {except_order_id!r} is not currently "
                f"pending; cancel siblings before removing the surviving leg from the book"
            )
        if (
            survivor.request.oco_group_id != oco_group_id
            or survivor.request.parent_order_id != parent_order_id
        ):
            raise ValueError(
                f"oco_cancel_siblings except_order_id {except_order_id!r} does not belong to "
                f"the ({oco_group_id!r}, {parent_order_id!r}) bracket "
                f"(actual: oco_group_id={survivor.request.oco_group_id!r}, "
                f"parent_order_id={survivor.request.parent_order_id!r})"
            )
        cancelled: List[str] = []
        for oid in list(self._pending.keys()):
            po = self._pending[oid]
            if (
                po.request.oco_group_id == oco_group_id
                and po.request.parent_order_id == parent_order_id
                and oid != except_order_id
            ):
                self.remove(oid)
                cancelled.append(oid)
        return cancelled

    def children_of(self, parent_order_id: str) -> List[PendingOrder]:
        """Direct children of ``parent_order_id`` (no recursion).

        Requires a non-empty string. ``OrderRequest.parent_order_id`` defaults
        to ``None`` for non-bracket orders, so calling with ``None`` would
        equality-match every top-level order in the book and cause callers to
        treat unrelated entries as bracket children.
        """
        if not isinstance(parent_order_id, str) or not parent_order_id:
            raise TypeError(
                f"children_of parent_order_id must be a non-empty str, got {parent_order_id!r}"
            )
        return [
            po for po in self._pending.values() if po.request.parent_order_id == parent_order_id
        ]

    def prune_known_top_level_order_ids(self) -> int:
        """Reclaim memory from ``_known_top_level_order_ids``.

        Removes any id whose top-level order is no longer in ``_pending``
        AND has no children currently in ``_pending``. Subsequent
        ``submit_attached`` calls referencing those ids will fail —
        correctly, because the bracket is fully resolved.

        Long-running services (paper-trade, live) should call this
        periodically to bound memory; it's a no-op for single-shot
        backtests where the OrderBook is discarded after the run.

        Returns the number of ids pruned.
        """
        # Active ids = currently-pending top-level orders + parents
        # referenced by any pending child.
        active: set[str] = set()
        for po in self._pending.values():
            if po.request.parent_order_id is None:
                active.add(po.order_id)
            else:
                active.add(po.request.parent_order_id)
        prunable = [oid for oid in self._known_top_level_order_ids if oid not in active]
        for oid in prunable:
            self._known_top_level_order_ids.pop(oid, None)
        return len(prunable)

    # ------------------------------------------------------------------
    # TIF expiry — callers pass the current bar timestamp; day orders
    # submitted on a strictly-earlier date are removed.
    # ------------------------------------------------------------------

    def expire_day_orders(self, current_date: str) -> List[PendingOrder]:
        """Expire DAY-TIF orders whose original submission date is strictly
        earlier than ``current_date``. Top-level expirations cascade-cancel
        any pending attached children via ``remove()``'s default
        (``was_filled=False``) lifecycle path so we don't leave orphan
        protective legs in the book.
        """
        expired: List[PendingOrder] = []
        for oid in list(self._pending.keys()):
            po = self._pending.get(oid)
            if po is None:
                # Already cascade-cancelled by an earlier parent in this loop.
                continue
            if po.request.tif != TimeInForce.DAY:
                continue
            # Anchor the expiry decision on ``original_submitted_at`` (immutable)
            # rather than ``submitted_at`` (which ``requeue`` advances on every
            # partial fill). Otherwise a DAY order partially filled on day D and
            # requeued to D+1 would only expire on D+2 — surviving an extra
            # session and breaking TIF semantics.
            anchor = po.original_submitted_at or po.submitted_at
            # Compare date prefix only so intraday timestamps also work.
            if _date_only(anchor) < _date_only(current_date):
                expired.append(po)
                # ``remove(was_filled=False)`` evicts the parent from the
                # eligible-parent set *and* cascades to any pending children.
                self.remove(oid)
        return expired


def _date_only(ts: str) -> str:
    return (ts or "")[:10]


_COMPACT_OFFSET_RE = re.compile(r"([+-])(\d{2})(\d{2})$")


def _normalize_ts(ts: str) -> str:
    """Best-effort ISO 8601 input normaliser used as both a pre-parse step
    (so ``datetime.fromisoformat`` accepts variants Python < 3.11 doesn't)
    and the fallback string-compare value for unparseable inputs.

    Handles two non-canonical forms:

    - Trailing ``Z`` (UTC zulu suffix) → ``+00:00``.
    - Compact offsets ``±HHMM`` (no colon) → ``±HH:MM``. Without this,
      values like ``+0000`` or ``+0530`` fail ``fromisoformat`` on Python
      3.10 and force ``_ts_lt`` into the lexicographic fallback.

    Anything else is returned unchanged.
    """
    if not isinstance(ts, str):
        return ts
    if ts.endswith("Z"):
        return ts[:-1] + "+00:00"
    m = _COMPACT_OFFSET_RE.search(ts)
    if m:
        return ts[: m.start()] + f"{m.group(1)}{m.group(2)}:{m.group(3)}"
    return ts


def _try_parse_ts(ts: str) -> Optional[datetime]:
    """Best-effort ISO 8601 parse. Returns ``None`` if the value isn't a
    string or doesn't parse cleanly — callers fall back to string compare.
    """
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(_normalize_ts(ts))
    except ValueError:
        return None


def _ts_lt(new_ts: str, existing_ts: str) -> bool:
    """True iff ``new_ts`` is *strictly* earlier than ``existing_ts``.

    Parses both as ISO 8601 datetimes when possible and compares them
    chronologically — so equivalent instants in different timezone offsets
    (e.g. ``2024-01-02T10:00:00+05:30`` vs ``2024-01-02T04:30:00+00:00``)
    correctly compare equal instead of being rejected as a regression by
    raw string ordering.

    Falls back to normalised string comparison when one side fails to
    parse, or when one side is naive and the other is timezone-aware
    (Python raises ``TypeError`` on mixed-awareness datetime comparisons).
    """
    new_dt = _try_parse_ts(new_ts)
    existing_dt = _try_parse_ts(existing_ts)
    if new_dt is not None and existing_dt is not None:
        if (new_dt.tzinfo is None) == (existing_dt.tzinfo is None):
            return new_dt < existing_dt
    return _normalize_ts(new_ts) < _normalize_ts(existing_ts)


# Convenience re-exports to make ``from .order_book import *`` unambiguous.
__all__ = [
    "OrderBook",
    "OrderSide",
    "OrderType",
    "PendingOrder",
    "TimeInForce",
]
