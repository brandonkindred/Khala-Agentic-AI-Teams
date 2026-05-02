"""Partial-fill emission tests for the streaming fill simulator (#386).

Covers the five acceptance bullets from the issue:

1. ``RealisticExecutionModel`` + low ADV → entry returns
   ``Fill(fill_kind=PARTIAL, unfilled_qty>0)``.
2. ``REQUEUE_NEXT_BAR`` → two consecutive entry fills sum to the original
   qty; the resulting ``TradeRecord`` carries
   ``partial_fill_count=2`` and ``participation_clipped=True``.
3. ``DROP`` policy preserves the silent-remainder behavior end-to-end
   (the partial Fill is still emitted, but the remainder is not requeued).
4. Bar-safety guard does not fire on a requeued order's next-bar fill —
   ``OrderBook.requeue`` advances ``submitted_at`` to the fill bar so the
   next bar is strictly later.
5. Partial exit: position qty is decremented across multiple bars,
   ``TradeRecord`` is emitted only on the closing bar, and
   ``TradeRecord.exit_price`` equals the qty-weighted average exit price.
"""

from __future__ import annotations

import pytest

from investment_team.execution.bar_safety import BarSafetyAssertion
from investment_team.execution.risk_filter import RiskFilter, RiskLimits
from investment_team.trading_service.engine.execution_model import RealisticExecutionModel
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
        # Default 0.10 cap is what production uses — pin explicitly here so
        # the math in this test file doesn't drift if the default changes.
        execution_model=RealisticExecutionModel(participation_cap=0.10),
    )
    return sim, order_book, portfolio


def _entry_order(qty: float, *, policy: UnfilledPolicy | None = None) -> OrderRequest:
    return OrderRequest(
        client_order_id="entry-1",
        symbol="AAA",
        side=OrderSide.LONG,
        qty=qty,
        order_type=OrderType.MARKET,
        tif=TimeInForce.DAY,
        unfilled_policy=policy,
    )


def _exit_order(qty: float, *, policy: UnfilledPolicy | None = None) -> OrderRequest:
    return OrderRequest(
        client_order_id="exit-1",
        symbol="AAA",
        side=OrderSide.SHORT,
        qty=qty,
        order_type=OrderType.MARKET,
        tif=TimeInForce.DAY,
        unfilled_policy=policy,
    )


# Participation math reminder (default cap = 0.10):
#   raw_participation = req.qty * ref_price / (bar.volume * bar.close)
#   if raw_participation <= cap: qty_fraction = 1.0
#   else:                        qty_fraction = cap / raw_participation
#
# To force a 50% partial: raw_participation = 0.20.
# At price=100, volume=10_000, bar_dollar_volume = 1_000_000.
# An order of qty=2_000 has notional = 200_000 → raw_participation = 0.20
# → qty_fraction = 0.5 → fills 1_000, leaves 1_000 unfilled.


def test_realistic_low_adv_emits_partial_entry_fill() -> None:
    """Single oversized order against a low-ADV bar emits a PARTIAL fill.

    No silent-drop: the engine must annotate the Fill with ``fill_kind``,
    ``unfilled_qty``, and ``cumulative_filled_qty`` so the strategy sees
    what actually filled.
    """
    sim, order_book, portfolio = _make_simulator()
    order_book.submit(
        _entry_order(2_000),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )

    outcome = sim.process_bar(_bar("2024-01-02", price=100.0, volume=10_000))

    assert len(outcome.entry_fills) == 1
    fill = outcome.entry_fills[0]
    assert fill.fill_kind == FillKind.PARTIAL
    assert fill.qty == pytest.approx(1_000.0, rel=1e-9)
    assert fill.unfilled_qty == pytest.approx(1_000.0, rel=1e-9)
    assert fill.cumulative_filled_qty == pytest.approx(1_000.0, rel=1e-9)

    pos = portfolio.positions["AAA"]
    assert pos.original_qty == 2_000
    assert pos.qty == pytest.approx(1_000.0, rel=1e-9)
    assert pos.participation_clipped is True
    assert pos.total_unfilled_qty == pytest.approx(1_000.0, rel=1e-9)
    assert pos.partial_fill_count == 1


