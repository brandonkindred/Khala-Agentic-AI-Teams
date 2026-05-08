"""Bracket / OCO materialization tests (Trading 5/5 Step 7 — issue #389).

Covers the five acceptance bullets from the issue:

1. Entry fill materializes two children with shared ``oco_group_id`` and
   ``parent_order_id`` set.
2. Take-profit fills next bar → stop-loss removed from book (OCO).
3. Stop-loss fills first → take-profit removed.
4. Parent never fills (e.g. LIMIT not crossed) → no children materialized.
5. Children not eligible for fill on the same bar as parent entry (bar-safety).

A sixth case for trailing stops will land with #390 (Step 8).
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


def _make_simulator(
    initial_capital: float = 10_000_000.0,
) -> tuple[FillSimulator, OrderBook, Portfolio]:
    portfolio = Portfolio(initial_capital=initial_capital)
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


def _bracket_entry(
    *,
    qty: float = 10.0,
    stop_price: float | None = 95.0,
    take_profit_price: float | None = 110.0,
    side: OrderSide = OrderSide.LONG,
    order_type: OrderType = OrderType.MARKET,
    limit_price: float | None = None,
) -> OrderRequest:
    return OrderRequest(
        client_order_id="entry-1",
        symbol="AAA",
        side=side,
        qty=qty,
        order_type=order_type,
        limit_price=limit_price,
        tif=TimeInForce.DAY,
        attached_stop_loss=StopAttachment(stop_price=stop_price)
        if stop_price is not None
        else None,
        attached_take_profit=(
            LimitAttachment(limit_price=take_profit_price)
            if take_profit_price is not None
            else None
        ),
    )


# ---------------------------------------------------------------------------
# 1. Entry fill materializes both children with correct shape
# ---------------------------------------------------------------------------


def test_entry_fill_materializes_stop_and_take_profit_children() -> None:
    sim, order_book, portfolio = _make_simulator()
    parent = order_book.submit(
        _bracket_entry(qty=10.0, stop_price=95.0, take_profit_price=110.0),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
        expect_brackets=True,
    )

    outcome = sim.process_bar(_bar("2024-01-02", open_price=100.0, volume=1_000_000.0))

    # Entry fully filled on bar 2.
    assert len(outcome.entry_fills) == 1
    assert outcome.entry_fills[0].qty == pytest.approx(10.0, rel=1e-9)
    # Parent removed from the book (terminal-fill path).
    assert parent.order_id not in order_book

    # Two children attached against the parent.
    children = order_book.children_of(parent.order_id)
    assert len(children) == 2
    expected_oco = f"oco_{parent.order_id}"
    sl = next(c for c in children if c.request.order_type == OrderType.STOP)
    tp = next(c for c in children if c.request.order_type == OrderType.LIMIT)

    for child in (sl, tp):
        assert child.armed is True
        # Opposite side to LONG parent.
        assert child.request.side == OrderSide.SHORT
        assert child.request.symbol == "AAA"
        assert child.request.qty == pytest.approx(10.0, rel=1e-9)
        assert child.request.parent_order_id == parent.order_id
        assert child.request.oco_group_id == expected_oco
        # GTC so the bracket survives across sessions; bar-safety blocks
        # same-bar fills via ``submitted_at`` (see test 5).
        assert child.request.tif == TimeInForce.GTC
        assert child.submitted_at == "2024-01-02"

    assert sl.request.stop_price == pytest.approx(95.0, rel=1e-9)
    assert sl.request.reason == "bracket_sl"
    assert tp.request.limit_price == pytest.approx(110.0, rel=1e-9)
    assert tp.request.reason == "bracket_tp"

    # Position is open; no trade closed yet.
    assert portfolio.positions["AAA"].qty == pytest.approx(10.0, rel=1e-9)
    assert outcome.closed_trades == []


# ---------------------------------------------------------------------------
# 2. Take-profit fill cancels stop-loss sibling
# ---------------------------------------------------------------------------


def test_take_profit_fill_cancels_stop_sibling() -> None:
    sim, order_book, portfolio = _make_simulator()
    parent = order_book.submit(
        _bracket_entry(qty=10.0, stop_price=95.0, take_profit_price=110.0),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
        expect_brackets=True,
    )

    # Bar 2: entry fills, children materialized.
    sim.process_bar(_bar("2024-01-02", open_price=100.0))

    # Bar 3: high crosses 110 → SHORT LIMIT take-profit fires.
    outcome = sim.process_bar(
        _bar("2024-01-03", open_price=108.0, high=112.0, low=107.0, close=111.0)
    )

    assert len(outcome.exit_fills) == 1
    assert outcome.exit_fills[0].price == pytest.approx(110.0, rel=1e-9)
    assert len(outcome.closed_trades) == 1
    # Position is closed, OCO siblings cleared from the book.
    assert "AAA" not in portfolio.positions
    assert order_book.children_of(parent.order_id) == []
    assert order_book.all_pending() == []


# ---------------------------------------------------------------------------
# 3. Stop-loss fill cancels take-profit sibling (mirror of test 2)
# ---------------------------------------------------------------------------


def test_stop_loss_fill_cancels_take_profit_sibling() -> None:
    sim, order_book, portfolio = _make_simulator()
    parent = order_book.submit(
        _bracket_entry(qty=10.0, stop_price=95.0, take_profit_price=110.0),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
        expect_brackets=True,
    )

    sim.process_bar(_bar("2024-01-02", open_price=100.0))

    # Bar 3: low crosses 95 → SHORT STOP stop-loss fires.
    outcome = sim.process_bar(_bar("2024-01-03", open_price=97.0, high=98.0, low=93.0, close=94.0))

    assert len(outcome.exit_fills) == 1
    # Stop child fills at ``min(bar.open, stop_price)`` for SHORT stop —
    # i.e. at 95 (bar opens above the stop level).
    assert outcome.exit_fills[0].price == pytest.approx(95.0, rel=1e-9)
    assert len(outcome.closed_trades) == 1
    assert "AAA" not in portfolio.positions
    assert order_book.children_of(parent.order_id) == []
    assert order_book.all_pending() == []


# ---------------------------------------------------------------------------
# 4. Parent never fills → no children materialized
# ---------------------------------------------------------------------------


def test_unfilled_limit_parent_does_not_materialize_children() -> None:
    """A LIMIT parent whose limit price never crosses on the fill bar leaves
    the parent pending and produces no bracket children."""
    sim, order_book, _ = _make_simulator()
    parent = order_book.submit(
        _bracket_entry(
            qty=10.0,
            stop_price=95.0,
            take_profit_price=110.0,
            order_type=OrderType.LIMIT,
            # Buy limit at 90 — never crosses on the bars we feed below.
            limit_price=90.0,
        ),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
        expect_brackets=True,
    )

    outcome = sim.process_bar(
        _bar("2024-01-02", open_price=100.0, high=105.0, low=98.0, close=102.0)
    )

    # Parent did not fill, no children materialized.
    assert outcome.entry_fills == []
    assert order_book.children_of(parent.order_id) == []
    # Parent still in the book waiting for its limit to cross.
    assert parent.order_id in order_book


# ---------------------------------------------------------------------------
# 5. Children not eligible for fill on the same bar as parent entry
# ---------------------------------------------------------------------------


def test_children_not_eligible_for_fill_on_entry_bar() -> None:
    """Bar-safety guard: children submitted with ``submitted_at=bar.timestamp``
    cannot fill on the same bar — even if the bar's range covers their
    trigger prices. Children remain pending until the next bar."""
    sim, order_book, portfolio = _make_simulator()
    parent = order_book.submit(
        _bracket_entry(qty=10.0, stop_price=95.0, take_profit_price=110.0),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
        expect_brackets=True,
    )

    # Bar 2's range covers BOTH child trigger prices (low=92 < stop=95;
    # high=115 > tp=110). Despite that, children must not fill on this
    # bar — only the entry fills.
    outcome = sim.process_bar(
        _bar("2024-01-02", open_price=100.0, high=115.0, low=92.0, close=100.0)
    )

    assert len(outcome.entry_fills) == 1
    assert outcome.exit_fills == []
    assert outcome.closed_trades == []

    children = order_book.children_of(parent.order_id)
    assert len(children) == 2
    for child in children:
        assert child.armed is True
        # ``submitted_at`` anchored to the entry-fill bar so bar-safety
        # blocks same-bar fills.
        assert child.submitted_at == "2024-01-02"
        # Still pending — has not filled.
        assert child.cumulative_filled_qty == 0.0

    assert portfolio.positions["AAA"].qty == pytest.approx(10.0, rel=1e-9)


# ---------------------------------------------------------------------------
# Regression guard: the no-attachment path is byte-identical to pre-#389
# ---------------------------------------------------------------------------


def test_no_attachment_path_is_unchanged() -> None:
    """Plain MARKET entry without attachments: no children created, no
    eligible-parent registration leakage, simple round-trip identical to
    pre-#389 behavior. Protects against the materializer accidentally
    firing on the no-bracket path."""
    sim, order_book, portfolio = _make_simulator()
    plain_entry = OrderRequest(
        client_order_id="entry-1",
        symbol="AAA",
        side=OrderSide.LONG,
        qty=10.0,
        order_type=OrderType.MARKET,
        tif=TimeInForce.DAY,
    )
    parent = order_book.submit(
        plain_entry,
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
        # expect_brackets left at default ``False``.
    )

    sim.process_bar(_bar("2024-01-02", open_price=100.0))
    assert order_book.children_of(parent.order_id) == []

    plain_exit = OrderRequest(
        client_order_id="exit-1",
        symbol="AAA",
        side=OrderSide.SHORT,
        qty=10.0,
        order_type=OrderType.MARKET,
        tif=TimeInForce.DAY,
    )
    order_book.submit(
        plain_exit,
        submitted_at="2024-01-02",
        submitted_equity=10_000_000.0,
    )
    outcome = sim.process_bar(_bar("2024-01-03", open_price=105.0))

    assert len(outcome.closed_trades) == 1
    assert "AAA" not in portfolio.positions
    assert order_book.all_pending() == []
