"""Unit tests for ``OrderBook`` and ``PendingOrder`` extensions added in
Trading 5/5 Step 2 (issue #384).

Step 2 is purely additive plumbing. No engine or service caller wires up the
new methods yet — these tests exercise the data-layer contract directly so
later steps (#386 partial-fill requeue, #389 bracket / OCO, #390 trailing
stop) can rely on it.
"""

from __future__ import annotations

import pytest

from investment_team.trading_service.engine.order_book import (
    OrderBook,
    PendingOrder,
)
from investment_team.trading_service.strategy.contract import (
    OrderRequest,
    OrderSide,
    OrderType,
    TimeInForce,
    UnsupportedOrderFeatureError,
)


def _base(**overrides) -> OrderRequest:
    """Minimal ``OrderRequest`` mirroring the helper in ``test_contract_gates``."""
    kwargs = {
        "client_order_id": "x",
        "symbol": "AAA",
        "side": OrderSide.LONG,
        "qty": 10.0,
        "order_type": OrderType.MARKET,
    }
    kwargs.update(overrides)
    return OrderRequest(**kwargs)


# ---------------------------------------------------------------------------
# PendingOrder field initialization
# ---------------------------------------------------------------------------


def test_submit_initializes_partial_fill_fields() -> None:
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
    )
    assert isinstance(po, PendingOrder)
    assert po.original_qty == 10.0
    assert po.remaining_qty == 10.0
    assert po.cumulative_filled_qty == 0.0
    assert po.twap_slices_remaining is None
    assert po.effective_stop_price is None
    assert po.trailing_water is None
    assert po.armed is True
    assert po.rejection_reason is None


# ---------------------------------------------------------------------------
# Submit → requeue → remove cycle
# ---------------------------------------------------------------------------


def test_submit_requeue_remove_cycle() -> None:
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
    )
    order_id = po.order_id

    requeued = book.requeue(
        order_id,
        new_remaining_qty=4.0,
        new_submitted_at="2024-01-03",
    )
    assert requeued is po  # same object, mutated in place
    assert requeued.remaining_qty == 4.0
    assert requeued.submitted_at == "2024-01-03"
    assert requeued.original_qty == 10.0  # immutable

    removed = book.remove(order_id)
    assert removed is po
    assert book.all_pending() == []
    assert book.pending_for_symbol("AAA") == []


# ---------------------------------------------------------------------------
# requeue refreshes submitted_at and rejects regression
# ---------------------------------------------------------------------------


def test_requeue_refreshes_submitted_at() -> None:
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
    )
    book.requeue(po.order_id, new_remaining_qty=5.0, new_submitted_at="2024-01-05")
    assert po.submitted_at == "2024-01-05"


def test_requeue_stale_timestamp_raises() -> None:
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-05",
        submitted_equity=100_000.0,
    )
    # Use ValueError (not assert) so the guard survives ``python -O``.
    with pytest.raises(ValueError, match="must not regress"):
        book.requeue(
            po.order_id,
            new_remaining_qty=5.0,
            new_submitted_at="2024-01-04",
        )


def test_requeue_same_timestamp_allowed() -> None:
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
    )
    # Equal timestamps are fine — the bar-safety guard fires only on strict
    # look-ahead, and intra-bar partial fills may legitimately requeue on the
    # same bar boundary before the simulator advances.
    book.requeue(po.order_id, new_remaining_qty=3.0, new_submitted_at="2024-01-02")
    assert po.remaining_qty == 3.0


def test_requeue_twap_slices_passthrough() -> None:
    book = OrderBook()
    po = book.submit(
        _base(qty=12.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
    )
    book.requeue(
        po.order_id,
        new_remaining_qty=8.0,
        new_submitted_at="2024-01-03",
        twap_slices_remaining=2,
    )
    assert po.twap_slices_remaining == 2

    book.requeue(
        po.order_id,
        new_remaining_qty=4.0,
        new_submitted_at="2024-01-04",
    )
    assert po.twap_slices_remaining is None


def test_requeue_keeps_cumulative_filled_consistent() -> None:
    """``requeue`` must update ``cumulative_filled_qty`` so the invariant
    ``original_qty == cumulative_filled_qty + remaining_qty`` holds after each
    partial-fill remainder. Otherwise downstream Fill / analytics readers see
    stale fill progress.
    """
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
    )
    assert po.cumulative_filled_qty == 0.0

    book.requeue(po.order_id, new_remaining_qty=6.0, new_submitted_at="2024-01-03")
    assert po.cumulative_filled_qty == 4.0
    assert po.original_qty == po.cumulative_filled_qty + po.remaining_qty

    book.requeue(po.order_id, new_remaining_qty=1.0, new_submitted_at="2024-01-04")
    assert po.cumulative_filled_qty == 9.0
    assert po.original_qty == po.cumulative_filled_qty + po.remaining_qty

    # Fully filled — remainder collapses to zero.
    book.requeue(po.order_id, new_remaining_qty=0.0, new_submitted_at="2024-01-05")
    assert po.cumulative_filled_qty == 10.0
    assert po.remaining_qty == 0.0


