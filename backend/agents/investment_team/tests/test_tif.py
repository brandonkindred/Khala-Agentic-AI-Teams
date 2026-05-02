"""IOC / FOK time-in-force runtime support in the fill simulator (#388).

The ``TimeInForce`` enum (DAY/GTC/IOC/FOK) was added to the contract in
Step 1 of #379 but the engine raised ``UnsupportedOrderFeatureError``
for ``IOC`` and ``FOK`` until this step. With #388 those gates are
lifted and the simulator honours the two non-default TIFs:

- **FOK** rejects on this bar if the bar can't fully absorb the order
  (participation-cap clip OR exit-side ``pos.qty`` shortfall).
- **IOC** lets whatever this bar can absorb through, emits a PARTIAL
  fill, then cancels the remainder regardless of ``unfilled_policy``.

DAY/GTC behaviour and golden parity must remain unchanged.
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
    FillKind,
    OrderRequest,
    OrderSide,
    OrderType,
    TimeInForce,
    UnfilledPolicy,
)


def _bar(ts: str, *, price: float = 100.0, volume: float = 1_000_000.0) -> Bar:
    return Bar(
        symbol="AAA",
        timestamp=ts,
        timeframe="1d",
        open=price,
        high=price + 1,
        low=price - 1,
        close=price,
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
        # Pin to the production cap so fixed bar/qty math below stays
        # stable if the default ever changes.
        execution_model=RealisticExecutionModel(participation_cap=0.10),
    )
    return sim, order_book, portfolio


def _entry_order(
    qty: float,
    *,
    tif: TimeInForce = TimeInForce.DAY,
    policy: UnfilledPolicy | None = None,
) -> OrderRequest:
    return OrderRequest(
        client_order_id="entry-1",
        symbol="AAA",
        side=OrderSide.LONG,
        qty=qty,
        order_type=OrderType.MARKET,
        tif=tif,
        unfilled_policy=policy,
    )


# Participation math reminder (default cap = 0.10):
#   raw_participation = req.qty * ref_price / (bar.volume * bar.close)
#   if raw_participation <= cap: qty_fraction = 1.0
#   else:                        qty_fraction = cap / raw_participation
#
# At price=100, volume=10_000  → bar_dollar_volume=1_000_000, capacity=100_000
#                                notional → 1_000 shares ⇒ qty=2_000 → 50% partial.
# At price=100, volume=10_000_000 → bar_dollar_volume=1e9, capacity=1e8 notional
#                                   → fully absorbs qty=2_000 ⇒ qty_fraction=1.0.


def test_fok_low_adv_emits_rejected_fill_and_no_position() -> None:
    """FOK + low-ADV bar → single REJECTED Fill, no Position created, no requeue.

    Acceptance bullet 1 of #388.
    """
    sim, order_book, portfolio = _make_simulator()
    order_book.submit(
        _entry_order(2_000, tif=TimeInForce.FOK),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )

    outcome = sim.process_bar(_bar("2024-01-02", price=100.0, volume=10_000))

    assert len(outcome.entry_fills) == 1
    fill = outcome.entry_fills[0]
    assert fill.fill_kind == FillKind.REJECTED
    assert fill.qty == 0.0
    assert fill.unfilled_qty == pytest.approx(2_000.0, rel=1e-9)
    assert fill.cumulative_filled_qty == 0.0
    # No money math: no position, no risk-gate side effect, no exit fill.
    assert "AAA" not in portfolio.positions
    assert outcome.exit_fills == []
    # Order removed from the book — no requeue.
    assert order_book.all_pending() == []

    # Belt-and-braces: a follow-up bar (even one that could fully absorb)
    # produces nothing because the FOK was already cancelled.
    follow_up = sim.process_bar(_bar("2024-01-03", price=100.0, volume=10_000_000))
    assert follow_up.entry_fills == []
    assert follow_up.exit_fills == []


def test_fok_full_absorption_emits_full_fill() -> None:
    """FOK + bar that can fully absorb → ``fill_kind=FULL``.

    Acceptance bullet 2 of #388.
    """
    sim, order_book, portfolio = _make_simulator()
    order_book.submit(
        _entry_order(2_000, tif=TimeInForce.FOK),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )

    outcome = sim.process_bar(_bar("2024-01-02", price=100.0, volume=10_000_000))

    assert len(outcome.entry_fills) == 1
    fill = outcome.entry_fills[0]
    assert fill.fill_kind == FillKind.FULL
    assert fill.qty == pytest.approx(2_000.0, rel=1e-9)
    assert fill.unfilled_qty == pytest.approx(0.0, abs=1e-9)
    assert "AAA" in portfolio.positions
    assert portfolio.positions["AAA"].qty == pytest.approx(2_000.0, rel=1e-9)
    # FOK fully filled → order removed (the standard full-fill path).
    assert order_book.all_pending() == []


def test_ioc_low_adv_emits_partial_then_removes_order() -> None:
    """IOC + low-ADV bar → PARTIAL fill with ``unfilled_qty>0``; the order
    is then removed and the next bar sees no pending order.

    Acceptance bullet 3 of #388.
    """
    sim, order_book, portfolio = _make_simulator()
    order_book.submit(
        _entry_order(2_000, tif=TimeInForce.IOC),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )

    outcome = sim.process_bar(_bar("2024-01-02", price=100.0, volume=10_000))

    # PARTIAL fill emitted exactly as it would be for DAY/DROP — IOC only
    # changes the *remainder* handling, not the Fill payload.
    assert len(outcome.entry_fills) == 1
    fill = outcome.entry_fills[0]
    assert fill.fill_kind == FillKind.PARTIAL
    assert fill.qty == pytest.approx(1_000.0, rel=1e-9)
    assert fill.unfilled_qty == pytest.approx(1_000.0, rel=1e-9)
    assert fill.cumulative_filled_qty == pytest.approx(1_000.0, rel=1e-9)

    # Position opened with the partial qty.
    assert "AAA" in portfolio.positions
    assert portfolio.positions["AAA"].qty == pytest.approx(1_000.0, rel=1e-9)

    # IOC: remainder cancelled — no continuation across bars.
    assert order_book.all_pending() == []

    follow_up = sim.process_bar(_bar("2024-01-03", price=100.0, volume=10_000_000))
    assert follow_up.entry_fills == []


def test_ioc_overrides_requeue_next_bar_policy() -> None:
    """IOC + ``REQUEUE_NEXT_BAR`` → IOC overrides; no requeue.

    Acceptance bullet 4 of #388. The strategy's ``unfilled_policy`` is
    ignored for IOC; the engine forces a same-bar cancel of the
    remainder.
    """
    sim, order_book, _portfolio = _make_simulator()
    order_book.submit(
        _entry_order(2_000, tif=TimeInForce.IOC, policy=UnfilledPolicy.REQUEUE_NEXT_BAR),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )

    outcome = sim.process_bar(_bar("2024-01-02", price=100.0, volume=10_000))

    assert outcome.entry_fills[0].fill_kind == FillKind.PARTIAL
    # No requeue despite REQUEUE_NEXT_BAR — IOC dominates.
    assert order_book.all_pending() == []
