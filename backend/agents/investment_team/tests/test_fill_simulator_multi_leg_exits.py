"""Multi-leg resting-exit materialization tests (issue #7509, step 2 of 3).

Step 1 (#7494) generalized the pure ``resolve_exit_leg_attachments`` helper
to resolve an arbitrary ordered list of leg specs into ``StopAttachment`` /
``LimitAttachment`` objects. This step extends ``OrderRequest`` with a
generic ``attached_exits`` list and the fill simulator's materialization
step (``FillSimulator._materialize_bracket_children``) to submit an
arbitrary number of those as resting OCO children — not just the two fixed
``attached_stop_loss``/``attached_take_profit`` bracket fields.

No DSL/dispatcher wiring is exercised here (that's out of scope until the
rule-kind migration issues); every request below is constructed directly,
the same way ``test_exit_leg_attachments.py`` exercises the Step 1 resolver
directly. The arm/latch/gap-through/trailing *lifecycle* itself is not
touched by this step — these tests exist to prove that lifecycle, and OCO
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
    bare = OrderRequest(client_order_id="entry-1", symbol="AAA", side=OrderSide.LONG, qty=10.0)
    assert bare.has_attached_exits is False

    with_bracket_field = bare.model_copy(
        update={"attached_stop_loss": StopAttachment(stop_price=95.0)}
    )
    assert with_bracket_field.has_attached_exits is True

    with_exits_list = bare.model_copy(
        update={"attached_exits": [LimitAttachment(limit_price=110.0)]}
    )
    assert with_exits_list.has_attached_exits is True


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
    """A STOP_LIMIT leg reuses the same gap-through handling whether it's a
    bracket field or one leg among several ``attached_exits`` — the
    per-``PendingOrder`` lifecycle in ``process_bar`` doesn't know or care
    how many siblings share its ``oco_group_id``."""
    sim, order_book, portfolio = _make_simulator()
    parent = order_book.submit(
        OrderRequest(
            client_order_id="entry-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=10.0,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
            attached_stop_loss=StopAttachment(stop_price=95.0, limit_offset=1.0),
            attached_take_profit=LimitAttachment(limit_price=110.0),
            attached_exits=[LimitAttachment(limit_price=130.0)],
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