def test_requeue_rejects_negative_remaining_qty() -> None:
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
    )
    with pytest.raises(ValueError, match="must be >= 0"):
        book.requeue(po.order_id, new_remaining_qty=-1.0, new_submitted_at="2024-01-03")
    # State unchanged.
    assert po.remaining_qty == 10.0
    assert po.cumulative_filled_qty == 0.0


def test_requeue_rejects_growing_remaining_qty() -> None:
    """A partial fill can only shrink the remainder. Growing it (e.g. an off-by-one
    or stale state in the caller) would imply a negative fill, which has no
    execution semantics.
    """
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
    )
    # First a real partial fill.
    book.requeue(po.order_id, new_remaining_qty=4.0, new_submitted_at="2024-01-03")

    # Trying to grow back to 6.0 is rejected even though it's still <= original_qty.
    with pytest.raises(ValueError, match="must not exceed current remaining_qty"):
        book.requeue(po.order_id, new_remaining_qty=6.0, new_submitted_at="2024-01-04")
    # State preserved from the legitimate partial fill.
    assert po.remaining_qty == 4.0
    assert po.cumulative_filled_qty == 6.0

    # And a value larger than original_qty is also rejected.
    with pytest.raises(ValueError, match="must not exceed current remaining_qty"):
        book.requeue(po.order_id, new_remaining_qty=11.0, new_submitted_at="2024-01-04")


def test_requeue_rejects_nan_and_inf_remaining_qty() -> None:
    """``NaN`` passes both ``< 0`` and ``> current`` checks vacuously, so an
    explicit finite-check is needed to keep ``nan``/``inf`` out of fill state.
    """
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
    )

    with pytest.raises(ValueError, match="must be finite"):
        book.requeue(po.order_id, new_remaining_qty=float("nan"), new_submitted_at="2024-01-03")
    with pytest.raises(ValueError, match="must be finite"):
        book.requeue(po.order_id, new_remaining_qty=float("inf"), new_submitted_at="2024-01-03")
    with pytest.raises(ValueError, match="must be finite"):
        book.requeue(po.order_id, new_remaining_qty=float("-inf"), new_submitted_at="2024-01-03")

    # State preserved — the rejected calls leave remaining/cumulative fields untouched.
    assert po.remaining_qty == 10.0
    assert po.cumulative_filled_qty == 0.0


def test_requeue_unknown_order_id_raises_keyerror() -> None:
    """A stale/missing id is a partial-fill bug, not an idempotent removal —
    raise loudly with a clear message rather than silently no-op like
    ``cancel`` / ``remove`` do.
    """
    book = OrderBook()
    book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
    )
    with pytest.raises(KeyError, match="not in the book"):
        book.requeue("o-bogus", new_remaining_qty=5.0, new_submitted_at="2024-01-03")


# ---------------------------------------------------------------------------
# OCO sibling cancellation
# ---------------------------------------------------------------------------


def test_oco_cancel_siblings_cancels_only_siblings() -> None:
    book = OrderBook()

    parent = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
    )

    sibling_a = book.submit_attached(
        _base(qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
        submitted_at="2024-01-03",
        submitted_equity=100_000.0,
        parent_order_id=parent.order_id,
        oco_group_id="g1",
    )
    sibling_b = book.submit_attached(
        _base(qty=10.0, order_type=OrderType.STOP, stop_price=95.0),
        submitted_at="2024-01-03",
        submitted_equity=100_000.0,
        parent_order_id=parent.order_id,
        oco_group_id="g1",
    )
    unrelated = book.submit_attached(
        _base(qty=5.0, symbol="BBB", order_type=OrderType.LIMIT, limit_price=55.0),
        submitted_at="2024-01-03",
        submitted_equity=100_000.0,
        parent_order_id="other",
        oco_group_id="g2",
    )

    cancelled = book.oco_cancel_siblings(
        "g1",
        except_order_id=sibling_a.order_id,
        parent_order_id=parent.order_id,
    )
    assert cancelled == [sibling_b.order_id]

    pending_ids = {po.order_id for po in book.all_pending()}
    assert pending_ids == {parent.order_id, sibling_a.order_id, unrelated.order_id}
    # Symbol index also stays consistent.
    assert book.pending_for_symbol("BBB") == [unrelated]


def test_oco_cancel_siblings_empty_when_no_match() -> None:
    book = OrderBook()
    book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
    )
    assert (
        book.oco_cancel_siblings(
            "nonexistent",
            except_order_id="o0",
            parent_order_id="o0",
        )
        == []
    )


