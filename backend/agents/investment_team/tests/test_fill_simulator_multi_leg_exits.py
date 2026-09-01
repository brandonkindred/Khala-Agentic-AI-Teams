"""Multi-leg resting-exit materialization tests.

An earlier step generalized the pure ``resolve_exit_leg_attachments`` helper
to resolve an arbitrary ordered list of leg specs into ``StopAttachment`` /
``LimitAttachment`` objects. This step extends ``OrderRequest`` with a
generic ``attached_exits`` list and the fill simulator's materialization
step (``FillSimulator._materialize_attached_exit_children``) to submit an
arbitrary number of those as resting OCO children — not just the two fixed
``attached_stop_loss``/``attached_take_profit`` bracket fields. Step 3
closes the remaining coverage on that plumbing: every leg *kind*
(not just LIMIT) independently firing and cancelling its siblings, an
all-stop-family group with no LIMIT leg at all, and the ``"bps"``
``trail_offset_kind`` pre-seed path for a generalized trailing leg (the
shape ``resolve_exit_leg_attachments`` actually produces for a
``TRAILING_STOP`` leg, as opposed to the ``"abs"`` default used by earlier
tests in this file).

No DSL/dispatcher wiring is exercised here (that's out of scope until the
rule-kind migration issues); every request below is constructed directly,
the same way ``test_exit_leg_attachments.py`` exercises the Step 1 resolver
directly. The arm/latch/gap-through/trailing *lifecycle* itself is not
touched by these steps — these tests exist to prove that lifecycle, and OCO
sibling cancellation, already work unchanged for N > 2 independently
attached children; ``test_bracket_orders.py`` / ``test_bracket_stop_limit.py``
cover the 2-leg bracket path and must keep passing unmodified.
"""

from __future__ import annotations

import pytest

from investment_team.execution.bar_safety import BarSafetyAssertion
from investment_team.execution.risk_filter import RiskFilter, RiskLimits
from investment_team.trading_service.engine.execution_model import (
    RealisticExecutionModel,
)
from investment_team.trading_service.engine.fill_simulator import (
    FillSimulator,
    FillSimulatorConfig,
)
from investment_team.trading_service.engine.order_book import OrderBook
from investment_team.trading_service.engine.portfolio import Portfolio
from investment_team.trading_service.strategy.contract import (
    Bar,
    LimitAttachment,
    OrderRequest,
    OrderSide,
    OrderType,
    StopAttachment,
    TimeInForce,
    UnfilledPolicy,
)


def _bar(
    ts: str,
    *,
    open_price: float = 100.0,
    high: float | None = None,
    low: float | None = None,
    close: float | None = None,
    volume: float = 1_000_000.0,
) -> Bar:
    return Bar(
        symbol="AAA",
        timestamp=ts,
        timeframe="1d",
        open=open_price,
        high=high if high is not None else open_price + 1.0,
        low=low if low is not None else open_price - 1.0,
        close=close if close is not None else open_price,
        volume=volume,
    )


def _make_simulator() -> tuple[FillSimulator, OrderBook, Portfolio]:
    portfolio = Portfolio(initial_capital=10_000_000.0)
    order_book = OrderBook()
    sim = FillSimulator(
        portfolio=portfolio,
        order_book=order_book,
        risk_filter=RiskFilter(RiskLimits(max_position_pct=100, max_gross_leverage=10.0)),
        config=FillSimulatorConfig(slippage_bps=0.0, transaction_cost_bps=0.0),
        bar_safety=BarSafetyAssertion(),
        execution_model=RealisticExecutionModel(participation_cap=0.10),
    )
    return sim, order_book, portfolio


# ---------------------------------------------------------------------------
# OrderRequest.has_attached_exits / validate_prices
# ---------------------------------------------------------------------------


def test_has_attached_exits_property() -> None:
    """``has_attached_exits`` is True for either bracket field or a
    non-empty ``attached_exits`` list, and False for an empty one — an
    empty list must not be treated as "has legs to materialize", which
    would otherwise submit a zero-child OCO group on entry fill."""
    bare = OrderRequest(client_order_id="entry-1", symbol="AAA", side=OrderSide.LONG, qty=10.0)
    assert bare.has_attached_exits is False

    empty_exits = bare.model_copy(update={"attached_exits": []})
    assert empty_exits.has_attached_exits is False

    with_bracket_field = bare.model_copy(
        update={"attached_stop_loss": StopAttachment(stop_price=95.0)}
    )
    assert with_bracket_field.has_attached_exits is True

    with_exits_list = bare.model_copy(
        update={"attached_exits": [LimitAttachment(limit_price=110.0)]}
    )
    assert with_exits_list.has_attached_exits is True


