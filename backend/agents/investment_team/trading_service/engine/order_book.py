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
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set

from ..strategy.contract import OrderRequest, OrderSide, OrderType, TimeInForce

# Tolerance below which a partial-fill remainder is treated as a terminal
# zero. Float math in upstream sizing (typically ``prev_remaining - filled``
# in the partial-fill path) can produce sub-epsilon residuals that aren't
# exactly 0; without this clamp those orders would linger in the book
# instead of being removed and the simulator could re-fill them. 1e-9 sits
# safely below any meaningful trading quantity (fractional-share venues
# generally don't go past 6 decimal places).
FILL_QTY_EPSILON = 1e-9


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
    # Set of *eligible-parent* top-level order ids. Used by
    # ``submit_attached`` to reject children whose ``parent_order_id`` is not
    # a real top-level order eligible to carry brackets:
    #
    # - Added on ``submit()`` (top-level entries).
    # - Discarded on ``cancel()`` and ``expire_day_orders()`` for top-level
    #   orders (cancelled / expired parents never opened, so attaching
    #   protective children to them would be a bug).
    # - Preserved on ``remove()`` (the terminal-fill path used by
    #   ``requeue(... new_remaining_qty=0)``): a *filled* parent is the
    #   normal case where bracket children are activated *after* the entry
    #   fills and is removed.
    #
    # ``submit_attached`` is never inserted here, so an attached child can
    # never be promoted into a parent (multi-level bracket trees rejected).
    #
    # Growth is bounded by lifecycle: cancel/expire prune in O(1) per call.
    # Filled parents only stay in the set until the operator decides to call
    # :meth:`prune_known_top_level_order_ids`, which evicts ids whose entry
    # is gone *and* whose children have all resolved.
    _known_top_level_order_ids: Set[str] = field(default_factory=set)

    # ------------------------------------------------------------------

    def submit(
        self,
        request: OrderRequest,
        *,
        submitted_at: str,
        submitted_equity: float,
    ) -> PendingOrder:
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
        self._known_top_level_order_ids.add(order_id)
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
        if parent_order_id not in self._known_top_level_order_ids:
            raise ValueError(
                f"submit_attached parent_order_id {parent_order_id!r} is not a known "
                f"top-level order id; submit the parent through OrderBook.submit() first "
                f"(attached children may not themselves be parents of further children)"
            )
        attached_request = request.model_copy(
            update={"parent_order_id": parent_order_id, "oco_group_id": oco_group_id}
        )
        attached_request.model_copy(
            update={"parent_order_id": None, "oco_group_id": None}
        ).validate_prices()

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
        # Cancelled top-level orders are no longer eligible parents — their
        # entry never produced (or won't produce any further) live exposure,
        # so attaching protective children would be a bug. Cascade-cancel
        # any pending children so we don't leave orphan protective legs in
        # the book without a live entry.
        if po.request.parent_order_id is None:
            self._known_top_level_order_ids.discard(order_id)
            self._cascade_cancel_children(order_id)
        return True

    def remove(self, order_id: str, *, was_filled: bool = False) -> Optional[PendingOrder]:
        """Drop an order from the book.

        Pass ``was_filled=True`` *only* when the removal represents a real
        terminal fill (used by ``requeue(... new_remaining_qty=0)`` and
        ``FillSimulator``'s entry / exit fill paths). In that case the
        order's id is preserved in ``_known_top_level_order_ids`` so
        bracket children can still be activated against it.

        The default ``was_filled=False`` is the safe choice for every other
        removal path (risk-gate rejection, insufficient capital, sibling
        cancellation, expiry cascade, manual removal). It evicts a top-level
        parent's id from the eligible-parent set *and* cascade-cancels any
        pending children so non-fill removals don't leave orphan bracket
        legs in the book.
        """
        po = self._pending.pop(order_id, None)
        if po is not None:
            ids = self._by_symbol.get(po.request.symbol)
            if ids and order_id in ids:
                ids.remove(order_id)
            if not was_filled and po.request.parent_order_id is None:
                self._known_top_level_order_ids.discard(order_id)
                self._cascade_cancel_children(order_id)
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

    def pending_for_symbol(self, symbol: str) -> List[PendingOrder]:
        return [self._pending[oid] for oid in self._by_symbol.get(symbol, [])]

    def all_pending(self) -> List[PendingOrder]:
        return list(self._pending.values())

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
        if not math.isfinite(new_remaining_qty):
            raise ValueError(f"requeue new_remaining_qty must be finite, got {new_remaining_qty!r}")
        # Compare both sides as parsed datetimes when possible, falling back
        # to normalised string compare when one side isn't ISO 8601. Otherwise
        # equivalent ISO 8601 instants in different timezone offsets (e.g.
        # ``+05:30`` vs ``+00:00``) would falsely reject as a regression.
        if _ts_lt(new_submitted_at, po.submitted_at):
            raise ValueError(
                f"requeue submitted_at must not regress: "
                f"old={po.submitted_at!r} new={new_submitted_at!r}"
            )
        # Clamp sub-epsilon residuals (positive *or* negative) to exactly zero
        # before the bounds checks so that a tiny remainder produced by float
        # math (e.g. ``prev - filled`` accumulating ULP error) is treated as a
        # terminal fill rather than lingering in the book. This also keeps the
        # ``< 0`` check below from rejecting tiny negatives that are physically
        # zero.
        if abs(new_remaining_qty) < FILL_QTY_EPSILON:
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
        po.submitted_at = new_submitted_at
        po.remaining_qty = new_remaining_qty
        po.cumulative_filled_qty = po.original_qty - new_remaining_qty
        po.twap_slices_remaining = twap_slices_remaining
        if new_remaining_qty == 0:
            # Fully filled — drop from the book so downstream consumers (notably
            # FillSimulator, which still sizes from req.qty) can't re-fill it.
            # ``was_filled=True`` keeps the id in ``_known_top_level_order_ids``
            # so bracket children can still be activated against this parent.
            # The returned PendingOrder reference still holds the final terminal
            # state for the caller to read.
            self.remove(order_id, was_filled=True)
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
        pruned = self._known_top_level_order_ids - active
        self._known_top_level_order_ids -= pruned
        return len(pruned)

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


def _normalize_ts(ts: str) -> str:
    """Map a trailing ``Z`` (UTC zulu suffix) to ``+00:00``. Used as both a
    pre-parse normaliser (so ``datetime.fromisoformat`` accepts the Z form on
    Python < 3.11) and the fallback string compare for unparseable inputs.
    """
    if not isinstance(ts, str):
        return ts
    if ts.endswith("Z"):
        return ts[:-1] + "+00:00"
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