def test_oco_cancel_siblings_does_not_cross_brackets() -> None:
    """Two independent brackets that reuse the same ``oco_group_id`` (e.g. a
    caller picks ``"oco-1"`` for every bracket) must not cross-cancel each
    other's protective legs. Scoping by ``parent_order_id`` enforces this.
    """
    book = OrderBook()

    parent_a = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
    )
    parent_b = book.submit(
        _base(qty=10.0, symbol="BBB"),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
    )

    a_tp = book.submit_attached(
        _base(qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
        submitted_at="2024-01-03",
        submitted_equity=100_000.0,
        parent_order_id=parent_a.order_id,
        oco_group_id="g-shared",
    )
    a_sl = book.submit_attached(
        _base(qty=10.0, order_type=OrderType.STOP, stop_price=95.0),
        submitted_at="2024-01-03",
        submitted_equity=100_000.0,
        parent_order_id=parent_a.order_id,
        oco_group_id="g-shared",
    )
    b_tp = book.submit_attached(
        _base(qty=10.0, symbol="BBB", order_type=OrderType.LIMIT, limit_price=210.0),
        submitted_at="2024-01-03",
        submitted_equity=100_000.0,
        parent_order_id=parent_b.order_id,
        oco_group_id="g-shared",
    )
    b_sl = book.submit_attached(
        _base(qty=10.0, symbol="BBB", order_type=OrderType.STOP, stop_price=195.0),
        submitted_at="2024-01-03",
        submitted_equity=100_000.0,
        parent_order_id=parent_b.order_id,
        oco_group_id="g-shared",
    )

    # A's take-profit fills → cancel A's stop, leave B untouched.
    cancelled = book.oco_cancel_siblings(
        "g-shared",
        except_order_id=a_tp.order_id,
        parent_order_id=parent_a.order_id,
    )
    assert cancelled == [a_sl.order_id]

    pending_ids = {po.order_id for po in book.all_pending()}
    assert pending_ids == {
        parent_a.order_id,
        parent_b.order_id,
        a_tp.order_id,
        b_tp.order_id,
        b_sl.order_id,
    }


# ---------------------------------------------------------------------------
# children_of
# ---------------------------------------------------------------------------


def test_children_of_returns_only_direct_children() -> None:
    book = OrderBook()

    parent = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
    )
    child_a = book.submit_attached(
        _base(qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
        submitted_at="2024-01-03",
        submitted_equity=100_000.0,
        parent_order_id=parent.order_id,
        oco_group_id="g1",
    )
    child_b = book.submit_attached(
        _base(qty=10.0, order_type=OrderType.STOP, stop_price=95.0),
        submitted_at="2024-01-03",
        submitted_equity=100_000.0,
        parent_order_id=parent.order_id,
        oco_group_id="g1",
    )
    # Unrelated parent + child.
    other_parent = book.submit(
        _base(qty=5.0, symbol="BBB"),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
    )
    book.submit_attached(
        _base(qty=5.0, symbol="BBB", order_type=OrderType.LIMIT, limit_price=55.0),
        submitted_at="2024-01-03",
        submitted_equity=100_000.0,
        parent_order_id=other_parent.order_id,
        oco_group_id="g2",
    )

    children = book.children_of(parent.order_id)
    assert {c.order_id for c in children} == {child_a.order_id, child_b.order_id}


def test_children_of_returns_empty_for_unknown_parent() -> None:
    book = OrderBook()
    book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
    )
    assert book.children_of("nope") == []


# ---------------------------------------------------------------------------
# submit_attached bypasses validate_prices but still indexes by symbol
# ---------------------------------------------------------------------------


