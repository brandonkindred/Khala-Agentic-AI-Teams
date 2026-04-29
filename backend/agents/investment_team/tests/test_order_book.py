"""Unit tests for ``OrderBook`` and ``PendingOrder`` extensions added in
Trading 5/5 Step 2 (issue #384).

Step 2 is purely additive plumbing. No engine or service caller wires up the
new methods yet — these tests exercise the data-layer contract directly so
later steps (#386 partial-fill requeue, #389 bracket / OCO, #390 trailing
stop) can rely on it.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from investment_team.trading_service.engine.order_book import (
    FILL_QTY_REL_TOL,
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
        expect_brackets=True,
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


def test_submit_rejects_request_with_parent_order_id() -> None:
    """Defense in depth — strategy-side ``validate_prices()`` already rejects
    these fields, but ``submit()`` is the gateway that registers an id as an
    eligible bracket parent. Refusing child-shaped requests here prevents an
    internal caller that bypasses ``validate_prices`` from smuggling an
    attached order into the eligible-parent set.
    """
    book = OrderBook()
    parent = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    bad_request = _base(qty=5.0).model_copy(update={"parent_order_id": parent.order_id})
    with pytest.raises(ValueError, match="must not receive a request with parent_order_id"):
        book.submit(bad_request, submitted_at="2024-01-03", submitted_equity=100_000.0)


def test_submit_rejects_request_with_oco_group_id() -> None:
    book = OrderBook()
    bad_request = _base(qty=10.0).model_copy(update={"oco_group_id": "g1"})
    with pytest.raises(ValueError, match="must not receive a request with parent_order_id"):
        book.submit(bad_request, submitted_at="2024-01-02", submitted_equity=100_000.0)
    assert book.all_pending() == []


def test_remove_default_evicts_top_level_id() -> None:
    """``remove(was_filled=False)`` (default) is the safe path used for risk-
    rejection / insufficient-capital paths in ``fill_simulator``: the parent
    never opened, so its id must be evicted from the eligible-parent set so
    a later ``submit_attached`` doesn't attach children to a non-existent
    entry. Distinct from ``requeue(0)`` which uses ``was_filled=True``.
    """
    book = OrderBook()
    parent = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    book.remove(parent.order_id)  # default was_filled=False — non-fill path

    with pytest.raises(ValueError, match="not a known top-level order id"):
        book.submit_attached(
            _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
            submitted_at="2024-01-03",
            submitted_equity=100_000.0,
            parent_order_id=parent.order_id,
            oco_group_id="g1",
        )


def test_remove_with_was_filled_preserves_top_level_id() -> None:
    """``remove(was_filled=True)`` is the terminal-fill path — keeps the id
    eligible so bracket children can still be activated against it.
    """
    book = OrderBook()
    parent = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    book.remove(parent.order_id, was_filled=True)

    child = book.submit_attached(
        _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
        submitted_at="2024-01-03",
        submitted_equity=100_000.0,
        parent_order_id=parent.order_id,
        oco_group_id="g1",
    )
    assert child.request.parent_order_id == parent.order_id


# ---------------------------------------------------------------------------
# Submit → requeue → remove cycle
# ---------------------------------------------------------------------------


def test_submit_requeue_remove_cycle() -> None:
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
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
        expect_brackets=True,
    )
    book.requeue(po.order_id, new_remaining_qty=5.0, new_submitted_at="2024-01-05")
    assert po.submitted_at == "2024-01-05"


def test_requeue_stale_timestamp_raises() -> None:
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-05",
        submitted_equity=100_000.0,
        expect_brackets=True,
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
        expect_brackets=True,
    )
    # Equal timestamps are fine — the bar-safety guard fires only on strict
    # look-ahead, and intra-bar partial fills may legitimately requeue on the
    # same bar boundary before the simulator advances.
    book.requeue(po.order_id, new_remaining_qty=3.0, new_submitted_at="2024-01-02")
    assert po.remaining_qty == 3.0


def test_requeue_normalizes_z_suffix_timestamp() -> None:
    """``…Z`` and ``…+00:00`` are equivalent ISO 8601 representations of the
    same instant; lexicographic comparison would falsely reject ``…Z`` as
    earlier than ``…+00:00`` (because ``Z`` < ``+`` in ASCII). The regression
    guard normalises both sides before comparing.
    """
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02T10:00:00+00:00",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    # Same instant, just expressed with the Z suffix.
    book.requeue(
        po.order_id,
        new_remaining_qty=4.0,
        new_submitted_at="2024-01-02T10:00:00Z",
    )
    assert po.remaining_qty == 4.0
    # ``requeue`` stores the caller-supplied timestamp verbatim — the
    # downstream look-ahead guard does its own chronological compare so we
    # don't need to mangle the stored format.
    assert po.submitted_at == "2024-01-02T10:00:00Z"

    # And the other direction: existing has Z suffix, new has +00:00 — also
    # not a regression.
    po2 = book.submit(
        _base(qty=5.0, symbol="BBB"),
        submitted_at="2024-01-02T10:00:00Z",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    book.requeue(
        po2.order_id,
        new_remaining_qty=2.0,
        new_submitted_at="2024-01-02T10:00:00+00:00",
    )
    assert po2.remaining_qty == 2.0


def test_requeue_twap_slices_passthrough() -> None:
    book = OrderBook()
    po = book.submit(
        _base(qty=12.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
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
        expect_brackets=True,
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
        expect_brackets=True,
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
        expect_brackets=True,
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
        expect_brackets=True,
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
        expect_brackets=True,
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
        expect_brackets=True,
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
        expect_brackets=True,
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
        expect_brackets=True,
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
        expect_brackets=True,
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


@pytest.mark.parametrize("residual", [1e-15, 1e-14, 1e-13])
def test_requeue_clamps_tiny_positive_residual_to_zero(residual) -> None:
    """Float math in upstream sizing (``prev_remaining - filled``) can produce
    sub-ULP remainders that aren't exactly 0. Without clamping, those
    orders linger in the book and can be re-filled by the simulator.

    For ``original_qty=10``, the relative threshold is
    ``10 * 1e-12 = 1e-11``, so values like ``1e-15`` / ``1e-14`` / ``1e-13``
    are all comfortably below ULP-level error and get clamped.
    """
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    book.requeue(po.order_id, new_remaining_qty=residual, new_submitted_at="2024-01-03")
    assert po.remaining_qty == 0.0
    assert po.cumulative_filled_qty == 10.0
    assert po not in book.all_pending()
    assert po not in book.pending_for_symbol("AAA")


@pytest.mark.parametrize("residual", [-1e-15, -1e-14, -1e-13])
def test_requeue_clamps_tiny_negative_residual_to_zero(residual) -> None:
    """Tiny negatives from accumulated float error are physically zero and
    shouldn't trip the ``< 0`` rejection. They should clamp the same way
    positive sub-tolerance residuals do.
    """
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    book.requeue(po.order_id, new_remaining_qty=residual, new_submitted_at="2024-01-03")
    assert po.remaining_qty == 0.0
    assert po.cumulative_filled_qty == 10.0
    assert po not in book.all_pending()


def test_requeue_preserves_remainder_above_relative_threshold() -> None:
    """A remainder safely above the relative tolerance threshold should not
    be clamped. For ``original_qty=10``, the threshold is ``1e-11``; a
    remainder of ``1e-7`` is six orders of magnitude above it and clearly
    a real partial fill, not float noise.
    """
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    above = 10.0 * FILL_QTY_REL_TOL * 1_000_000  # 1e-5 — well above the clamp
    book.requeue(po.order_id, new_remaining_qty=above, new_submitted_at="2024-01-03")
    assert po.remaining_qty == above
    assert po.cumulative_filled_qty == 10.0 - above
    assert po in book.all_pending()


def test_requeue_rejects_negative_above_relative_threshold() -> None:
    """The clamp only swallows sub-tolerance negatives. A genuinely negative
    remainder still gets rejected by the strict ``< 0`` guard.
    """
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    with pytest.raises(ValueError, match="must be >= 0"):
        book.requeue(
            po.order_id,
            new_remaining_qty=-1e-6,  # well past the relative-tolerance clamp
            new_submitted_at="2024-01-03",
        )
    assert po.remaining_qty == 10.0  # unchanged


def test_requeue_does_not_clamp_legitimate_small_remainder() -> None:
    """For a legitimately small order (e.g. fractional-token venue where
    every quantity is sub-1e-9 in absolute terms), a remainder that would
    be below the *absolute* sub-epsilon threshold of an earlier
    implementation must NOT be clamped if it's still a meaningful fraction
    of the order's own size. The clamp is *relative* to ``original_qty``.

    Regression test for the reviewer's concern that an absolute
    ``FILL_QTY_EPSILON`` threshold over-clamped real partial fills on
    small orders.
    """
    book = OrderBook()
    po = book.submit(
        _base(qty=1e-8),  # legitimate fractional-token amount
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    # 5e-10 is 5% of the original 1e-8 — a meaningful partial-fill
    # remainder, NOT ULP noise. Must not be clamped.
    book.requeue(po.order_id, new_remaining_qty=5e-10, new_submitted_at="2024-01-03")
    assert po.remaining_qty == 5e-10
    assert po in book.all_pending()


# ---------------------------------------------------------------------------
# OCO sibling cancellation
# ---------------------------------------------------------------------------


def test_oco_cancel_siblings_cancels_only_siblings() -> None:
    book = OrderBook()

    parent = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    other_parent = book.submit(
        _base(qty=5.0, symbol="BBB"),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )

    sibling_a = book.submit_attached(
        _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
        submitted_at="2024-01-03",
        submitted_equity=100_000.0,
        parent_order_id=parent.order_id,
        oco_group_id="g1",
    )
    sibling_b = book.submit_attached(
        _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.STOP, stop_price=95.0),
        submitted_at="2024-01-03",
        submitted_equity=100_000.0,
        parent_order_id=parent.order_id,
        oco_group_id="g1",
    )
    unrelated = book.submit_attached(
        _base(
            side=OrderSide.SHORT,
            qty=5.0,
            symbol="BBB",
            order_type=OrderType.LIMIT,
            limit_price=55.0,
        ),
        submitted_at="2024-01-03",
        submitted_equity=100_000.0,
        parent_order_id=other_parent.order_id,
        oco_group_id="g2",
    )

    cancelled = book.oco_cancel_siblings(
        "g1",
        except_order_id=sibling_a.order_id,
        parent_order_id=parent.order_id,
    )
    assert cancelled == [sibling_b.order_id]

    pending_ids = {po.order_id for po in book.all_pending()}
    assert pending_ids == {
        parent.order_id,
        other_parent.order_id,
        sibling_a.order_id,
        unrelated.order_id,
    }
    # Symbol index also stays consistent.
    assert {po.order_id for po in book.pending_for_symbol("BBB")} == {
        other_parent.order_id,
        unrelated.order_id,
    }


def test_oco_cancel_siblings_rejects_stale_except_order_id() -> None:
    """A stale ``except_order_id`` (not currently pending) would otherwise
    fall through and let the cancellation loop nuke every leg in the group,
    leaving the bracket unprotected. Reject explicitly.
    """
    book = OrderBook()
    book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    with pytest.raises(ValueError, match="is not currently pending"):
        book.oco_cancel_siblings(
            "g1",
            except_order_id="o-stale",
            parent_order_id="o-stale",
        )


def test_oco_cancel_siblings_rejects_mismatched_except_leg() -> None:
    """``except_order_id`` is currently pending, but its (group, parent) tuple
    doesn't match the requested cancellation scope — reject so a typo can't
    silently clear an unrelated bracket.
    """
    book = OrderBook()
    parent = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    sibling = book.submit_attached(
        _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
        submitted_at="2024-01-03",
        submitted_equity=100_000.0,
        parent_order_id=parent.order_id,
        oco_group_id="g1",
    )
    # except_order_id is real and pending, but with mismatched group / parent.
    with pytest.raises(ValueError, match="does not belong to"):
        book.oco_cancel_siblings(
            "different-group",
            except_order_id=sibling.order_id,
            parent_order_id=parent.order_id,
        )
    # Nothing got cancelled.
    assert sibling in book.all_pending()


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
        expect_brackets=True,
    )
    sibling = book.submit_attached(
        _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
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
        expect_brackets=True,
    )
    parent_b = book.submit(
        _base(qty=10.0, symbol="BBB"),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )

    a_tp = book.submit_attached(
        _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
        submitted_at="2024-01-03",
        submitted_equity=100_000.0,
        parent_order_id=parent_a.order_id,
        oco_group_id="g-shared",
    )
    a_sl = book.submit_attached(
        _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.STOP, stop_price=95.0),
        submitted_at="2024-01-03",
        submitted_equity=100_000.0,
        parent_order_id=parent_a.order_id,
        oco_group_id="g-shared",
    )
    b_tp = book.submit_attached(
        _base(
            side=OrderSide.SHORT,
            qty=10.0,
            symbol="BBB",
            order_type=OrderType.LIMIT,
            limit_price=210.0,
        ),
        submitted_at="2024-01-03",
        submitted_equity=100_000.0,
        parent_order_id=parent_b.order_id,
        oco_group_id="g-shared",
    )
    b_sl = book.submit_attached(
        _base(
            side=OrderSide.SHORT,
            qty=10.0,
            symbol="BBB",
            order_type=OrderType.STOP,
            stop_price=195.0,
        ),
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
        expect_brackets=True,
    )
    child_a = book.submit_attached(
        _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
        submitted_at="2024-01-03",
        submitted_equity=100_000.0,
        parent_order_id=parent.order_id,
        oco_group_id="g1",
    )
    child_b = book.submit_attached(
        _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.STOP, stop_price=95.0),
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
        expect_brackets=True,
    )
    book.submit_attached(
        _base(
            side=OrderSide.SHORT,
            qty=5.0,
            symbol="BBB",
            order_type=OrderType.LIMIT,
            limit_price=55.0,
        ),
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
        expect_brackets=True,
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
        expect_brackets=True,
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
        expect_brackets=True,
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
        _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
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
        expect_brackets=True,
    )
    child = book.submit_attached(
        _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
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
        expect_brackets=True,
    )


def test_submit_attached_rejects_limit_child_without_price() -> None:
    book = OrderBook()
    parent = _bracket_parent(book)
    with pytest.raises(ValueError, match="limit order requires limit_price"):
        book.submit_attached(
            _base(
                side=OrderSide.SHORT, qty=10.0, order_type=OrderType.LIMIT
            ),  # limit_price missing
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
            _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.STOP),  # stop_price missing
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
            _base(
                side=OrderSide.SHORT, qty=10.0, order_type=OrderType.TRAILING_STOP, stop_price=95.0
            ),
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
                side=OrderSide.SHORT,
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
            _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
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
            _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
            submitted_at="2024-01-03",
            submitted_equity=100_000.0,
            parent_order_id=parent.order_id,
            oco_group_id=bad_group,
        )
    assert len(book.all_pending()) == 1


# ---------------------------------------------------------------------------
# submit_attached parent linkage. A typo / stale id would otherwise create an
# orphan child that the simulator treats as a standalone order.
# ---------------------------------------------------------------------------


def test_submit_attached_rejects_unknown_parent_order_id() -> None:
    """An ``parent_order_id`` that was never allocated by this book is a
    bracket plumbing bug — reject loudly so the orphan child can't slip into
    the book and execute as a standalone entry/exit.
    """
    book = OrderBook()
    with pytest.raises(ValueError, match="not a known top-level order id"):
        book.submit_attached(
            _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
            submitted_at="2024-01-03",
            submitted_equity=100_000.0,
            parent_order_id="o-typo",
            oco_group_id="g1",
        )
    assert book.all_pending() == []


def test_submit_attached_accepts_parent_after_removal() -> None:
    """Typical bracket flow: parent fills, gets removed from ``_pending``,
    children are then submitted to materialize the protective legs. Validation
    must accept the parent's id even though it's no longer in ``_pending``.
    """
    book = OrderBook()
    parent = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    # Simulate the terminal-fill removal path used by ``requeue(0)``;
    # ``was_filled=True`` keeps the parent eligible for bracket children.
    book.remove(parent.order_id, was_filled=True)
    assert parent not in book.all_pending()

    child = book.submit_attached(
        _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
        submitted_at="2024-01-03",
        submitted_equity=100_000.0,
        parent_order_id=parent.order_id,
        oco_group_id="g1",
    )
    assert child.request.parent_order_id == parent.order_id


def test_submit_attached_rejects_cancelled_parent() -> None:
    """A cancelled top-level order never opened, so attaching protective
    children to it would be a bug. ``cancel()`` must remove the parent's id
    from the eligible-parent set so subsequent ``submit_attached`` fails.
    """
    book = OrderBook()
    parent = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    assert book.cancel(parent.order_id) is True

    with pytest.raises(ValueError, match="not a known top-level order id"):
        book.submit_attached(
            _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
            submitted_at="2024-01-03",
            submitted_equity=100_000.0,
            parent_order_id=parent.order_id,
            oco_group_id="g1",
        )


def test_submit_attached_rejects_expired_parent() -> None:
    """Same lifecycle rule as cancel: a DAY order expired without filling
    is not eligible to be a bracket parent.
    """
    book = OrderBook()
    parent = book.submit(
        _base(qty=10.0, tif=TimeInForce.DAY),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    expired = book.expire_day_orders("2024-01-03")
    assert expired == [parent]

    with pytest.raises(ValueError, match="not a known top-level order id"):
        book.submit_attached(
            _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
            submitted_at="2024-01-03",
            submitted_equity=100_000.0,
            parent_order_id=parent.order_id,
            oco_group_id="g1",
        )


def test_submit_attached_rejects_attached_child_as_parent() -> None:
    """Multi-level bracket trees (``parent → child → grandchild``) are not
    supported by this API. Only ids allocated by ``submit()`` (top-level
    orders) are eligible parents — attached children must not be promoted
    into parents themselves, otherwise ``children_of`` traversal and OCO
    cancellation semantics break.
    """
    book = OrderBook()
    parent = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    child = book.submit_attached(
        _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
        submitted_at="2024-01-03",
        submitted_equity=100_000.0,
        parent_order_id=parent.order_id,
        oco_group_id="g1",
    )
    # The child has a real, valid order_id, but attempting to use it as a
    # parent for another submit_attached must fail.
    with pytest.raises(ValueError, match="not a known top-level order id"):
        book.submit_attached(
            _base(side=OrderSide.SHORT, qty=5.0, order_type=OrderType.STOP, stop_price=95.0),
            submitted_at="2024-01-04",
            submitted_equity=100_000.0,
            parent_order_id=child.order_id,
            oco_group_id="g2",
        )
    # Grandchild was not queued.
    assert book.children_of(child.order_id) == []


# ---------------------------------------------------------------------------
# prune_known_top_level_order_ids — operators of long-running services should
# call this periodically to bound memory growth in ``_known_top_level_order_ids``.
# ---------------------------------------------------------------------------


def test_prune_keeps_active_parents() -> None:
    """A top-level order that's still pending — or that has at least one
    pending child — must be preserved by prune so the next bracket
    materialization still validates correctly.
    """
    book = OrderBook()
    pending_parent = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    parent_with_child = book.submit(
        _base(qty=5.0, symbol="BBB"),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    # Remove the parent but keep its child pending — parent_id is still
    # referenced by an active leg, so prune must keep it.
    book.submit_attached(
        _base(
            side=OrderSide.SHORT,
            qty=5.0,
            symbol="BBB",
            order_type=OrderType.LIMIT,
            limit_price=55.0,
        ),
        submitted_at="2024-01-03",
        submitted_equity=100_000.0,
        parent_order_id=parent_with_child.order_id,
        oco_group_id="g2",
    )
    # Filled parent (was_filled=True) — keeps the id in the eligible-parent
    # set so the still-pending child can later be referenced.
    book.remove(parent_with_child.order_id, was_filled=True)

    pruned = book.prune_known_top_level_order_ids()
    assert pruned == 0
    # Both parent ids still admit new children.
    book.submit_attached(
        _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
        submitted_at="2024-01-04",
        submitted_equity=100_000.0,
        parent_order_id=pending_parent.order_id,
        oco_group_id="g1",
    )
    book.submit_attached(
        _base(
            side=OrderSide.SHORT, qty=5.0, symbol="BBB", order_type=OrderType.STOP, stop_price=45.0
        ),
        submitted_at="2024-01-04",
        submitted_equity=100_000.0,
        parent_order_id=parent_with_child.order_id,
        oco_group_id="g3",
    )


def test_prune_evicts_fully_resolved_parents() -> None:
    """A top-level order whose entry has been removed *and* has no remaining
    pending children is fully resolved — its id is safe to evict, and
    subsequent ``submit_attached`` calls referencing it correctly fail.
    """
    book = OrderBook()
    parent = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    # Filled-parent removal: keeps the id in the eligible-parent set so
    # ``prune_known_top_level_order_ids`` is the natural eviction path.
    book.remove(parent.order_id, was_filled=True)
    assert book.children_of(parent.order_id) == []

    pruned = book.prune_known_top_level_order_ids()
    assert pruned == 1

    # Submitting a child against the pruned parent now fails — the bracket is
    # fully resolved.
    with pytest.raises(ValueError, match="not a known top-level order id"):
        book.submit_attached(
            _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
            submitted_at="2024-01-04",
            submitted_equity=100_000.0,
            parent_order_id=parent.order_id,
            oco_group_id="g1",
        )


# ---------------------------------------------------------------------------
# DAY-TIF expiry uses the immutable ``original_submitted_at`` anchor so a
# partially-filled-and-requeued DAY order still expires at the end of its
# original session, not the day after the last partial fill.
# ---------------------------------------------------------------------------


def test_expire_day_orders_uses_original_submitted_at() -> None:
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0, tif=TimeInForce.DAY),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    # Partial fill bumps ``submitted_at`` to D+1 so the look-ahead guard still
    # holds, but the original submission date is preserved separately.
    book.requeue(po.order_id, new_remaining_qty=4.0, new_submitted_at="2024-01-03")
    assert po.submitted_at == "2024-01-03"
    assert po.original_submitted_at == "2024-01-02"

    # On D+1 (2024-01-03) the order should expire — it was originally a DAY
    # order submitted on 2024-01-02, so it must not survive into 2024-01-03's
    # session.
    expired = book.expire_day_orders("2024-01-03")
    assert expired == [po]
    assert po not in book.all_pending()


def test_expire_day_orders_keeps_order_on_original_day() -> None:
    """Sanity: a DAY order shouldn't expire on the same date it was submitted,
    even after a same-day requeue.
    """
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0, tif=TimeInForce.DAY),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    # Same-day requeue (the look-ahead guard allows equal timestamps).
    book.requeue(po.order_id, new_remaining_qty=5.0, new_submitted_at="2024-01-02")
    assert book.expire_day_orders("2024-01-02") == []
    assert po in book.all_pending()


def test_expire_day_orders_cascades_to_pending_children() -> None:
    """When a top-level DAY order expires unfilled, any already-pending
    attached children must also be cancelled — otherwise the orphan
    protective legs would execute as standalone orders without the parent
    entry having ever opened.
    """
    book = OrderBook()
    parent = book.submit(
        _base(qty=10.0, tif=TimeInForce.DAY),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    # Children submitted while the parent is still pending (preventive
    # bracket pattern). Use GTC so the children themselves don't expire.
    child_a = book.submit_attached(
        _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        parent_order_id=parent.order_id,
        oco_group_id="g1",
    )
    child_b = book.submit_attached(
        _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.STOP, stop_price=95.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        parent_order_id=parent.order_id,
        oco_group_id="g1",
    )
    # Parent expires at end-of-session — children must be cascaded out.
    expired = book.expire_day_orders("2024-01-03")
    assert expired == [parent]
    assert book.all_pending() == []
    assert child_a not in book.all_pending()
    assert child_b not in book.all_pending()


# ---------------------------------------------------------------------------
# requeue uses datetime-aware timestamp comparison so equivalent ISO 8601
# instants in different timezone offsets don't trip the regression guard.
# ---------------------------------------------------------------------------


def test_requeue_treats_equivalent_offsets_as_equal() -> None:
    """``2024-01-02T10:00:00+05:30`` and ``2024-01-02T04:30:00+00:00`` are the
    same instant. Lexicographic string compare would falsely reject the
    second as a regression (``+`` ASCII < ``+05:30`` literally is fine, but
    the time portion sorts ``04:`` before ``10:``). Datetime parsing fixes
    this.
    """
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02T10:00:00+05:30",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    # Same instant in UTC offset — must not raise.
    book.requeue(
        po.order_id,
        new_remaining_qty=4.0,
        new_submitted_at="2024-01-02T04:30:00+00:00",
    )
    assert po.remaining_qty == 4.0


def test_requeue_rejects_strictly_earlier_instant_across_offsets() -> None:
    """Datetime-aware compare still rejects a genuinely earlier instant
    expressed in a different offset.
    """
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02T10:00:00+00:00",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    # 09:00 IST = 03:30 UTC, which is before 10:00 UTC → regression.
    with pytest.raises(ValueError, match="must not regress"):
        book.requeue(
            po.order_id,
            new_remaining_qty=5.0,
            new_submitted_at="2024-01-02T09:00:00+05:30",
        )


def test_requeue_falls_back_to_string_compare_for_unparseable_ts() -> None:
    """When a timestamp doesn't parse as ISO 8601, the regression guard
    falls back to normalised string comparison so callers using non-ISO
    formats aren't silently accepted in either direction.
    """
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0),
        submitted_at="bar-2024-01-02",  # not ISO 8601
        submitted_equity=100_000.0,
    )
    # Lexicographically earlier — should still raise via string fallback.
    with pytest.raises(ValueError, match="must not regress"):
        book.requeue(po.order_id, new_remaining_qty=5.0, new_submitted_at="bar-2024-01-01")
    # And same lexicographic ordering forward — accepted.
    book.requeue(po.order_id, new_remaining_qty=4.0, new_submitted_at="bar-2024-01-03")
    assert po.remaining_qty == 4.0


# ---------------------------------------------------------------------------
# Cascade-cancel attached children when a top-level parent leaves the book
# without filling. Covers cancel() and remove(was_filled=False); the
# expire_day_orders cascade is exercised separately above.
# ---------------------------------------------------------------------------


def _bracket_with_two_children(book: OrderBook):
    parent = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    child_a = book.submit_attached(
        _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        parent_order_id=parent.order_id,
        oco_group_id="g1",
    )
    child_b = book.submit_attached(
        _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.STOP, stop_price=95.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        parent_order_id=parent.order_id,
        oco_group_id="g1",
    )
    return parent, child_a, child_b


def test_cancel_cascades_to_pending_children() -> None:
    """``cancel()`` on a top-level parent must cascade-cancel its attached
    children. Otherwise an orphan TP / SL leg would still execute as a
    standalone order even though the entry was cancelled.
    """
    book = OrderBook()
    parent, child_a, child_b = _bracket_with_two_children(book)
    assert book.cancel(parent.order_id) is True
    # Parent and both children gone.
    assert book.all_pending() == []
    assert child_a not in book.all_pending()
    assert child_b not in book.all_pending()


def test_remove_unfilled_cascades_to_pending_children() -> None:
    """``remove(was_filled=False)`` on a top-level parent must cascade for
    the same reason as ``cancel()``: a non-filled removal (risk-rejection,
    insufficient capital, manual cleanup) leaves no entry, so the
    protective legs are orphans.
    """
    book = OrderBook()
    parent, child_a, child_b = _bracket_with_two_children(book)
    book.remove(parent.order_id)  # default was_filled=False
    assert book.all_pending() == []
    assert child_a not in book.all_pending()
    assert child_b not in book.all_pending()


def test_remove_filled_does_not_cascade_to_pending_children() -> None:
    """``remove(was_filled=True)`` is the terminal-fill path. The parent
    just opened, so its bracket children should *stay* live to protect the
    new position. Cascade must not fire here.
    """
    book = OrderBook()
    parent, child_a, child_b = _bracket_with_two_children(book)
    book.remove(parent.order_id, was_filled=True)
    pending_ids = {po.order_id for po in book.all_pending()}
    assert pending_ids == {child_a.order_id, child_b.order_id}


# ---------------------------------------------------------------------------
# Auto-eviction of filled-parent ids once the bracket is fully resolved.
# Without this, ``_known_top_level_order_ids`` would only ever shrink on
# cancel/expire/non-fill paths, so long-running services would accumulate
# every filled parent forever even after their brackets fully closed out.
# ---------------------------------------------------------------------------


def test_filled_parent_remains_eligible_while_children_pending() -> None:
    """The filled parent must stay in the eligible-parent set as long as at
    least one of its protective legs is still pending — otherwise the
    bracket flow can't add late children (e.g. a re-armed stop) to it.
    """
    book = OrderBook()
    parent, child_a, child_b = _bracket_with_two_children(book)
    book.remove(parent.order_id, was_filled=True)  # entry fills
    # Both children still pending — parent stays eligible.
    book.submit_attached(
        _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.STOP, stop_price=90.0),
        submitted_at="2024-01-03",
        submitted_equity=100_000.0,
        parent_order_id=parent.order_id,
        oco_group_id="g2",
    )
    # Cancel one of the original children — parent still has b + the new
    # leg pending, so still eligible.
    book.cancel(child_a.order_id)
    book.submit_attached(
        _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.LIMIT, limit_price=120.0),
        submitted_at="2024-01-04",
        submitted_equity=100_000.0,
        parent_order_id=parent.order_id,
        oco_group_id="g3",
    )


def test_filled_parent_auto_evicted_when_last_child_resolves() -> None:
    """Once the parent's last pending child is removed (filled or cancelled),
    the parent's id is auto-evicted from ``_known_top_level_order_ids``
    so subsequent ``submit_attached`` calls referencing the resolved
    bracket correctly fail.
    """
    book = OrderBook()
    parent, child_a, child_b = _bracket_with_two_children(book)
    book.remove(parent.order_id, was_filled=True)  # entry fills

    # Resolve child_a via terminal-fill requeue.
    book.requeue(child_a.order_id, new_remaining_qty=0.0, new_submitted_at="2024-01-03")
    # Parent still eligible — child_b is pending.
    book.submit_attached(
        _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.STOP, stop_price=90.0),
        submitted_at="2024-01-03",
        submitted_equity=100_000.0,
        parent_order_id=parent.order_id,
        oco_group_id="g4",
    )
    # Now cancel both remaining children.
    pending_children = book.children_of(parent.order_id)
    for child in pending_children:
        book.cancel(child.order_id)
    # Last child gone → parent auto-evicted. New submit_attached fails.
    with pytest.raises(ValueError, match="not a known top-level order id"):
        book.submit_attached(
            _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.STOP, stop_price=85.0),
            submitted_at="2024-01-04",
            submitted_equity=100_000.0,
            parent_order_id=parent.order_id,
            oco_group_id="g5",
        )


def test_filled_parent_evicted_when_only_child_terminal_fills() -> None:
    """The eviction also fires when the *last* child is removed via the
    terminal-fill ``requeue(0)`` path (not just via cancel) — exercises
    the ``remove(was_filled=True)`` branch on the child.
    """
    book = OrderBook()
    parent = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    child = book.submit_attached(
        _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        parent_order_id=parent.order_id,
        oco_group_id="g1",
    )
    book.remove(parent.order_id, was_filled=True)
    book.requeue(child.order_id, new_remaining_qty=0.0, new_submitted_at="2024-01-03")
    with pytest.raises(ValueError, match="not a known top-level order id"):
        book.submit_attached(
            _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.STOP, stop_price=95.0),
            submitted_at="2024-01-04",
            submitted_equity=100_000.0,
            parent_order_id=parent.order_id,
            oco_group_id="g2",
        )


def test_book_contains_membership_test() -> None:
    """``order_id in book`` exposes pending membership for iteration patterns
    (notably ``FillSimulator.process_bar``) that snapshot ``pending_for_symbol``
    and need to skip orders cascade-cancelled mid-loop.
    """
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    assert po.order_id in book
    assert "o-bogus" not in book
    book.cancel(po.order_id)
    assert po.order_id not in book


def test_submit_attached_rejects_symbol_mismatch() -> None:
    """A child must trade the same symbol as its parent. A typo would
    otherwise route the child under the wrong symbol while still tagged
    with the parent / OCO ids.
    """
    book = OrderBook()
    parent = book.submit(
        _base(qty=10.0, symbol="AAA"),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    with pytest.raises(ValueError, match="does not match parent"):
        book.submit_attached(
            _base(
                side=OrderSide.SHORT,
                qty=10.0,
                symbol="BBB",
                order_type=OrderType.LIMIT,
                limit_price=110.0,
            ),
            submitted_at="2024-01-03",
            submitted_equity=100_000.0,
            parent_order_id=parent.order_id,
            oco_group_id="g1",
        )
    assert book.children_of(parent.order_id) == []


def test_submit_attached_accepts_matching_symbol_after_parent_filled() -> None:
    """Symbol validation must work even after the parent has been removed
    via ``remove(was_filled=True)`` — the parent's symbol stays cached in
    ``_known_top_level_order_ids`` so post-fill activation can verify.
    """
    book = OrderBook()
    parent = book.submit(
        _base(qty=10.0, symbol="AAA"),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    book.remove(parent.order_id, was_filled=True)
    book.submit_attached(  # matching symbol — accepted
        _base(
            side=OrderSide.SHORT,
            qty=10.0,
            symbol="AAA",
            order_type=OrderType.LIMIT,
            limit_price=110.0,
        ),
        submitted_at="2024-01-03",
        submitted_equity=100_000.0,
        parent_order_id=parent.order_id,
        oco_group_id="g1",
    )
    with pytest.raises(ValueError, match="does not match parent"):
        book.submit_attached(
            _base(
                side=OrderSide.SHORT,
                qty=10.0,
                symbol="BBB",
                order_type=OrderType.STOP,
                stop_price=95.0,
            ),
            submitted_at="2024-01-03",
            submitted_equity=100_000.0,
            parent_order_id=parent.order_id,
            oco_group_id="g2",
        )


def test_requeue_rejects_non_string_submitted_at() -> None:
    """``new_submitted_at`` must be a string. A non-string would otherwise
    fall through to a raw ``<`` comparison and raise an unhelpful
    ``TypeError``; reject up front with a controlled message.
    """
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    for bad in (None, 123, 1.5, datetime(2024, 1, 5)):
        with pytest.raises(TypeError, match="must be a str"):
            book.requeue(po.order_id, new_remaining_qty=5.0, new_submitted_at=bad)
    # State unchanged.
    assert po.remaining_qty == 10.0


def test_no_auto_evict_while_parent_still_pending() -> None:
    """Auto-evict must only fire after the parent itself is gone from
    ``_pending``. Removing a child while the parent is still live should
    leave the parent eligible (so additional children can attach).
    """
    book = OrderBook()
    parent = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    child = book.submit_attached(
        _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        parent_order_id=parent.order_id,
        oco_group_id="g1",
    )
    book.cancel(child.order_id)
    # Parent is still pending → still eligible. Submit a new child.
    book.submit_attached(
        _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.STOP, stop_price=95.0),
        submitted_at="2024-01-03",
        submitted_equity=100_000.0,
        parent_order_id=parent.order_id,
        oco_group_id="g2",
    )


def test_cancel_only_cancels_direct_children_of_target() -> None:
    """Cascade is scoped to the cancelled parent's *own* children — an
    unrelated bracket shouldn't be touched.
    """
    book = OrderBook()
    parent_a, child_a1, child_a2 = _bracket_with_two_children(book)
    parent_b = book.submit(
        _base(qty=5.0, symbol="BBB"),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    child_b = book.submit_attached(
        _base(
            side=OrderSide.SHORT,
            qty=5.0,
            symbol="BBB",
            order_type=OrderType.LIMIT,
            limit_price=55.0,
        ),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        parent_order_id=parent_b.order_id,
        oco_group_id="g2",
    )
    book.cancel(parent_a.order_id)
    pending_ids = {po.order_id for po in book.all_pending()}
    assert pending_ids == {parent_b.order_id, child_b.order_id}
    assert child_a1 not in book.all_pending()
    assert child_a2 not in book.all_pending()


# ---------------------------------------------------------------------------
# Pre-armed bracket children: submit_attached sets ``armed=False`` while
# the parent is still pending so the simulator's fill loop skips the child
# until the bracket materializer (#389) flips it on after the parent fills.
# Children submitted *after* the parent fills (post-fill bracket activation)
# are armed immediately.
# ---------------------------------------------------------------------------


def test_submit_attached_disarmed_when_parent_still_pending() -> None:
    book = OrderBook()
    parent = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    child = book.submit_attached(
        _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        parent_order_id=parent.order_id,
        oco_group_id="g1",
    )
    assert child.armed is False
    # Parent itself is always armed — it's the entry.
    assert parent.armed is True


def test_submit_attached_armed_when_parent_already_filled() -> None:
    book = OrderBook()
    parent = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    book.remove(parent.order_id, was_filled=True)  # entry fills
    child = book.submit_attached(
        _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
        submitted_at="2024-01-03",
        submitted_equity=100_000.0,
        parent_order_id=parent.order_id,
        oco_group_id="g1",
    )
    assert child.armed is True


def test_submit_attached_rejects_market_child() -> None:
    """Market children would fire on the next bar, defeating the protective-
    leg semantic. ``submit_attached`` must reject ``OrderType.MARKET``.
    """
    book = OrderBook()
    parent = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    with pytest.raises(ValueError, match="not MARKET"):
        book.submit_attached(
            _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.MARKET),
            submitted_at="2024-01-03",
            submitted_equity=100_000.0,
            parent_order_id=parent.order_id,
            oco_group_id="g1",
        )
    assert book.children_of(parent.order_id) == []


# ---------------------------------------------------------------------------
# Bracket children must take the opposite side from the parent — protective
# legs close out the parent's position. A same-side child would be silently
# dropped by the simulator's same-side path, leaving the bracket unprotected.
# ---------------------------------------------------------------------------


def test_submit_attached_rejects_same_side_long_child() -> None:
    book = OrderBook()
    parent = book.submit(
        _base(qty=10.0, side=OrderSide.LONG),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    with pytest.raises(ValueError, match="must be opposite parent"):
        book.submit_attached(
            _base(side=OrderSide.LONG, qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
            submitted_at="2024-01-03",
            submitted_equity=100_000.0,
            parent_order_id=parent.order_id,
            oco_group_id="g1",
        )
    assert book.children_of(parent.order_id) == []


def test_submit_attached_rejects_same_side_short_child() -> None:
    book = OrderBook()
    parent = book.submit(
        _base(qty=10.0, side=OrderSide.SHORT),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    with pytest.raises(ValueError, match="must be opposite parent"):
        book.submit_attached(
            _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.STOP, stop_price=110.0),
            submitted_at="2024-01-03",
            submitted_equity=100_000.0,
            parent_order_id=parent.order_id,
            oco_group_id="g1",
        )


def test_submit_attached_accepts_short_child_for_long_parent() -> None:
    """Sanity: a LONG entry with a SHORT take-profit (protective leg) is
    the canonical bracket and must succeed.
    """
    book = OrderBook()
    parent = book.submit(
        _base(qty=10.0, side=OrderSide.LONG),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    child = book.submit_attached(
        _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
        submitted_at="2024-01-03",
        submitted_equity=100_000.0,
        parent_order_id=parent.order_id,
        oco_group_id="g1",
    )
    assert child.request.side == OrderSide.SHORT


# ---------------------------------------------------------------------------
# requeue's terminal-fill path forwards ``was_filled`` to ``remove()`` so
# future callers (e.g. partial-fill on exits in #386) can opt out of the
# eligible-parent registration.
# ---------------------------------------------------------------------------


def test_requeue_terminal_fill_default_was_filled_preserves_eligibility() -> None:
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    # Default ``was_filled=True`` (entry-style).
    book.requeue(po.order_id, new_remaining_qty=0.0, new_submitted_at="2024-01-03")
    # Parent's id is still eligible — bracket children can be activated.
    book.submit_attached(
        _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
        submitted_at="2024-01-04",
        submitted_equity=100_000.0,
        parent_order_id=po.order_id,
        oco_group_id="g1",
    )


def test_requeue_terminal_fill_was_filled_false_evicts() -> None:
    """A future caller handling a non-entry partial fill (e.g. an exit
    closing out a position) passes ``was_filled=False`` so the order's id
    is NOT registered as a future bracket parent.
    """
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    book.requeue(
        po.order_id,
        new_remaining_qty=0.0,
        new_submitted_at="2024-01-03",
        was_filled=False,
    )
    # Now the id is no longer eligible to receive bracket children.
    with pytest.raises(ValueError, match="not a known top-level order id"):
        book.submit_attached(
            _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
            submitted_at="2024-01-04",
            submitted_equity=100_000.0,
            parent_order_id=po.order_id,
            oco_group_id="g1",
        )


# ---------------------------------------------------------------------------
# expect_brackets opt-in: only orders submitted with ``expect_brackets=True``
# register in ``_known_top_level_order_ids``. Non-bracket strategies pay no
# memory cost, and stale-id reuse drops to near-zero since the set only
# contains parents the strategy has explicitly declared bracket intent for.
# ---------------------------------------------------------------------------


def test_submit_default_does_not_register_eligible_parent() -> None:
    book = OrderBook()
    parent = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        # expect_brackets omitted → defaults to False
    )
    # ``submit_attached`` correctly fails: the strategy didn't declare bracket
    # intent at submission time, so the parent's id was never registered.
    with pytest.raises(ValueError, match="not a known top-level order id"):
        book.submit_attached(
            _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
            submitted_at="2024-01-03",
            submitted_equity=100_000.0,
            parent_order_id=parent.order_id,
            oco_group_id="g1",
        )


def test_submit_with_expect_brackets_registers_eligible_parent() -> None:
    book = OrderBook()
    parent = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    # Now ``submit_attached`` succeeds — the strategy declared bracket intent.
    child = book.submit_attached(
        _base(side=OrderSide.SHORT, qty=10.0, order_type=OrderType.LIMIT, limit_price=110.0),
        submitted_at="2024-01-03",
        submitted_equity=100_000.0,
        parent_order_id=parent.order_id,
        oco_group_id="g1",
    )
    assert child.request.parent_order_id == parent.order_id


# ---------------------------------------------------------------------------
# requeue rejects non-numeric ``new_remaining_qty`` with a structured
# ``TypeError`` rather than letting ``math.isfinite`` raise its own.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_qty", ["10", b"10", None, [10.0], object()])
def test_requeue_rejects_non_numeric_remaining_qty(bad_qty) -> None:
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
    )
    with pytest.raises(TypeError, match="must be int or float"):
        book.requeue(po.order_id, new_remaining_qty=bad_qty, new_submitted_at="2024-01-03")
    # State unchanged.
    assert po.remaining_qty == 10.0


# ---------------------------------------------------------------------------
# Compact UTC offsets (``+0000`` / ``-0500``) are valid ISO 8601 but Python
# 3.10's ``datetime.fromisoformat`` rejects them; ``_normalize_ts`` rewrites
# them before parse so the regression check works correctly.
# ---------------------------------------------------------------------------


def test_requeue_handles_compact_offset_in_regression_check() -> None:
    """A compact-offset timestamp must compare correctly against an
    already-stored equivalent ``+00:00``-form timestamp — same instant
    must not raise as a regression.
    """
    book = OrderBook()
    po = book.submit(
        _base(qty=10.0),
        submitted_at="2024-01-02T10:00:00+00:00",
        submitted_equity=100_000.0,
    )
    # Same instant in compact form — must not raise.
    book.requeue(
        po.order_id,
        new_remaining_qty=4.0,
        new_submitted_at="2024-01-02T10:00:00+0000",
    )
    assert po.remaining_qty == 4.0


def test_submit_attached_market_rejection_message_lists_supported_types() -> None:
    """The MARKET-rejection message must point callers at types that are
    actually supported (``LIMIT`` / ``STOP``) — not at ``STOP_LIMIT``
    (doesn't exist) or ``TRAILING_STOP`` (still gated until #390).
    """
    book = OrderBook()
    parent = book.submit(
        _base(qty=10.0, side=OrderSide.LONG),
        submitted_at="2024-01-02",
        submitted_equity=100_000.0,
        expect_brackets=True,
    )
    with pytest.raises(ValueError) as excinfo:
        book.submit_attached(
            _base(qty=10.0, side=OrderSide.SHORT, order_type=OrderType.MARKET),
            submitted_at="2024-01-03",
            submitted_equity=100_000.0,
            parent_order_id=parent.order_id,
            oco_group_id="g1",
        )
    msg = str(excinfo.value)
    assert "LIMIT or STOP" in msg
    # Make sure we're not pointing callers at non-existent or gated types.
    assert "STOP_LIMIT" not in msg