def test_requeue_next_bar_two_partials_sum_to_original() -> None:
    """Two-bar entry fill + clean exit produces a TradeRecord with
    ``partial_fill_count=2`` and ``participation_clipped=True``.
    """
    sim, order_book, portfolio = _make_simulator()
    order_book.submit(
        _entry_order(2_000, policy=UnfilledPolicy.REQUEUE_NEXT_BAR),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )

    # Bar 1: low volume → 50% partial fill, remainder requeued.
    bar1 = sim.process_bar(_bar("2024-01-02", price=100.0, volume=10_000))
    assert bar1.entry_fills[0].fill_kind == FillKind.PARTIAL
    assert "entry-1" in {po.request.client_order_id for po in order_book.all_pending()}, (
        "remainder should remain pending after a REQUEUE_NEXT_BAR partial"
    )

    # Bar 2: high volume → remainder fills cleanly.
    bar2 = sim.process_bar(_bar("2024-01-03", price=100.0, volume=10_000_000))
    assert len(bar2.entry_fills) == 1
    final_entry = bar2.entry_fills[0]
    assert final_entry.fill_kind == FillKind.FULL
    assert final_entry.cumulative_filled_qty == pytest.approx(2_000.0, rel=1e-9)
    pos = portfolio.positions["AAA"]
    assert pos.qty == pytest.approx(2_000.0, rel=1e-9)
    assert pos.partial_fill_count == 2

    # Bar 3: clean full exit on a high-volume bar.
    order_book.submit(
        _exit_order(2_000),
        submitted_at="2024-01-03",
        submitted_equity=10_000_000.0,
    )
    bar3 = sim.process_bar(_bar("2024-01-04", price=105.0, volume=10_000_000))

    assert len(bar3.closed_trades) == 1
    record = bar3.closed_trades[0]
    assert record.partial_fill_count == 2
    assert record.participation_clipped is True
    assert record.total_unfilled_qty == pytest.approx(1_000.0, rel=1e-9)
    assert record.shares == pytest.approx(2_000.0, rel=1e-9)


def test_drop_policy_emits_partial_fill_but_does_not_requeue() -> None:
    """Under ``DROP`` the partial Fill is still emitted (no silent-drop) but
    the remainder is removed from the book — no next-bar continuation."""
    sim, order_book, _ = _make_simulator()
    order_book.submit(
        _entry_order(2_000, policy=UnfilledPolicy.DROP),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )

    outcome = sim.process_bar(_bar("2024-01-02", price=100.0, volume=10_000))
    assert outcome.entry_fills[0].fill_kind == FillKind.PARTIAL
    assert outcome.entry_fills[0].unfilled_qty == pytest.approx(1_000.0, rel=1e-9)
    # Remainder dropped, not requeued.
    assert order_book.all_pending() == []


def test_requeued_order_passes_bar_safety() -> None:
    """The requeue path must advance ``submitted_at`` to the fill bar so the
    bar-safety guard (which compares ``submitted_at < fill_bar.timestamp``)
    keeps holding on the next-bar continuation."""
    sim, order_book, _ = _make_simulator()
    order_book.submit(
        _entry_order(2_000, policy=UnfilledPolicy.REQUEUE_NEXT_BAR),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )

    sim.process_bar(_bar("2024-01-02", price=100.0, volume=10_000))

    pending = order_book.all_pending()
    assert len(pending) == 1
    # ``OrderBook.requeue`` rewrites ``submitted_at`` to the fill bar.
    assert pending[0].submitted_at == "2024-01-02"
    assert pending[0].cumulative_filled_qty == pytest.approx(1_000.0, rel=1e-9)

    # The next-bar continuation must not raise LookAheadError despite the
    # original ``submitted_at`` being two bars in the past.
    outcome = sim.process_bar(_bar("2024-01-03", price=100.0, volume=10_000_000))
    assert outcome.entry_fills[0].fill_kind == FillKind.FULL