def test_attached_exits_stop_leg_rejects_negative_trail_offset() -> None:
    """A ``StopAttachment`` in ``attached_exits`` gets the same offset
    validation as the fixed ``attached_stop_loss`` field — a negative
    ``trail_offset`` must be rejected at submission, not surface later as
    an unprotected position when ``_materialize_stop_child`` runs."""
    with pytest.raises(ValueError, match=r"attached_exits\[0\]\.trail_offset must be non-negative"):
        OrderRequest(
            client_order_id="entry-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=10.0,
            order_type=OrderType.MARKET,
            attached_exits=[StopAttachment(stop_price=95.0, trail_offset=-2.0)],
        ).validate_prices()


def test_attached_exits_stop_leg_rejects_negative_limit_offset() -> None:
    """A negative ``limit_offset`` on an ``attached_exits`` stop leg is
    rejected, mirroring the bracket-field rule."""
    with pytest.raises(ValueError, match=r"attached_exits\[0\]\.limit_offset must be non-negative"):
        OrderRequest(
            client_order_id="entry-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=10.0,
            order_type=OrderType.MARKET,
            attached_exits=[StopAttachment(stop_price=95.0, limit_offset=-1.0)],
        ).validate_prices()


def test_attached_exits_stop_leg_rejects_trail_and_limit_offset_together() -> None:
    """``trail_offset`` and ``limit_offset`` remain mutually exclusive on an
    ``attached_exits`` stop leg, mirroring the bracket-field rule."""
    with pytest.raises(
        ValueError, match=r"attached_exits\[0\] cannot set both trail_offset and limit_offset"
    ):
        OrderRequest(
            client_order_id="entry-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=10.0,
            order_type=OrderType.MARKET,
            attached_exits=[StopAttachment(stop_price=95.0, trail_offset=2.0, limit_offset=1.0)],
        ).validate_prices()


def test_attached_exits_second_stop_leg_offsets_are_also_validated() -> None:
    """A valid first leg must not shadow validation of a later one."""
    with pytest.raises(ValueError, match=r"attached_exits\[1\]\.trail_offset must be non-negative"):
        OrderRequest(
            client_order_id="entry-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=10.0,
            order_type=OrderType.MARKET,
            attached_exits=[
                StopAttachment(stop_price=95.0),
                StopAttachment(stop_price=90.0, trail_offset=-1.0),
            ],
        ).validate_prices()


# ---------------------------------------------------------------------------
# Materialization: N > 2 independently-attached children from attached_exits
# ---------------------------------------------------------------------------


def test_entry_fill_materializes_n_attached_exits_children() -> None:
    """Four legs — STOP, STOP_LIMIT, TRAILING_STOP, LIMIT — all attached via
    ``attached_exits`` (no bracket fields at all) materialize as four
    independent OCO children sharing one ``oco_group_id``."""
    sim, order_book, _portfolio = _make_simulator()
    parent = order_book.submit(
        OrderRequest(
            client_order_id="entry-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=10.0,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
            attached_exits=[
                StopAttachment(stop_price=90.0),
                StopAttachment(stop_price=93.0, limit_offset=1.0),
                StopAttachment(stop_price=92.0, trail_offset=2.0),
                LimitAttachment(limit_price=110.0),
            ],
        ),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
        expect_brackets=True,
    )

    sim.process_bar(_bar("2024-01-02", open_price=100.0))

    children = order_book.children_of(parent.order_id)
    assert len(children) == 4
    expected_oco = f"oco_{parent.order_id}"
    for idx, child in enumerate(children):
        assert child.request.parent_order_id == parent.order_id
        assert child.request.oco_group_id == expected_oco
        assert child.request.side == OrderSide.SHORT
        assert child.request.qty == pytest.approx(10.0, rel=1e-9)
        assert child.request.tif == TimeInForce.GTC

    types = [c.request.order_type for c in children]
    assert types == [
        OrderType.STOP,
        OrderType.STOP_LIMIT,
        OrderType.TRAILING_STOP,
        OrderType.LIMIT,
    ]
    reasons = [c.request.reason for c in children]
    assert reasons == [
        "engine_exit:exit_leg_0",
        "engine_exit:exit_leg_1",
        "engine_exit:exit_leg_2",
        "engine_exit:exit_leg_3",
    ]
    client_order_ids = [c.request.client_order_id for c in children]
    assert client_order_ids == [
        "entry-1_exit0",
        "entry-1_exit1",
        "entry-1_exit2",
        "entry-1_exit3",
    ]

    # Trailing leg (index 2) is pre-seeded from the entry's actual fill
    # price (100.0), exactly like a bracket trailing-stop child.
    trailing_child = children[2]
    assert trailing_child.trailing_water == pytest.approx(100.0, rel=1e-9)
    assert trailing_child.effective_stop_price == pytest.approx(98.0, rel=1e-9)