def test_submit_attached_skips_risk_gates() -> None:
    """``submit_attached`` must accept a request with ``parent_order_id`` /
    ``oco_group_id`` set even though calling ``validate_prices()`` directly on
    the same payload still raises (the gate is owned by the strategy-side
    service path; engine-side bracket materialization owns its own correctness).
    """
    book = OrderBook()
    parent = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
    )

    # Independent proof that the gate is real on the strategy path.
    gated = OrderRequest(
        client_order_id="child",
        symbol="AAA",
        side=OrderSide.LONG,
        qty=10.0,
        order_type=OrderType.LIMIT,
        limit_price=110.0,
        parent_order_id=parent.order_id,
        oco_group_id="g1",
    )
    with pytest.raises(UnsupportedOrderFeatureError):
        gated.validate_prices()

    # submit_attached must not raise on the same payload shape.
    child = book.submit_attached(
        _base(qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
        submitted_at="2024-01-03",
        submitted_equity=100_000.0,
        parent_order_id=parent.order_id,
        oco_group_id="g1",
    )
    assert child.request.parent_order_id == parent.order_id
    assert child.request.oco_group_id == "g1"
    assert child.original_qty == 10.0
    assert child.remaining_qty == 10.0


def test_submit_attached_indexes_by_symbol() -> None:
    book = OrderBook()
    parent = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
    )
    child = book.submit_attached(
        _base(qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
        submitted_at="2024-01-03",
        submitted_equity=100_000.0,
        parent_order_id=parent.order_id,
        oco_group_id="g1",
    )
    assert child in book.pending_for_symbol("AAA")
    # Removing the child also clears the symbol index entry.
    book.remove(child.order_id)
    assert child not in book.pending_for_symbol("AAA")


# ---------------------------------------------------------------------------
# submit_attached re-runs *every* validate_prices() gate except the
# parent_order_id / oco_group_id ones — i.e. malformed children (LIMIT
# without limit_price, STOP without stop_price) and unsupported types
# (TRAILING_STOP, IOC/FOK) must still be rejected at submission time so we
# never queue an order that would crash the simulator later.
# ---------------------------------------------------------------------------


def _bracket_parent(book: OrderBook):
    return book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
    )


def test_submit_attached_rejects_limit_child_without_price() -> None:
    book = OrderBook()
    parent = _bracket_parent(book)
    with pytest.raises(ValueError, match="limit order requires limit_price"):
        book.submit_attached(
            _base(qty=10.0, order_type=OrderType.LIMIT),  # limit_price missing
            submitted_at="2024-01-03",
            submitted_equity=100_000.0,
            parent_order_id=parent.order_id,
            oco_group_id="g1",
        )
    # Nothing got queued.
    assert book.children_of(parent.order_id) == []


def test_submit_attached_rejects_stop_child_without_price() -> None:
    book = OrderBook()
    parent = _bracket_parent(book)
    with pytest.raises(ValueError, match="stop order requires stop_price"):
        book.submit_attached(
            _base(qty=10.0, order_type=OrderType.STOP),  # stop_price missing
            submitted_at="2024-01-03",
            submitted_equity=100_000.0,
            parent_order_id=parent.order_id,
            oco_group_id="g1",
        )
    assert book.children_of(parent.order_id) == []


def test_submit_attached_rejects_trailing_stop_child() -> None:
    """TRAILING_STOP is gated until #390 ships — the gate must still fire on
    bracket children even though strategy-level submit was bypassed.
    """
    book = OrderBook()
    parent = _bracket_parent(book)
    with pytest.raises(UnsupportedOrderFeatureError, match="#390"):
        book.submit_attached(
            _base(qty=10.0, order_type=OrderType.TRAILING_STOP, stop_price=95.0),
            submitted_at="2024-01-03",
            submitted_equity=100_000.0,
            parent_order_id=parent.order_id,
            oco_group_id="g1",
        )
    assert book.children_of(parent.order_id) == []


def test_submit_attached_rejects_ioc_child() -> None:
    """IOC/FOK are gated until #388 ships — same as above."""
    book = OrderBook()
    parent = _bracket_parent(book)
    with pytest.raises(UnsupportedOrderFeatureError, match="#388"):
        book.submit_attached(
            _base(
                qty=10.0,
                order_type=OrderType.LIMIT,
                limit_price=110.0,
                tif=TimeInForce.IOC,
            ),
            submitted_at="2024-01-03",
            submitted_equity=100_000.0,
            parent_order_id=parent.order_id,
            oco_group_id="g1",
        )
    assert book.children_of(parent.order_id) == []
