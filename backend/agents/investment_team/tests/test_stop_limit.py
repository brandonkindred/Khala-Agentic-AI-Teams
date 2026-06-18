"""STOP_LIMIT runtime tests — the third stop order type (stop / stop-limit / trailing).

A stop-limit triggers like a plain STOP (stop level crossed) but then rests as a
LIMIT at a separate ``limit_price``. The defining behavior — and the one way it
diverges from a plain STOP, which always fills once triggered — is the
**gap-through non-fill**: if the bar gaps entirely through the limit, the order
does not fill, stays resting, and the position remains open. That is intended,
not a malfunction.

Geometry (mirrors the realistic limit model: fill at the limit, no free alpha):

* A SHORT stop-limit closing a LONG (sell): triggers when ``bar.low <= stop``;
  fills at ``limit`` when ``bar.high >= limit`` (``limit <= stop``).
* A LONG stop-limit closing a SHORT (buy): triggers when ``bar.high >= stop``;
  fills at ``limit`` when ``bar.low <= limit`` (``limit >= stop``).
"""

from __future__ import annotations

import pytest

from investment_team.execution.bar_safety import BarSafetyAssertion
from investment_team.execution.risk_filter import RiskFilter, RiskLimits
from investment_team.trading_service.engine.execution_model import (
    OptimisticExecutionModel,
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
    OrderRequest,
    OrderSide,
    OrderType,
    TimeInForce,
)


def _bar(
    ts: str,
    *,
    open_price: float,
    high: float,
    low: float,
    close: float | None = None,
    volume: float = 1_000_000.0,
) -> Bar:
    return Bar(
        symbol="AAA",
        timestamp=ts,
        timeframe="1d",
        open=open_price,
        high=high,
        low=low,
        close=close if close is not None else open_price,
        volume=volume,
    )


def _make_simulator(model) -> tuple[FillSimulator, OrderBook, Portfolio]:
    portfolio = Portfolio(initial_capital=10_000_000.0)
    order_book = OrderBook()
    sim = FillSimulator(
        portfolio=portfolio,
        order_book=order_book,
        risk_filter=RiskFilter(RiskLimits(max_position_pct=100, max_gross_leverage=10.0)),
        config=FillSimulatorConfig(slippage_bps=0.0, transaction_cost_bps=0.0),
        bar_safety=BarSafetyAssertion(),
        execution_model=model,
    )
    return sim, order_book, portfolio


def _models():
    return [
        RealisticExecutionModel(participation_cap=0.10),
        OptimisticExecutionModel(warn=False),
    ]


def _open_long(sim: FillSimulator, order_book: OrderBook, *, entry_open: float = 100.0) -> None:
    order_book.submit(
        OrderRequest(
            client_order_id="entry-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=10.0,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
        ),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )
    sim.process_bar(_bar("2024-01-02", open_price=entry_open, high=entry_open, low=entry_open))


def _open_short(sim: FillSimulator, order_book: OrderBook, *, entry_open: float = 100.0) -> None:
    order_book.submit(
        OrderRequest(
            client_order_id="entry-1",
            symbol="AAA",
            side=OrderSide.SHORT,
            qty=10.0,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
        ),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )
    sim.process_bar(_bar("2024-01-02", open_price=entry_open, high=entry_open, low=entry_open))


def _submit_stop_limit(
    order_book: OrderBook,
    *,
    side: OrderSide,
    stop_price: float,
    limit_price: float,
) -> None:
    order_book.submit(
        OrderRequest(
            client_order_id="sl-1",
            symbol="AAA",
            side=side,
            qty=10.0,
            order_type=OrderType.STOP_LIMIT,
            stop_price=stop_price,
            limit_price=limit_price,
            tif=TimeInForce.GTC,
        ),
        submitted_at="2024-01-02",
        submitted_equity=10_000_000.0,
    )


def _unfilled_events(outcome) -> list:
    return [e for e in outcome.diagnostic_events if e.kind == "stop_limit_unfilled"]


# ---------------------------------------------------------------------------
# validate_prices
# ---------------------------------------------------------------------------


def test_validate_requires_both_prices() -> None:
    with pytest.raises(ValueError, match="both stop_price and limit_price"):
        OrderRequest(
            client_order_id="x",
            symbol="AAA",
            side=OrderSide.SHORT,
            qty=1.0,
            order_type=OrderType.STOP_LIMIT,
            stop_price=95.0,
        ).validate_prices()
    with pytest.raises(ValueError, match="both stop_price and limit_price"):
        OrderRequest(
            client_order_id="x",
            symbol="AAA",
            side=OrderSide.SHORT,
            qty=1.0,
            order_type=OrderType.STOP_LIMIT,
            limit_price=94.0,
        ).validate_prices()


def test_validate_rejects_wrong_side_limit() -> None:
    # SHORT (sell) stop-limit closing a long requires limit <= stop.
    with pytest.raises(ValueError, match="short stop_limit"):
        OrderRequest(
            client_order_id="x",
            symbol="AAA",
            side=OrderSide.SHORT,
            qty=1.0,
            order_type=OrderType.STOP_LIMIT,
            stop_price=95.0,
            limit_price=96.0,
        ).validate_prices()
    # LONG (buy) stop-limit closing a short requires limit >= stop.
    with pytest.raises(ValueError, match="long stop_limit"):
        OrderRequest(
            client_order_id="x",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=1.0,
            order_type=OrderType.STOP_LIMIT,
            stop_price=105.0,
            limit_price=104.0,
        ).validate_prices()