def test_attached_exits_oco_cancel_is_n_way_not_paired() -> None:
    """When one of three independently-attached legs fills, *both* other
    siblings are cancelled — proving ``oco_cancel_siblings`` (already
    N-way generic) isn't just removing a single paired sibling."""
    sim, order_book, portfolio = _make_simulator()
    parent = order_book.submit(
        OrderRequest(
            client_order_id="entry-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=10.0,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
            attached_exits=[
                StopAttachment(stop_price=90.0),
                StopAttachment(stop_price=85.0),
                LimitAttachment(limit_price=110.0),
            ],
        ),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
        expect_brackets=True,
    )

    sim.process_bar(_bar("2024-01-02", open_price=100.0))
    assert len(order_book.children_of(parent.order_id)) == 3

    # High crosses 110 (target fires); low stays well above both stops.
    outcome = sim.process_bar(
        _bar("2024-01-03", open_price=108.0, high=112.0, low=107.0, close=111.0)
    )

    assert len(outcome.exit_fills) == 1
    assert outcome.exit_fills[0].price == pytest.approx(110.0, rel=1e-9)
    assert "AAA" not in portfolio.positions
    assert order_book.children_of(parent.order_id) == []
    assert order_book.all_pending() == []


def test_bracket_fields_and_attached_exits_materialize_together() -> None:
    """A request may carry the fixed bracket fields *and* generalized
    ``attached_exits`` legs at once — all children share one
    ``oco_group_id`` and get non-colliding default ``client_order_id``s."""
    sim, order_book, _portfolio = _make_simulator()
    parent = order_book.submit(
        OrderRequest(
            client_order_id="entry-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=10.0,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
            attached_stop_loss=StopAttachment(stop_price=95.0),
            attached_take_profit=LimitAttachment(limit_price=110.0),
            attached_exits=[LimitAttachment(limit_price=120.0)],
        ),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
        expect_brackets=True,
    )

    sim.process_bar(_bar("2024-01-02", open_price=100.0))

    children = order_book.children_of(parent.order_id)
    assert len(children) == 3
    expected_oco = f"oco_{parent.order_id}"
    assert {c.request.oco_group_id for c in children} == {expected_oco}

    by_reason = {c.request.reason: c for c in children}
    assert set(by_reason) == {
        "engine_exit:bracket_sl",
        "engine_exit:bracket_tp",
        "engine_exit:exit_leg_0",
    }
    assert by_reason["engine_exit:bracket_sl"].request.client_order_id == "entry-1_sl"
    assert by_reason["engine_exit:bracket_tp"].request.client_order_id == "entry-1_tp"
    assert by_reason["engine_exit:exit_leg_0"].request.client_order_id == "entry-1_exit0"
    assert by_reason["engine_exit:exit_leg_0"].request.limit_price == pytest.approx(120.0, rel=1e-9)


