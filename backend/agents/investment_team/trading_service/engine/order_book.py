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
from typing import Dict, List, Optional

from ..strategy.contract import OrderRequest, OrderSide, OrderType, TimeInForce


@dataclass
class PendingOrder:
    order_id: str
    request: OrderRequest
    submitted_at: str  # timestamp of the bar on which this was accepted
    submitted_equity: float  # for audit / risk-recheck if needed
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

    # ------------------------------------------------------------------

    def submit(
        self,
        request: OrderRequest,
        *,
        submitted_at: str,
        submitted_equity: float,
    ) -> PendingOrder:
        self._next_id += 1
        order_id = f"o{self._next_id}"
        po = PendingOrder(
            order_id=order_id,
            request=request,
            submitted_at=submitted_at,
            submitted_equity=submitted_equity,
            original_qty=request.qty,
            remaining_qty=request.qty,
        )
        self._pending[order_id] = po
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
            original_qty=attached_request.qty,
            remaining_qty=attached_request.qty,
        )
        self._pending[order_id] = po
        self._by_symbol.setdefault(attached_request.symbol, []).append(order_id)
        return po

    def cancel(self, order_id: str) -> bool:
        po = self._pending.pop(order_id, None)
        if po is None:
            return False
        ids = self._by_symbol.get(po.request.symbol)
        if ids and order_id in ids:
            ids.remove(order_id)
        return True

    def remove(self, order_id: str) -> Optional[PendingOrder]:
        po = self._pending.pop(order_id, None)
        if po is not None:
            ids = self._by_symbol.get(po.request.symbol)
            if ids and order_id in ids:
                ids.remove(order_id)
        return po

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
        if not math.isfinite(new_remaining_qty):
            raise ValueError(f"requeue new_remaining_qty must be finite, got {new_remaining_qty!r}")
        if new_submitted_at < po.submitted_at:
            raise ValueError(
                f"requeue submitted_at must not regress: "
                f"old={po.submitted_at!r} new={new_submitted_at!r}"
            )
        if new_remaining_qty < 0:
            raise ValueError(f"requeue new_remaining_qty must be >= 0, got {new_remaining_qty!r}")
        if new_remaining_qty > po.remaining_qty:
            raise ValueError(
                f"requeue new_remaining_qty must not exceed current remaining_qty: "
                f"current={po.remaining_qty!r} new={new_remaining_qty!r}"
            )
        po.submitted_at = new_submitted_at
        po.remaining_qty = new_remaining_qty
        po.cumulative_filled_qty = po.original_qty - new_remaining_qty
        po.twap_slices_remaining = twap_slices_remaining
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
        """
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
        """Direct children of ``parent_order_id`` (no recursion)."""
        return [
            po for po in self._pending.values() if po.request.parent_order_id == parent_order_id
        ]

    # ------------------------------------------------------------------
    # TIF expiry — callers pass the current bar timestamp; day orders
    # submitted on a strictly-earlier date are removed.
    # ------------------------------------------------------------------

    def expire_day_orders(self, current_date: str) -> List[PendingOrder]:
        expired: List[PendingOrder] = []
        for oid in list(self._pending.keys()):
            po = self._pending[oid]
            if po.request.tif != TimeInForce.DAY:
                continue
            # Compare date prefix only so intraday timestamps also work.
            if _date_only(po.submitted_at) < _date_only(current_date):
                expired.append(po)
                self.remove(oid)
        return expired


def _date_only(ts: str) -> str:
    return (ts or "")[:10]


# Convenience re-exports to make ``from .order_book import *`` unambiguous.
__all__ = [
    "OrderBook",
    "OrderSide",
    "OrderType",
    "PendingOrder",
    "TimeInForce",
]
