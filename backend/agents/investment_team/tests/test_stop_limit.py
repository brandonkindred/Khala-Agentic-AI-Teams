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
def test_short_stop_limit_fills_on_recovery_after_gap_through(model) -> None:
    """Once triggered, a stop-limit latches into a resting limit. A SHORT
    stop-limit that gaps through its limit (no fill) must still fill on a later
    recovery bar where the limit is marketable, even though the stop level is
    NOT re-crossed on that bar — otherwise the protective exit stays stuck open."""
    sim, order_book, portfolio = _make_simulator(model)
    _open_long(sim, order_book, entry_open=100.0)
    _submit_stop_limit(order_book, side=OrderSide.SHORT, stop_price=95.0, limit_price=94.0)

    # Bar 1 — gap through: triggers (low 85 <= stop 95) but high 90 < limit 94.
    outcome1 = sim.process_bar(_bar("2024-01-03", open_price=90.0, high=90.0, low=85.0, close=88.0))
    assert outcome1.exit_fills == []
    assert "AAA" in portfolio.positions
    assert len(_unfilled_events(outcome1)) == 1

    # Bar 2 — recovery: stop NOT re-crossed (low 96 > 95), but the sell limit at
    # 94 is marketable (high 97 >= 94). Latched order fills at the limit.
    outcome2 = sim.process_bar(_bar("2024-01-04", open_price=96.0, high=97.0, low=96.0, close=96.5))
    assert len(outcome2.exit_fills) == 1
    assert outcome2.exit_fills[0].price == pytest.approx(94.0, rel=1e-9)
    assert "AAA" not in portfolio.positions
    # Recovery fill is not a re-trigger, so no new unfilled telemetry.
    assert _unfilled_events(outcome2) == []


@pytest.mark.parametrize("model", _models())
def test_long_stop_limit_fills_on_recovery_after_gap_through(model) -> None:
    """Buy-side symmetry: a LONG stop-limit that gaps up through its limit
    latches and fills on a later bar where the buy limit is marketable, without
    the stop being re-crossed."""
    sim, order_book, portfolio = _make_simulator(model)
    _open_short(sim, order_book, entry_open=100.0)
    _submit_stop_limit(order_book, side=OrderSide.LONG, stop_price=105.0, limit_price=106.0)

    # Bar 1 — gap through up: triggers (high 112 >= stop 105) but low 108 > limit 106.
    outcome1 = sim.process_bar(
        _bar("2024-01-03", open_price=110.0, high=112.0, low=108.0, close=111.0)
    )
    assert outcome1.exit_fills == []
    assert "AAA" in portfolio.positions
    assert len(_unfilled_events(outcome1)) == 1

    # Bar 2 — recovery down: stop NOT re-crossed (high 104 < 105), but the buy
    # limit at 106 is marketable (low 103 <= 106). Latched order fills at 106.
    outcome2 = sim.process_bar(
        _bar("2024-01-04", open_price=104.0, high=104.0, low=103.0, close=103.5)
    )
    assert len(outcome2.exit_fills) == 1
    assert outcome2.exit_fills[0].price == pytest.approx(106.0, rel=1e-9)
    assert "AAA" not in portfolio.positions


@pytest.mark.parametrize("model", _models())
def test_same_bar_submission_does_not_arm_on_lookahead(model) -> None:
    """Look-ahead safety: a strategy-side standalone STOP_LIMIT submitted on the
    SAME bar it is processed must NOT arm off that bar's data via the gap-through
    path (which never reaches ``bar_safety.check_fill``). Otherwise it would
    carry same-bar trigger info forward and fill on a later bar. Contrast with
    ``test_short_stop_limit_fills_on_recovery_after_gap_through`` (submitted on an
    earlier bar → arms → fills)."""
    sim, order_book, portfolio = _make_simulator(model)
    _open_long(sim, order_book, entry_open=100.0)
    # Submit with ``submitted_at`` equal to the bar we then process (look-ahead).
    order_book.submit(
        OrderRequest(
            client_order_id="sl-1",
            symbol="AAA",
            side=OrderSide.SHORT,
            qty=10.0,
            order_type=OrderType.STOP_LIMIT,
            stop_price=95.0,
            limit_price=94.0,
            tif=TimeInForce.GTC,
        ),
        submitted_at="2024-01-03",
        submitted_equity=10_000_000.0,
    )

    # Same-bar gap-through: triggers (low 85 <= 95) but high 90 < limit 94. The
    # order must NOT arm and must NOT emit telemetry — the bar is not strictly
    # after submission.
    outcome1 = sim.process_bar(_bar("2024-01-03", open_price=90.0, high=90.0, low=85.0, close=88.0))
    assert outcome1.exit_fills == []
    assert _unfilled_events(outcome1) == []
    po = next(p for p in order_book.all_pending() if p.request.client_order_id == "sl-1")
    assert po.stop_limit_armed is False

    # Later bar where the stop is NOT re-crossed but the limit would be
    # marketable if it had armed. Because the look-ahead arming was prevented,
    # it stays un-triggered and does NOT fill.
    outcome2 = sim.process_bar(_bar("2024-01-04", open_price=96.0, high=97.0, low=96.0, close=96.5))
    assert outcome2.exit_fills == []
    assert "AAA" in portfolio.positions