def test_stop_limit_leg_among_multiple_exits_still_gap_throughs() -> None:
    """A STOP_LIMIT leg materialized from ``attached_exits`` (not the fixed
    ``attached_stop_loss`` field) reuses the exact same gap-through
    handling as a bracket field — the per-``PendingOrder`` lifecycle in
    ``process_bar`` doesn't know or care which field (or list index) a
    leg's materialization came from, or how many siblings share its
    ``oco_group_id``."""
    sim, order_book, portfolio = _make_simulator()
    parent = order_book.submit(
        OrderRequest(
            client_order_id="entry-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=10.0,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
            attached_take_profit=LimitAttachment(limit_price=110.0),
            attached_exits=[
                StopAttachment(stop_price=95.0, limit_offset=1.0),
                LimitAttachment(limit_price=130.0),
            ],
        ),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
        expect_brackets=True,
    )
    sim.process_bar(_bar("2024-01-02", open_price=100.0))
    assert len(order_book.children_of(parent.order_id)) == 3

    # Gap down through the 94.0 limit: low=80 (< stop 95 triggers),
    # high=85 (< limit 94, and well short of both LIMIT targets).
    outcome = sim.process_bar(_bar("2024-01-03", open_price=85.0, high=85.0, low=80.0, close=82.0))

    assert outcome.exit_fills == []
    assert "AAA" in portfolio.positions
    assert portfolio.positions["AAA"].side == OrderSide.LONG
    assert any(e.kind == "stop_limit_unfilled" for e in outcome.diagnostic_events)
    # All three legs remain live — nothing filled or was incorrectly cancelled.
    children = order_book.children_of(parent.order_id)
    assert len(children) == 3
    types = [c.request.order_type for c in children]
    assert types.count(OrderType.STOP_LIMIT) == 1
    assert types.count(OrderType.LIMIT) == 2


def test_twap_age_out_materializes_attached_exits_legs_for_open_position() -> None:
    """A TWAP_N entry carrying only generalized ``attached_exits`` legs
    (no fixed bracket fields) that ages out via the no-trigger counter
    must still materialize protective children sized to the partially
    filled position — the same ``_maybe_materialize_brackets_on_abandon``
    hook covered for the fixed bracket fields by
    ``test_bracket_orders.py::test_twap_age_out_bracket_parent_materializes_brackets_for_open_position``
    delegates to the same shared per-leg helpers, so it must not silently
    drop ``attached_exits`` legs on this abandon path."""
    sim, order_book, portfolio = _make_simulator()
    parent = order_book.submit(
        OrderRequest(
            client_order_id="entry-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=200.0,
            order_type=OrderType.LIMIT,
            limit_price=100.0,
            tif=TimeInForce.DAY,
            unfilled_policy=UnfilledPolicy.TWAP_N,
            twap_slices=2,
            attached_exits=[
                StopAttachment(stop_price=95.0),
                LimitAttachment(limit_price=110.0),
            ],
        ),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
        expect_brackets=True,
    )

    # Bar 2: low (98) <= limit_price (100) triggers a partial fill on a
    # low-ADV bar (200 * 100 notional vs 1_000 * 100 dollar volume -> raw
    # 0.20 -> cap clips to 0.5 -> 100 fills, 100 requeued;
    # ``twap_slices_remaining`` seeded to 1).
    sim.process_bar(
        _bar("2024-01-02", open_price=99.0, high=101.0, low=98.0, close=100.0, volume=1_000.0)
    )
    assert portfolio.positions["AAA"].qty == pytest.approx(100.0, rel=1e-9)
    assert parent.order_id in order_book
    assert order_book.children_of(parent.order_id) == []

    # Bar 3: bar.low (104) > limit_price (100) -> no trigger ->
    # ``twap_slices_remaining`` decrements from 1 to 0 -> parent removed
    # with ``was_filled=True`` -> abandon hook materializes the
    # ``attached_exits`` legs sized to the 100-share open position.
    sim.process_bar(_bar("2024-01-03", open_price=105.0, high=107.0, low=104.0, close=106.0))

    assert parent.order_id not in order_book
    children = order_book.children_of(parent.order_id)
    assert len(children) == 2, "TWAP-aged-out attached_exits legs must still materialize"
    sl = next(c for c in children if c.request.order_type == OrderType.STOP)
    tp = next(c for c in children if c.request.order_type == OrderType.LIMIT)
    assert sl.request.qty == pytest.approx(100.0, rel=1e-9)
    assert tp.request.qty == pytest.approx(100.0, rel=1e-9)
    assert sl.armed is True and tp.armed is True
    expected_oco = f"oco_{parent.order_id}"
    assert sl.request.oco_group_id == expected_oco
    assert tp.request.oco_group_id == expected_oco
    assert portfolio.positions["AAA"].qty == pytest.approx(100.0, rel=1e-9)


