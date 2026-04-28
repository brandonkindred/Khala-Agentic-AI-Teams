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
    FILL_QTY_EPSILON,
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


def test_requeue_accepts_zero_twap_slices() -> None:
    """``twap_slices_remaining=0`` is the terminal slice marker — TWAP just
    drained — and must be accepted alongside ``None`` and positive ints.
    """
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
    )
    book.requeue(
        po.order_id,
        new_remaining_qty=4.0,
        new_submitted_at="2024-01-03",
        twap_slices_remaining=0,
    )
    assert po.twap_slices_remaining == 0


@pytest.mark.parametrize("bad_slices", [-1, 1.5, "two", True, False])
def test_requeue_rejects_bad_twap_slices(bad_slices) -> None:
    """Negative, float, str, and bool values are all caller bugs. Bools are
    rejected explicitly because ``isinstance(True, int)`` is True in Python
    and silently accepting ``True``/``False`` as slice counts hides confusion
    in the caller.
    """
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
    )
    with pytest.raises(
        ValueError, match="twap_slices_remaining must be a non-negative int or None"
    ):
        book.requeue(
            po.order_id,
            new_remaining_qty=4.0,
            new_submitted_at="2024-01-03",
            twap_slices_remaining=bad_slices,
        )
    # State unchanged — rejected calls leave the order intact.
    assert po.remaining_qty == 10.0
    assert po.cumulative_filled_qty == 0.0
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

    # Fully filled — remainder collapses to zero. The PendingOrder reference
    # still holds the terminal state for the caller to read, but it is removed
    # from the book so the simulator can't double-fill it.
    book.requeue(po.order_id, new_remaining_qty=0.0, new_submitted_at="2024-01-05")
    assert po.cumulative_filled_qty == 10.0
    assert po.remaining_qty == 0.0
    assert po not in book.all_pending()
    assert po not in book.pending_for_symbol("AAA")


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


@pytest.mark.parametrize("bad_qty", [True, False])
def test_requeue_rejects_bool_remaining_qty(bad_qty) -> None:
    """``bool`` is a subclass of ``int`` in Python, so ``True``/``False`` would
    otherwise pass ``math.isfinite`` and the range checks and silently land in
    ``remaining_qty`` as ``1``/``0``. Reject them explicitly.
    """
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
    )
    with pytest.raises(ValueError, match="must be a real number, got bool"):
        book.requeue(po.order_id, new_remaining_qty=bad_qty, new_submitted_at="2024-01-03")
    # State unchanged.
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


def test_requeue_zero_remainder_removes_order_from_book() -> None:
    """``requeue(..., new_remaining_qty=0)`` is the terminal step of a partial-fill
    sequence. The order must be removed from ``_pending`` and from the symbol
    index so downstream consumers (notably ``FillSimulator``, which sizes from
    ``req.qty`` rather than ``remaining_qty``) cannot double-fill it. The
    returned reference still carries the terminal state for the caller.
    """
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
    )
    # First a real partial fill — order remains in the book.
    book.requeue(po.order_id, new_remaining_qty=4.0, new_submitted_at="2024-01-03")
    assert po in book.all_pending()
    assert po in book.pending_for_symbol("AAA")

    returned = book.requeue(po.order_id, new_remaining_qty=0.0, new_submitted_at="2024-01-04")
    # Caller still gets the PendingOrder reference with terminal state.
    assert returned is po
    assert po.remaining_qty == 0.0
    assert po.cumulative_filled_qty == po.original_qty == 10.0
    # But it is no longer queued for further fills.
    assert po not in book.all_pending()
    assert po not in book.pending_for_symbol("AAA")
    # And the next requeue raises (book no longer holds the id).
    with pytest.raises(KeyError, match="not in the book"):
        book.requeue(po.order_id, new_remaining_qty=0.0, new_submitted_at="2024-01-05")


@pytest.mark.parametrize("residual", [1e-15, 1e-12, FILL_QTY_EPSILON / 2])
def test_requeue_clamps_tiny_positive_residual_to_zero(residual) -> None:
    """Float math in upstream sizing (``prev_remaining - filled``) can produce
    sub-epsilon remainders that aren't exactly 0. Without clamping, those
    orders linger in the book and can be re-filled by the simulator. Verify
    that any value with absolute magnitude below FILL_QTY_EPSILON is treated
    as a terminal fill: the order is removed and ``cumulative_filled_qty``
    equals ``original_qty``.
    """
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
    )
    book.requeue(po.order_id, new_remaining_qty=residual, new_submitted_at="2024-01-03")
    assert po.remaining_qty == 0.0
    assert po.cumulative_filled_qty == 10.0
    assert po not in book.all_pending()
    assert po not in book.pending_for_symbol("AAA")