@pytest.mark.parametrize("model", _models())
def test_armed_exit_binds_to_its_position(model) -> None:
    """When a strategy-side stop-limit exit arms, it is bound to the position it
    protects (``working_against_entry_order_id``) so the stale-continuation guard
    can later discard it if that position is closed by another exit."""
    sim, order_book, portfolio = _make_simulator(model)
    _open_long(sim, order_book, entry_open=100.0)
    _submit_stop_limit(order_book, side=OrderSide.SHORT, stop_price=95.0, limit_price=94.0)

    # Gap-through: arms but does not fill; the long is still open.
    sim.process_bar(_bar("2024-01-03", open_price=90.0, high=90.0, low=85.0, close=88.0))
    entry_order_id = portfolio.positions["AAA"].entry_order_id
    po = next(p for p in order_book.all_pending() if p.request.client_order_id == "sl-1")
    assert po.stop_limit_armed is True
    assert po.working_against_entry_order_id == entry_order_id


@pytest.mark.parametrize("model", _models())
def test_armed_exit_discarded_when_position_closed_by_another_exit(model) -> None:
    """An armed stop-limit exit whose position is closed by a separate exit must
    be discarded on a later recovery bar — NOT routed through ``_fill_entry`` to
    open a reverse position. Without the position binding it would open a short."""
    sim, order_book, portfolio = _make_simulator(model)
    _open_long(sim, order_book, entry_open=100.0)
    _submit_stop_limit(order_book, side=OrderSide.SHORT, stop_price=95.0, limit_price=94.0)

    # Arm via gap-through (no fill).
    sim.process_bar(_bar("2024-01-03", open_price=90.0, high=90.0, low=85.0, close=88.0))
    assert "AAA" in portfolio.positions

    # Close the long with a separate SHORT MARKET exit on a bar where the 94
    # limit is NOT marketable (high 93 < 94), so the stop-limit cannot fill.
    order_book.submit(
        OrderRequest(
            client_order_id="close-1",
            symbol="AAA",
            side=OrderSide.SHORT,
            qty=10.0,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
        ),
        submitted_at="2024-01-03",
        submitted_equity=10_000_000.0,
    )
    sim.process_bar(_bar("2024-01-04", open_price=93.0, high=93.0, low=92.0, close=92.5))
    assert "AAA" not in portfolio.positions  # long closed by the market exit

    # Recovery bar where the 94 limit IS marketable. The stale armed stop-limit
    # must be discarded (stale-continuation guard), not opened as a reverse short.
    outcome = sim.process_bar(_bar("2024-01-05", open_price=96.0, high=97.0, low=96.0, close=96.5))
    assert "AAA" not in portfolio.positions
    assert outcome.entry_fills == []
    assert not any(p.request.client_order_id == "sl-1" for p in order_book.all_pending())


def test_unfilled_trigger_aggregates_into_diagnostics_counter() -> None:
    """End-to-end through the diagnostics aggregation: a gap-through outcome
    fed to ``_apply_fill_outcome_events`` bumps
    ``BacktestExecutionDiagnostics.stop_limit_unfilled_triggers`` and records a
    ``stop_limit_unfilled`` lifecycle event. Covers the events→counter path in
    ``service.py`` that the per-bar simulator test does not exercise."""
    from investment_team.models import BacktestExecutionDiagnostics
    from investment_team.trading_service.service import _apply_fill_outcome_events

    sim, order_book, portfolio = _make_simulator(RealisticExecutionModel(participation_cap=0.10))
    _open_long(sim, order_book, entry_open=100.0)
    _submit_stop_limit(order_book, side=OrderSide.SHORT, stop_price=95.0, limit_price=94.0)

    outcome = sim.process_bar(_bar("2024-01-03", open_price=90.0, high=90.0, low=85.0, close=88.0))
    assert _unfilled_events(outcome)  # simulator emitted the event

    diagnostics = BacktestExecutionDiagnostics()
    _apply_fill_outcome_events(diagnostics, outcome)
    assert diagnostics.stop_limit_unfilled_triggers == 1
    assert any(e.event_type == "stop_limit_unfilled" for e in diagnostics.last_order_events)


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