# ---------------------------------------------------------------------------
# Independent fill/cancel for every leg kind, an
# all-stop-family group with no LIMIT sibling, and the "bps" trailing
# pre-seed path.
# ---------------------------------------------------------------------------


def test_stop_leg_fill_among_multiple_attached_exits_cancels_siblings() -> None:
    """The existing N-way-cancel test (above) only ever drives the LIMIT leg
    to fire first. This proves the mirror case: a plain STOP leg firing
    among three independently-attached, mixed-kind siblings cancels *both*
    others (a TRAILING_STOP and a LIMIT) — the fill/cancel outcome doesn't
    depend on which leg *kind* happens to be the one that triggers."""
    sim, order_book, portfolio = _make_simulator()
    parent = order_book.submit(
        OrderRequest(
            client_order_id="entry-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=10.0,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
            attached_exits=[
                StopAttachment(stop_price=95.0),
                StopAttachment(stop_price=80.0, trail_offset=15.0),
                LimitAttachment(limit_price=200.0),
            ],
        ),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
        expect_brackets=True,
    )

    sim.process_bar(_bar("2024-01-02", open_price=100.0))
    assert len(order_book.children_of(parent.order_id)) == 3

    # low=94 crosses the plain STOP (95) but stays well above the trailing
    # leg's pre-seeded effective stop (100 - 15 = 85) and well below the
    # LIMIT target (200) — only the STOP leg triggers this bar.
    outcome = sim.process_bar(_bar("2024-01-03", open_price=97.0, high=99.0, low=94.0, close=95.0))

    assert len(outcome.exit_fills) == 1
    assert outcome.exit_fills[0].price == pytest.approx(95.0, rel=1e-9)
    assert "AAA" not in portfolio.positions
    assert order_book.children_of(parent.order_id) == []
    assert order_book.all_pending() == []


def test_attached_exits_all_stop_family_no_limit_sibling() -> None:
    """An ``attached_exits`` group made entirely of STOP-family legs — STOP,
    STOP_LIMIT, TRAILING_STOP — with no LIMIT leg at all still materializes
    and fires/cancels correctly: nothing about arming, latching, or OCO
    cancellation implicitly depends on a LIMIT sibling existing in the
    group."""
    sim, order_book, portfolio = _make_simulator()
    parent = order_book.submit(
        OrderRequest(
            client_order_id="entry-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=10.0,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
            attached_exits=[
                StopAttachment(stop_price=95.0),
                StopAttachment(stop_price=90.0, limit_offset=1.0),
                StopAttachment(stop_price=80.0, trail_offset=15.0),
            ],
        ),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
        expect_brackets=True,
    )

    sim.process_bar(_bar("2024-01-02", open_price=100.0))
    children = order_book.children_of(parent.order_id)
    assert len(children) == 3
    types = [c.request.order_type for c in children]
    assert types == [OrderType.STOP, OrderType.STOP_LIMIT, OrderType.TRAILING_STOP]
    assert OrderType.LIMIT not in types

    # low=94 crosses only the plain STOP (95); the STOP_LIMIT (90) and the
    # trailing leg's pre-seeded effective stop (100 - 15 = 85) are untouched.
    outcome = sim.process_bar(_bar("2024-01-03", open_price=97.0, high=99.0, low=94.0, close=95.0))

    assert len(outcome.exit_fills) == 1
    assert outcome.exit_fills[0].price == pytest.approx(95.0, rel=1e-9)
    assert "AAA" not in portfolio.positions
    assert order_book.children_of(parent.order_id) == []
    assert order_book.all_pending() == []