def test_partial_exit_records_weighted_avg_price() -> None:
    """Exit clipped across two bars → TradeRecord only on the closing bar
    and ``exit_price`` is the qty-weighted average of the two fills."""
    sim, order_book, portfolio = _make_simulator()

    # Open a clean position first via a high-volume entry bar.
    order_book.submit(
        _entry_order(2_000),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )
    sim.process_bar(_bar("2024-01-02", price=100.0, volume=10_000_000))
    pos = portfolio.positions["AAA"]
    assert pos.qty == pytest.approx(2_000.0, rel=1e-9)
    assert pos.participation_clipped is False

    # Submit the closing order. The next bar's volume forces a 50% partial
    # exit; the bar after fills the rest.
    order_book.submit(
        _exit_order(2_000, policy=UnfilledPolicy.REQUEUE_NEXT_BAR),
        submitted_at="2024-01-02",
        submitted_equity=10_000_000.0,
    )

    bar1 = sim.process_bar(_bar("2024-01-03", price=110.0, volume=10_000))
    assert len(bar1.closed_trades) == 0, "no TradeRecord until the position is fully closed"
    assert len(bar1.exit_fills) == 1
    assert bar1.exit_fills[0].fill_kind == FillKind.PARTIAL
    assert bar1.exit_fills[0].qty == pytest.approx(1_000.0, rel=1e-9)

    # Position partially closed but still open.
    assert "AAA" in portfolio.positions
    assert portfolio.positions["AAA"].qty == pytest.approx(1_000.0, rel=1e-9)

    bar2 = sim.process_bar(_bar("2024-01-04", price=120.0, volume=10_000_000))
    assert len(bar2.closed_trades) == 1
    record = bar2.closed_trades[0]
    # Weighted-avg exit: (1000 * 110 + 1000 * 120) / 2000 = 115
    assert record.exit_price == pytest.approx(115.0, rel=1e-9)
    assert record.shares == pytest.approx(2_000.0, rel=1e-9)
    assert record.participation_clipped is True
    # ``partial_fill_count`` only tracks entry-side pieces; this entry was a
    # single full fill.
    assert record.partial_fill_count == 1
    # Position fully closed.
    assert "AAA" not in portfolio.positions


def test_exit_does_not_overfill_when_entry_continuation_grows_position() -> None:
    """Same-bar entry continuation + exit must not over-close.

    Regression for a Codex review note on PR #417: when a partial entry
    is requeued and its remainder fills on the same bar an exit fires
    against, ``_fill_exit`` must size the fill from
    ``min(po.remaining_qty, pos.qty)`` — not from ``pos.qty`` alone —
    or the exit will close newly-added shares the strategy never asked
    to unwind.
    """
    sim, order_book, portfolio = _make_simulator()

    # Bar 1 (low volume): partial entry → pos.qty=1000, remainder=1000 requeued.
    order_book.submit(
        _entry_order(2_000, policy=UnfilledPolicy.REQUEUE_NEXT_BAR),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )
    sim.process_bar(_bar("2024-01-02", price=100.0, volume=10_000))
    assert portfolio.positions["AAA"].qty == pytest.approx(1_000.0, rel=1e-9)

    # The strategy sees pos.qty=1000 and submits an exit for that qty —
    # *before* the entry remainder fills next bar.
    order_book.submit(
        _exit_order(1_000),
        submitted_at="2024-01-02",
        submitted_equity=10_000_000.0,
    )

    # Bar 2 (high volume): entry continuation grows the position to 2000,
    # then the exit fires. The exit must respect the strategy's req.qty
    # (1000), not balloon to the new pos.qty (2000).
    bar2 = sim.process_bar(_bar("2024-01-03", price=100.0, volume=10_000_000))

    # Exit fill should be exactly the strategy's requested 1000.
    assert len(bar2.exit_fills) == 1
    assert bar2.exit_fills[0].qty == pytest.approx(1_000.0, rel=1e-9)
    # Position remains open with the entry continuation's added shares —
    # not silently emptied to zero.
    assert "AAA" in portfolio.positions
    assert portfolio.positions["AAA"].qty == pytest.approx(1_000.0, rel=1e-9)
    # No TradeRecord yet — only original_qty exits trigger one.
    assert len(bar2.closed_trades) == 0