def test_validate_rejects_ioc_fok() -> None:
    for tif in (TimeInForce.IOC, TimeInForce.FOK):
        with pytest.raises(ValueError, match="market or limit"):
            OrderRequest(
                client_order_id="x",
                symbol="AAA",
                side=OrderSide.SHORT,
                qty=1.0,
                order_type=OrderType.STOP_LIMIT,
                stop_price=95.0,
                limit_price=94.0,
                tif=tif,
            ).validate_prices()


def test_validate_accepts_wellformed_both_sides() -> None:
    OrderRequest(
        client_order_id="x",
        symbol="AAA",
        side=OrderSide.SHORT,
        qty=1.0,
        order_type=OrderType.STOP_LIMIT,
        stop_price=95.0,
        limit_price=94.0,
    ).validate_prices()
    OrderRequest(
        client_order_id="x",
        symbol="AAA",
        side=OrderSide.LONG,
        qty=1.0,
        order_type=OrderType.STOP_LIMIT,
        stop_price=105.0,
        limit_price=106.0,
    ).validate_prices()


# ---------------------------------------------------------------------------
# Fills
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model", _models())
def test_short_stop_limit_closes_long_fills_at_limit(model) -> None:
    sim, order_book, portfolio = _make_simulator(model)
    _open_long(sim, order_book, entry_open=100.0)
    _submit_stop_limit(order_book, side=OrderSide.SHORT, stop_price=95.0, limit_price=94.0)

    # bar low=93 (<= stop 95, triggers) and high=96 (>= limit 94, fillable) → fill @ 94.
    outcome = sim.process_bar(_bar("2024-01-03", open_price=95.0, high=96.0, low=93.0, close=94.0))
    assert len(outcome.exit_fills) == 1
    assert outcome.exit_fills[0].price == pytest.approx(94.0, rel=1e-9)
    assert "AAA" not in portfolio.positions
    assert _unfilled_events(outcome) == []


@pytest.mark.parametrize("model", _models())
def test_long_stop_limit_closes_short_fills_at_limit(model) -> None:
    sim, order_book, portfolio = _make_simulator(model)
    _open_short(sim, order_book, entry_open=100.0)
    _submit_stop_limit(order_book, side=OrderSide.LONG, stop_price=105.0, limit_price=106.0)

    # bar high=107 (>= stop 105, triggers) and low=104 (<= limit 106, fillable) → fill @ 106.
    outcome = sim.process_bar(
        _bar("2024-01-03", open_price=105.0, high=107.0, low=104.0, close=106.0)
    )
    assert len(outcome.exit_fills) == 1
    assert outcome.exit_fills[0].price == pytest.approx(106.0, rel=1e-9)
    assert "AAA" not in portfolio.positions


# ---------------------------------------------------------------------------
# Gap-through non-fill (the headline case)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model", _models())
def test_short_stop_limit_gap_through_does_not_fill(model) -> None:
    """SHORT stop-limit: bar gaps entirely below the limit. Triggered (stop
    crossed) but unfillable — no fill, order stays resting, position open."""
    sim, order_book, portfolio = _make_simulator(model)
    _open_long(sim, order_book, entry_open=100.0)
    _submit_stop_limit(order_book, side=OrderSide.SHORT, stop_price=95.0, limit_price=94.0)

    # Gap down: open=90, high=90 (< limit 94), low=85 (<= stop 95, triggers).
    outcome = sim.process_bar(_bar("2024-01-03", open_price=90.0, high=90.0, low=85.0, close=88.0))
    assert outcome.exit_fills == []
    assert "AAA" in portfolio.positions  # still open
    events = _unfilled_events(outcome)
    assert len(events) == 1
    assert events[0].order_type == OrderType.STOP_LIMIT.value
    # The order is still resting on the book for a later bar.
    assert any(po.request.client_order_id == "sl-1" for po in order_book.all_pending())


@pytest.mark.parametrize("model", _models())
def test_long_stop_limit_gap_through_does_not_fill(model) -> None:
    sim, order_book, portfolio = _make_simulator(model)
    _open_short(sim, order_book, entry_open=100.0)
    _submit_stop_limit(order_book, side=OrderSide.LONG, stop_price=105.0, limit_price=106.0)

    # Gap up: open=110, low=108 (> limit 106), high=112 (>= stop 105, triggers).
    outcome = sim.process_bar(
        _bar("2024-01-03", open_price=110.0, high=112.0, low=108.0, close=111.0)
    )
    assert outcome.exit_fills == []
    assert "AAA" in portfolio.positions
    assert len(_unfilled_events(outcome)) == 1


# ---------------------------------------------------------------------------
# No trigger
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model", _models())
def test_stop_limit_no_trigger_rests_quietly(model) -> None:
    """Bar never crosses the stop → no fill and NO unfilled-trigger telemetry
    (the counter only counts triggered-but-unfilled, not untriggered)."""
    sim, order_book, portfolio = _make_simulator(model)
    _open_long(sim, order_book, entry_open=100.0)
    _submit_stop_limit(order_book, side=OrderSide.SHORT, stop_price=95.0, limit_price=94.0)

    # low=96 > stop 95 → never triggers.
    outcome = sim.process_bar(_bar("2024-01-03", open_price=99.0, high=100.0, low=96.0, close=98.0))
    assert outcome.exit_fills == []
    assert "AAA" in portfolio.positions
    assert _unfilled_events(outcome) == []