@pytest.mark.parametrize("residual", [-1e-15, -FILL_QTY_EPSILON / 2])
def test_requeue_clamps_tiny_negative_residual_to_zero(residual) -> None:
    """Tiny negatives from accumulated float error are physically zero and
    shouldn't trip the ``< 0`` rejection. They should clamp the same way
    positive sub-epsilon residuals do.
    """
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
    )
    book.requeue(po.order_id, new_remaining_qty=residual, new_submitted_at="2024-01-03")
    assert po.remaining_qty == 0.0
    assert po.cumulative_filled_qty == 10.0
    assert po not in book.all_pending()


def test_requeue_preserves_remainder_above_epsilon() -> None:
    """A remainder safely above FILL_QTY_EPSILON should not be clamped — the
    order stays in the book with the requested partial-fill remainder.
    """
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
    )
    above = FILL_QTY_EPSILON * 100  # 1e-7 — well above the clamp tolerance
    book.requeue(po.order_id, new_remaining_qty=above, new_submitted_at="2024-01-03")
    assert po.remaining_qty == above
    assert po.cumulative_filled_qty == 10.0 - above
    assert po in book.all_pending()


def test_requeue_rejects_negative_above_epsilon() -> None:
    """The clamp only swallows sub-epsilon negatives. A genuinely negative
    remainder still gets rejected by the strict ``< 0`` guard.
    """
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
    )
    with pytest.raises(ValueError, match="must be >= 0"):
        book.requeue(
            po.order_id,
            new_remaining_qty=-FILL_QTY_EPSILON * 1000,  # -1e-6, well past the clamp
            new_submitted_at="2024-01-03",
        )
    assert po.remaining_qty == 10.0  # unchanged


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


@pytest.mark.parametrize("arg", ["oco_group_id", "except_order_id", "parent_order_id"])
@pytest.mark.parametrize("bad_value", [None, "", 123, 1.5])
def test_oco_cancel_siblings_rejects_bad_id_args(arg, bad_value) -> None:
    """``OrderRequest`` stores ``oco_group_id``/``parent_order_id`` as
    ``Optional[str]`` defaulting to ``None``, so calling
    ``oco_cancel_siblings`` with ``None`` (or any non-string) for either id
    would equality-match every ordinary parent order in the book and nuke
    unrelated orders. Reject all three id args defensively.
    """
    book = OrderBook()
    parent = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
    )
    sibling = book.submit_attached(
        _base(qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
        submitted_at="2024-01-03",
        submitted_equity=100_000.0,
        parent_order_id=parent.order_id,
        oco_group_id="g1",
    )
    kwargs: dict = {
        "oco_group_id": "g1",
        "except_order_id": sibling.order_id,
        "parent_order_id": parent.order_id,
    }
    kwargs[arg] = bad_value
    with pytest.raises(TypeError, match=f"{arg} must be a non-empty str"):
        book.oco_cancel_siblings(
            kwargs.pop("oco_group_id"),
            except_order_id=kwargs["except_order_id"],
            parent_order_id=kwargs["parent_order_id"],
        )
    # Nothing got cancelled — both pending orders are still in the book.
    assert {po.order_id for po in book.all_pending()} == {parent.order_id, sibling.order_id}


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


@pytest.mark.parametrize("bad_parent", [None, "", 123, 1.5])
def test_children_of_rejects_bad_parent_id(bad_parent) -> None:
    """``OrderRequest.parent_order_id`` defaults to ``None`` for top-level
    orders, so calling ``children_of(None)`` would equality-match every
    top-level order and cause callers to apply child-order logic
    (cancellation, etc.) to unrelated entries. Reject defensively.
    """
    book = OrderBook()
    book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
    )
    with pytest.raises(TypeError, match="parent_order_id must be a non-empty str"):
        book.children_of(bad_parent)


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


# ---------------------------------------------------------------------------
# parent_order_id / oco_group_id type validation. Pydantic's
# ``model_copy(update=...)`` does not validate update values, so a non-string
# id would be stored silently and break later string-based lookups
# (``children_of`` / ``oco_cancel_siblings`` compare with ``==``).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_parent", [123, None, "", 1.5])
def test_submit_attached_rejects_bad_parent_order_id(bad_parent) -> None:
    book = OrderBook()
    _bracket_parent(book)
    with pytest.raises(TypeError, match="parent_order_id must be a non-empty str"):
        book.submit_attached(
            _base(qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
            submitted_at="2024-01-03",
            submitted_equity=100_000.0,
            parent_order_id=bad_parent,
            oco_group_id="g1",
        )
    # Nothing got queued — only the bracket parent remains.
    assert len(book.all_pending()) == 1


@pytest.mark.parametrize("bad_group", [123, None, "", 1.5])
def test_submit_attached_rejects_bad_oco_group_id(bad_group) -> None:
    book = OrderBook()
    parent = _bracket_parent(book)
    with pytest.raises(TypeError, match="oco_group_id must be a non-empty str"):
        book.submit_attached(
            _base(qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
            submitted_at="2024-01-03",
            submitted_equity=100_000.0,
            parent_order_id=parent.order_id,
            oco_group_id=bad_group,
        )
    assert len(book.all_pending()) == 1