def test_attached_exits_bps_trailing_leg_preseeds_and_ratchets_to_fill() -> None:
    """``resolve_exit_leg_attachments`` always resolves a ``TRAILING_STOP``
    leg's ``trail_offset`` as a ``"bps"`` value (see
    ``test_exit_leg_attachments.py::test_single_trailing_stop_leg_sets_trail_offset``),
    never ``"abs"`` — but every trailing leg materialized in this file so
    far used the ``"abs"`` default, leaving the ``"bps"`` branch of the
    entry-fill pre-seed (``_materialize_stop_child``'s
    ``apply_bps_offset(entry_fill_price, sl.trail_offset)``) unexercised at
    the materialization level (only covered for a *standalone* TRAILING_STOP
    order in ``test_trailing_stop.py``, not for a leg attached via
    ``attached_exits``). This drives one through pre-seed, ratchet, and
    fill, alongside two siblings (a far STOP and a far LIMIT) that must be
    cancelled when it fires.
    """
    sim, order_book, portfolio = _make_simulator()
    parent = order_book.submit(
        OrderRequest(
            client_order_id="entry-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=10.0,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
            attached_exits=[
                StopAttachment(stop_price=98.0, trail_offset=300.0, trail_offset_kind="bps"),
                StopAttachment(stop_price=50.0),
                LimitAttachment(limit_price=500.0),
            ],
        ),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
        expect_brackets=True,
    )

    # Entry fills at the bar's open (100.0): the trailing leg's pre-seed
    # must use that actual fill price, not its own nominal stop_price
    # preview (98.0) — trail_offset=300 bps of 100.0 is 3.0, so
    # effective_stop_price = 100.0 - 3.0 = 97.0.
    sim.process_bar(_bar("2024-01-02", open_price=100.0))
    children = order_book.children_of(parent.order_id)
    assert len(children) == 3
    trailing_child = next(c for c in children if c.request.order_type == OrderType.TRAILING_STOP)
    assert trailing_child.trailing_water == pytest.approx(100.0, rel=1e-9)
    assert trailing_child.effective_stop_price == pytest.approx(97.0, rel=1e-9)

    # Favorable bar: water ratchets to the new high (120), and since the
    # offset is "bps" it re-derives from that *new* water each time
    # (120 * (1 - 0.03) = 116.4), not a fixed 3.0 distance from entry.
    # low=117 stays above 116.4 so this bar ratchets without triggering.
    sim.process_bar(_bar("2024-01-03", open_price=110.0, high=120.0, low=117.0, close=118.0))
    trailing_child = next(
        c
        for c in order_book.children_of(parent.order_id)
        if c.request.order_type == OrderType.TRAILING_STOP
    )
    assert trailing_child.trailing_water == pytest.approx(120.0, rel=1e-9)
    assert trailing_child.effective_stop_price == pytest.approx(116.4, rel=1e-9)

    # Retrace bar: low=114 <= 116.4 triggers; fill = min(open=117, 116.4).
    # The far STOP (50) and LIMIT (500) siblings never come into range.
    outcome = sim.process_bar(
        _bar("2024-01-04", open_price=117.0, high=118.0, low=114.0, close=115.0)
    )

    assert len(outcome.exit_fills) == 1
    assert outcome.exit_fills[0].price == pytest.approx(116.4, rel=1e-9)
    assert "AAA" not in portfolio.positions
    assert order_book.children_of(parent.order_id) == []
    assert order_book.all_pending() == []


# ---------------------------------------------------------------------------
# The submit-side eligibility flag the service computes
# ---------------------------------------------------------------------------


def test_attached_exits_only_entry_is_registered_as_a_bracket_parent() -> None:
    """`expect_brackets` must be derived from `has_attached_exits`, not a hand-rolled OR.

    `OrderBook.submit` only registers an id as an eligible bracket parent when
    `expect_brackets` is True, and `submit_attached` rejects anything else. Every
    other test in this file passes the flag by hand, so an entry whose exit legs
    live *only* in `attached_exits` — the shape this module generalized to — has
    no coverage of the service's own computation of that flag. Deriving it from
    the two fixed bracket fields alone leaves such an entry unregistered, and
    materializing its children on fill raises "not a known top-level order id".
    """
    sim, order_book, _portfolio = _make_simulator()
    request = OrderRequest(
        client_order_id="entry-1",
        symbol="AAA",
        side=OrderSide.LONG,
        qty=10.0,
        order_type=OrderType.MARKET,
        tif=TimeInForce.DAY,
        attached_exits=[StopAttachment(stop_price=90.0)],
    )
    # No fixed bracket fields at all — the legs are in `attached_exits`.
    assert request.attached_stop_loss is None
    assert request.attached_take_profit is None
    assert request.has_attached_exits is True

    parent = order_book.submit(
        request,
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
        # Exactly what TradingService passes at its submit call site.
        expect_brackets=request.has_attached_exits,
    )

    sim.process_bar(_bar("2024-01-02", open_price=100.0))

    children = order_book.children_of(parent.order_id)
    assert len(children) == 1
    assert children[0].request.parent_order_id == parent.order_id
