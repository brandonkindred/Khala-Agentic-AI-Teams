"""Bracket-attachment STOP_LIMIT tests.

``StopAttachment.limit_offset`` materializes the protective stop leg as a
STOP_LIMIT child (a stop that, once triggered, rests as a limit ``limit_offset``
away from the stop). The bracket path is the supported home for stop-limits
because its children are GTC + ``REQUEUE_NEXT_BAR`` and so tolerate the
gap-through non-fill natively. ``trail_offset`` and ``limit_offset`` are mutually
exclusive (a trailing stop-limit child is out of scope).
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


def test_limit_offset_validates_and_materializes_stop_limit_child() -> None:
    """``StopAttachment(limit_offset=...)`` materializes a STOP_LIMIT child with
    the limit on the protective side of the stop (abs and bps variants)."""
    # Both abs and bps variants validate at submission time.
    OrderRequest(
        client_order_id="entry-1",
        symbol="AAA",
        side=OrderSide.LONG,
        qty=10.0,
        order_type=OrderType.MARKET,
        attached_stop_loss=StopAttachment(stop_price=95.0, limit_offset=1.0),
    ).validate_prices()
    OrderRequest(
        client_order_id="entry-1",
        symbol="AAA",
        side=OrderSide.LONG,
        qty=10.0,
        order_type=OrderType.MARKET,
        attached_stop_loss=StopAttachment(
            stop_price=95.0, limit_offset=50.0, limit_offset_kind="bps"
        ),
    ).validate_prices()

    sim, order_book, _portfolio = _make_simulator()
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
        ),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
        expect_brackets=True,
    )
    sim.process_bar(_bar("2024-01-02", open_price=100.0))

    children = order_book.children_of(parent.order_id)
    sl = next(c for c in children if c.request.order_type == OrderType.STOP_LIMIT)
    # Long parent → SHORT sell-stop-limit child; limit one unit below the stop.
    assert sl.request.side == OrderSide.SHORT
    assert sl.request.stop_price == pytest.approx(95.0, rel=1e-9)
    assert sl.request.limit_price == pytest.approx(94.0, rel=1e-9)


def test_limit_offset_bps_computes_relative_to_stop() -> None:
    sim, order_book, _portfolio = _make_simulator()
    parent = order_book.submit(
        OrderRequest(
            client_order_id="entry-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=10.0,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
            attached_stop_loss=StopAttachment(
                stop_price=100.0, limit_offset=200.0, limit_offset_kind="bps"
            ),
        ),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
        expect_brackets=True,
    )
    sim.process_bar(_bar("2024-01-02", open_price=100.0))
    sl = next(
        c
        for c in order_book.children_of(parent.order_id)
        if c.request.order_type == OrderType.STOP_LIMIT
    )
    # 200 bps of 100 = 2.0 → limit at 98.0.
    assert sl.request.limit_price == pytest.approx(98.0, rel=1e-9)


def test_bracket_stop_limit_gap_through_leaves_position_open_and_tp_live() -> None:
    """The protective stop-limit triggers but gaps through its limit: the
    position stays open, the OCO take-profit sibling stays live, and no
    reverse position is opened."""
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
        ),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
        expect_brackets=True,
    )
    sim.process_bar(_bar("2024-01-02", open_price=100.0))

    # Gap down through the 94.0 limit: low=80 (< stop 95 triggers), high=85 (< limit 94).
    outcome = sim.process_bar(_bar("2024-01-03", open_price=85.0, high=85.0, low=80.0, close=82.0))
    assert outcome.exit_fills == []
    assert "AAA" in portfolio.positions
    assert portfolio.positions["AAA"].side == OrderSide.LONG  # not reversed
    assert any(e.kind == "stop_limit_unfilled" for e in outcome.diagnostic_events)
    # Both OCO legs remain on the book (neither filled).
    children = order_book.children_of(parent.order_id)
    types = {c.request.order_type for c in children}
    assert OrderType.STOP_LIMIT in types
    assert OrderType.LIMIT in types


def test_trail_offset_and_limit_offset_together_rejected() -> None:
    with pytest.raises(ValueError, match="both trail_offset and limit_offset"):
        OrderRequest(
            client_order_id="entry-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=10.0,
            order_type=OrderType.MARKET,
            attached_stop_loss=StopAttachment(stop_price=95.0, trail_offset=2.0, limit_offset=1.0),
        ).validate_prices()


def test_negative_limit_offset_rejected() -> None:
    with pytest.raises(ValueError, match="limit_offset must be non-negative"):
        OrderRequest(
            client_order_id="entry-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=10.0,
            order_type=OrderType.MARKET,
            attached_stop_loss=StopAttachment(stop_price=95.0, limit_offset=-1.0),
        ).validate_prices()
