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
    # ``original_qty`` tracks cumulative *entry-filled* qty, not the
    # strategy's request — so a DROP / REJECT path leaves it at what's
    # actually held, which is what subsequent exits will close against.
    assert pos.original_qty == pytest.approx(1_000.0, rel=1e-9)
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


def test_dropped_entry_remainder_does_not_strand_position() -> None:
    """A partial entry with ``DROP`` followed by an exit for the held qty
    must close the position cleanly.

    Regression for a Codex review note on PR #417: ``Position.is_closed``
    compares cumulative exits against ``original_qty``. If ``original_qty``
    were pinned to the strategy's *request* (2000) instead of the
    actually-filled qty (1000), exiting the held shares would leave the
    position stranded with ``qty=0`` and no ``TradeRecord``.
    """
    sim, order_book, portfolio = _make_simulator()

    # Bar 1: partial entry under DROP → pos.qty=1000, remainder=1000 dropped.
    order_book.submit(
        _entry_order(2_000, policy=UnfilledPolicy.DROP),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )
    sim.process_bar(_bar("2024-01-02", price=100.0, volume=10_000))
    assert order_book.all_pending() == [], "DROP must remove the remainder from the book"
    pos = portfolio.positions["AAA"]
    assert pos.qty == pytest.approx(1_000.0, rel=1e-9)
    assert pos.original_qty == pytest.approx(1_000.0, rel=1e-9), (
        "original_qty must reflect actually-held qty, not the abandoned request"
    )

    # Bar 2: exit the held qty (1000). Position must close cleanly.
    order_book.submit(
        _exit_order(1_000),
        submitted_at="2024-01-02",
        submitted_equity=10_000_000.0,
    )
    bar2 = sim.process_bar(_bar("2024-01-03", price=110.0, volume=10_000_000))
    assert len(bar2.closed_trades) == 1
    assert bar2.closed_trades[0].shares == pytest.approx(1_000.0, rel=1e-9)
    assert "AAA" not in portfolio.positions, "position must be popped on full close"


def test_partial_unwind_exit_reports_full_fill_kind() -> None:
    """Strategy requests qty < pos.qty (partial unwind) and the exit fills
    fully against high volume — ``Fill.fill_kind`` must be ``FULL`` (the
    exit *order* completed) even though the position is still open.

    Regression for a Codex P2 review note: ``fill_kind`` describes the
    exit order's completeness, not the position's closure state.
    """
    sim, order_book, portfolio = _make_simulator()

    # Open a 2000-qty position cleanly.
    order_book.submit(
        _entry_order(2_000),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )
    sim.process_bar(_bar("2024-01-02", price=100.0, volume=10_000_000))
    assert portfolio.positions["AAA"].qty == pytest.approx(2_000.0, rel=1e-9)

    # Partial unwind: exit only 500 of the 2000.
    order_book.submit(
        _exit_order(500),
        submitted_at="2024-01-02",
        submitted_equity=10_000_000.0,
    )
    bar2 = sim.process_bar(_bar("2024-01-03", price=110.0, volume=10_000_000))

    assert len(bar2.exit_fills) == 1
    fill = bar2.exit_fills[0]
    # Order filled to its requested size — must be FULL, not PARTIAL.
    assert fill.fill_kind == FillKind.FULL
    assert fill.unfilled_qty == pytest.approx(0.0, abs=1e-9)
    assert fill.qty == pytest.approx(500.0, rel=1e-9)
    # Position still open with residual exposure — no TradeRecord yet.
    assert len(bar2.closed_trades) == 0
    assert portfolio.positions["AAA"].qty == pytest.approx(1_500.0, rel=1e-9)


def test_risk_gate_re_applied_on_entry_continuation() -> None:
    """A continuation slice must re-check the risk gate. If the limits no
    longer admit the post-extend exposure, the continuation is rejected
    even though the original entry's first slice fit at submit time.

    Regression for a Codex P1 review note: ``can_enter`` was being
    skipped on continuations, letting later slices breach
    leverage / concentration limits set after the initial fill.
    """
    portfolio = Portfolio(initial_capital=1_000_000.0)
    order_book = OrderBook()
    # Tight concentration cap: a fully-filled 2000-share order at ~$100
    # = $200k notional = 20% of equity. ``max_symbol_concentration_pct``
    # at 15% admits the first 1000-share slice (10%) but rejects the
    # post-extend total (20%).
    sim = FillSimulator(
        portfolio=portfolio,
        order_book=order_book,
        risk_filter=RiskFilter(
            RiskLimits(
                max_position_pct=100,
                max_symbol_concentration_pct=15,
                max_gross_leverage=10.0,
            )
        ),
        config=FillSimulatorConfig(slippage_bps=0.0, transaction_cost_bps=0.0),
        bar_safety=BarSafetyAssertion(),
        execution_model=RealisticExecutionModel(participation_cap=0.10),
    )

    # First slice (1000 shares × $100 ≈ 10% concentration) fits. Remainder
    # would push to 20% concentration, breaching max_position_pct=15.
    order_book.submit(
        _entry_order(2_000, policy=UnfilledPolicy.REQUEUE_NEXT_BAR),
        submitted_at="2024-01-01",
        submitted_equity=1_000_000.0,
    )
    bar1 = sim.process_bar(_bar("2024-01-02", price=100.0, volume=10_000))
    assert len(bar1.entry_fills) == 1
    assert bar1.entry_fills[0].fill_kind == FillKind.PARTIAL
    assert portfolio.positions["AAA"].qty == pytest.approx(1_000.0, rel=1e-9)

    # Bar 2: high volume would otherwise let the continuation fully fill,
    # but the risk gate rejects it.
    bar2 = sim.process_bar(_bar("2024-01-03", price=100.0, volume=10_000_000))
    assert bar2.entry_fills == [], "continuation must be rejected by risk gate"
    # Position keeps its first-slice qty; the rejected continuation is
    # removed from the book without growing exposure.
    assert portfolio.positions["AAA"].qty == pytest.approx(1_000.0, rel=1e-9)
    assert order_book.all_pending() == []


def test_over_ask_exit_reports_truncated_remainder_as_unfilled() -> None:
    """An exit order requesting more shares than the position holds must
    report the not-fillable portion as ``unfilled_qty`` — silently
    truncating to the live position size and reporting ``unfilled_qty=0``
    would mislabel the order as fully complete.

    Regression for a Codex P2 review note on PR #417.
    """
    sim, order_book, portfolio = _make_simulator()

    # Open a 500-qty position via a DROP-partial entry (req=2000, fills 500,
    # remainder dropped). Position now has qty=500, original_qty=500.
    order_book.submit(
        _entry_order(2_000, policy=UnfilledPolicy.DROP),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )
    # raw_part = (2000 * 100) / (5000 * 100) = 0.4 → qty_fraction = 0.25
    # → filled_qty = 2000 * 0.25 = 500.
    sim.process_bar(_bar("2024-01-02", price=100.0, volume=5_000))
    assert portfolio.positions["AAA"].qty == pytest.approx(500.0, rel=1e-9)
    assert portfolio.positions["AAA"].original_qty == pytest.approx(500.0, rel=1e-9)

    # Strategy mistakenly asks to exit 1000 (more than held).
    order_book.submit(
        _exit_order(1_000),
        submitted_at="2024-01-02",
        submitted_equity=10_000_000.0,
    )
    bar2 = sim.process_bar(_bar("2024-01-03", price=110.0, volume=10_000_000))

    # The actual close fills 500 (everything that exists), but the order
    # asked for 1000 → ``unfilled_qty=500`` reflects what the strategy still
    # wanted that could never execute.
    assert len(bar2.exit_fills) == 1
    fill = bar2.exit_fills[0]
    assert fill.qty == pytest.approx(500.0, rel=1e-9)
    assert fill.unfilled_qty == pytest.approx(500.0, rel=1e-9)
    assert fill.fill_kind == FillKind.PARTIAL
    # Per-order monotonic cumulative — *this exit order's* fills, not the
    # position-wide cumulative_exit_qty.
    assert fill.cumulative_filled_qty == pytest.approx(500.0, rel=1e-9)
    # Position is fully closed (we exited everything that was held); the
    # TradeRecord captures the actual round-trip.
    assert len(bar2.closed_trades) == 1
    assert bar2.closed_trades[0].shares == pytest.approx(500.0, rel=1e-9)
    assert "AAA" not in portfolio.positions
