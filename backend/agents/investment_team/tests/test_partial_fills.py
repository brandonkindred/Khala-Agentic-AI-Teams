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
from investment_team.trading_service.engine.execution_model import (
    FillTerms,
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
        # Default 0.10 cap is what production uses — pin explicitly here so
        # the math in this test file doesn't drift if the default changes.
        execution_model=RealisticExecutionModel(participation_cap=0.10),
    )
    return sim, order_book, portfolio


def _entry_order(
    qty: float,
    *,
    policy: UnfilledPolicy | None = None,
    twap_slices: int | None = None,
) -> OrderRequest:
    return OrderRequest(
        client_order_id="entry-1",
        symbol="AAA",
        side=OrderSide.LONG,
        qty=qty,
        order_type=OrderType.MARKET,
        tif=TimeInForce.DAY,
        unfilled_policy=policy,
        twap_slices=twap_slices,
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


def test_continuation_rejection_preserves_parent_eligible_for_brackets() -> None:
    """When a continuation slice is rejected (risk gate or capital), the
    parent's id must stay registered in ``OrderBook``'s eligible-parent
    set so a later ``submit_attached`` against that parent still works.

    Regression for a Codex P1 review note on PR #417: removing the parent
    with the default ``was_filled=False`` evicted it from the eligible set
    even though the first slice had already opened exposure, breaking
    bracket-attachment flows for requeued entries that terminate on
    continuation rejection.
    """
    portfolio = Portfolio(initial_capital=1_000_000.0)
    order_book = OrderBook()
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

    # First slice fits the 15% cap, the post-extend full size (20%) doesn't.
    parent = order_book.submit(
        OrderRequest(
            client_order_id="parent-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=2_000,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
            unfilled_policy=UnfilledPolicy.REQUEUE_NEXT_BAR,
        ),
        submitted_at="2024-01-01",
        submitted_equity=1_000_000.0,
        expect_brackets=True,
    )
    parent_id = parent.order_id

    sim.process_bar(_bar("2024-01-02", price=100.0, volume=10_000))
    sim.process_bar(_bar("2024-01-03", price=100.0, volume=10_000_000))
    # Continuation rejected; first-slice position remains.
    assert portfolio.positions["AAA"].qty == pytest.approx(1_000.0, rel=1e-9)
    assert order_book.all_pending() == []

    # The parent should still be eligible for bracket attachment — this
    # call must not raise.
    order_book.submit_attached(
        OrderRequest(
            client_order_id="bracket-1",
            symbol="AAA",
            side=OrderSide.SHORT,
            qty=1_000,
            order_type=OrderType.STOP,
            stop_price=95.0,
            tif=TimeInForce.GTC,
        ),
        submitted_at="2024-01-03",
        submitted_equity=1_000_000.0,
        parent_order_id=parent_id,
        oco_group_id="bracket-group-1",
    )


def test_capital_is_conserved_across_fragmented_round_trip() -> None:
    """A multi-slice entry + multi-slice exit at the same price must
    return capital exactly to its starting value (zero costs, zero
    slippage). Per-slice rounding inside ``extend`` / ``partial_close``
    used to introduce cumulative cash drift on fragmented fills.

    Regression for two Codex P2 review notes on PR #417.
    """
    sim, order_book, portfolio = _make_simulator()
    initial = portfolio.capital

    # Entry: REQUEUE_NEXT_BAR with cap forcing ≥ 2 partials. Sub-$10 price
    # so fill_price is rounded to 4 decimals (multiplier with integer qty
    # produces the same notional whether or not we round per-slice, but
    # we still want to assert no drift creeps in).
    order_book.submit(
        _entry_order(2_000, policy=UnfilledPolicy.REQUEUE_NEXT_BAR),
        submitted_at="2024-01-01",
        submitted_equity=initial,
    )
    # Bar 1: cap forces ~50% fill, remainder requeued.
    sim.process_bar(_bar("2024-01-02", price=5.1234, volume=200_000))
    # Bar 2: high volume, remainder fills.
    sim.process_bar(_bar("2024-01-03", price=5.1234, volume=200_000_000))
    assert portfolio.positions["AAA"].qty == pytest.approx(2_000.0, rel=1e-9)

    # Exit at the *same* price via REQUEUE_NEXT_BAR — same fragmentation.
    order_book.submit(
        _exit_order(2_000, policy=UnfilledPolicy.REQUEUE_NEXT_BAR),
        submitted_at="2024-01-03",
        submitted_equity=initial,
    )
    sim.process_bar(_bar("2024-01-04", price=5.1234, volume=200_000))
    sim.process_bar(_bar("2024-01-05", price=5.1234, volume=200_000_000))
    assert "AAA" not in portfolio.positions

    # Zero-cost flat round-trip: capital must equal initial.
    assert portfolio.capital == pytest.approx(initial, rel=1e-9)


def test_over_ask_exit_total_unfilled_capped_at_cap_clipped_only() -> None:
    """When an over-ask exit gets requeued, the never-fillable portion
    must not accumulate into ``total_unfilled_qty`` on every bar — only
    the participation-cap-clipped slice does.

    Regression for a Codex P2 review note on PR #417: the previous
    ``pos.total_unfilled_qty += unfilled`` (with ``unfilled`` measured
    from the request) added the same ghost over-ask amount each bar, so
    a 2000-share exit against a 1000-share position could report > 2000
    unfilled in the final ``TradeRecord``.
    """
    sim, order_book, portfolio = _make_simulator()
    order_book.submit(
        _entry_order(1_000),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )
    sim.process_bar(_bar("2024-01-02", price=100.0, volume=10_000_000))

    order_book.submit(
        _exit_order(2_000, policy=UnfilledPolicy.REQUEUE_NEXT_BAR),
        submitted_at="2024-01-02",
        submitted_equity=10_000_000.0,
    )
    # Bar 1: cap-clipped: fillable=min(2000, 1000)=1000, qty_fraction=0.5,
    # filled=500, cap_clipped=500. pos.qty=500.
    bar1 = sim.process_bar(_bar("2024-01-03", price=100.0, volume=10_000))
    assert bar1.closed_trades == []
    # Bar 2: high volume, fillable=min(remaining, pos.qty)=500, filled=500.
    # cap_clipped=0. Position closes.
    bar2 = sim.process_bar(_bar("2024-01-04", price=100.0, volume=10_000_000))
    assert len(bar2.closed_trades) == 1
    record = bar2.closed_trades[0]
    # total_unfilled_qty must reflect cap-clipped slices only (500), not
    # the carried over-ask amount times each requeue (which would have
    # given 1500 + 1000 = 2500 under the old behavior).
    assert record.total_unfilled_qty == pytest.approx(500.0, rel=1e-9)
    assert record.shares == pytest.approx(1_000.0, rel=1e-9)


def test_multi_bar_exit_records_weighted_bid_price() -> None:
    """A multi-bar partial exit reports a qty-weighted ``exit_bid_price``
    on the TradeRecord (mirroring the weighted ``exit_price``), not just
    the closing bar's reference.

    Regression for a Codex P2 review note on PR #417: leaving
    ``exit_bid_price`` anchored to the final bar mismatched the two
    fields and skewed slippage diagnostics on multi-slice exits.
    """
    sim, order_book, portfolio = _make_simulator()
    order_book.submit(
        _entry_order(2_000),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )
    sim.process_bar(_bar("2024-01-02", price=100.0, volume=10_000_000))

    order_book.submit(
        _exit_order(2_000, policy=UnfilledPolicy.REQUEUE_NEXT_BAR),
        submitted_at="2024-01-02",
        submitted_equity=10_000_000.0,
    )
    # Bar 1: cap-clipped 50% partial @ ref=110 → 1000 shares.
    sim.process_bar(_bar("2024-01-03", price=110.0, volume=10_000))
    # Bar 2: full close @ ref=120 → 1000 shares.
    bar2 = sim.process_bar(_bar("2024-01-04", price=120.0, volume=10_000_000))

    assert len(bar2.closed_trades) == 1
    record = bar2.closed_trades[0]
    # Weighted bid = (1000 * 110 + 1000 * 120) / 2000 = 115 — matching the
    # weighted exit_price (since slippage is zero in this test).
    assert record.exit_bid_price == pytest.approx(115.0, rel=1e-9)
    assert record.exit_price == pytest.approx(115.0, rel=1e-9)


def test_multi_bar_entry_records_weighted_entry_bid_price() -> None:
    """A multi-bar partial entry continuation reports a qty-weighted
    ``entry_bid_price`` on the TradeRecord (mirroring the weighted
    ``entry_price``), not the first slice's reference.

    Regression for a Codex P2 review note on PR #417: ``Portfolio.extend``
    used to update ``entry_price`` to a weighted average but leave
    ``entry_bid_price`` anchored at the first slice, skewing entry-side
    slippage metrics on fragmented entries.
    """
    sim, order_book, portfolio = _make_simulator()

    # Bar 1: partial fill @ ref=100, cap forces 50% → 1000 shares filled.
    order_book.submit(
        _entry_order(2_000, policy=UnfilledPolicy.REQUEUE_NEXT_BAR),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )
    sim.process_bar(_bar("2024-01-02", price=100.0, volume=10_000))
    # Bar 2: full continuation @ ref=110.
    sim.process_bar(_bar("2024-01-03", price=110.0, volume=10_000_000))

    pos = portfolio.positions["AAA"]
    # Weighted entry bid = (1000 * 100 + 1000 * 110) / 2000 = 105 — same as
    # weighted entry_price since slippage is zero.
    assert pos.entry_bid_price == pytest.approx(105.0, rel=1e-9)
    assert pos.entry_price == pytest.approx(105.0, rel=1e-9)

    # Close cleanly at the same weighted price; TradeRecord propagates
    # the weighted entry bid.
    order_book.submit(
        _exit_order(2_000),
        submitted_at="2024-01-03",
        submitted_equity=10_000_000.0,
    )
    bar3 = sim.process_bar(_bar("2024-01-04", price=120.0, volume=10_000_000))
    assert len(bar3.closed_trades) == 1
    assert bar3.closed_trades[0].entry_bid_price == pytest.approx(105.0, rel=1e-9)


class _ScriptedFractionModel:
    """Test stub: returns a per-bar ``qty_fraction`` keyed by bar timestamp.

    Lets us drive ``_fill_entry`` / ``_continue_entry`` into the
    ``filled_qty <= 0`` zero-fraction branch deterministically — the
    realistic model can't actually return ``qty_fraction = 0`` from any
    legitimate input, so the only way to test that branch's policy
    handling is with a controlled stub.
    """

    name = "scripted-fraction"

    def __init__(self, fractions: dict[str, float]) -> None:
        self._fractions = fractions

    def compute_fill_terms(self, req, bar, next_bar):
        ref = bar.open
        return FillTerms(
            reference_price=ref,
            qty_fraction=self._fractions.get(bar.timestamp, 1.0),
            extra_slip_bps=0.0,
        )


def test_zero_fraction_continuation_honors_requeue_policy() -> None:
    """A continuation slice that returns ``qty_fraction = 0`` (e.g.
    transient no-liquidity bar under a custom execution model) must
    honor ``UnfilledPolicy.REQUEUE_NEXT_BAR`` and re-queue the
    remainder for the next bar instead of permanently dropping it.

    Regression for a Codex P2 review note on PR #417: the previous
    branch unconditionally called ``order_book.remove`` and ignored
    the policy.
    """
    portfolio = Portfolio(initial_capital=10_000_000.0)
    order_book = OrderBook()
    sim = FillSimulator(
        portfolio=portfolio,
        order_book=order_book,
        risk_filter=RiskFilter(RiskLimits(max_position_pct=100, max_gross_leverage=10.0)),
        config=FillSimulatorConfig(slippage_bps=0.0, transaction_cost_bps=0.0),
        bar_safety=BarSafetyAssertion(),
        execution_model=_ScriptedFractionModel(
            {
                "2024-01-02": 0.5,  # bar 1: 50% partial
                "2024-01-03": 0.0,  # bar 2: no liquidity
                "2024-01-04": 1.0,  # bar 3: recovers, fills the rest
            }
        ),
    )

    order_book.submit(
        _entry_order(2_000, policy=UnfilledPolicy.REQUEUE_NEXT_BAR),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )
    # Bar 1: 50% partial → pos.qty=1000, requeue 1000.
    sim.process_bar(_bar("2024-01-02", price=100.0))
    assert portfolio.positions["AAA"].qty == pytest.approx(1_000.0, rel=1e-9)

    # Bar 2: zero-fraction → emit REJECTED Fill, but order must still
    # be in the book (REQUEUE policy honored).
    bar2 = sim.process_bar(_bar("2024-01-03", price=100.0))
    assert len(bar2.entry_fills) == 1
    assert bar2.entry_fills[0].fill_kind == FillKind.REJECTED
    assert order_book.all_pending(), "REQUEUE_NEXT_BAR must keep the remainder pending"

    # Bar 3: recovers → continuation fills the remaining 1000.
    bar3 = sim.process_bar(_bar("2024-01-04", price=100.0))
    assert len(bar3.entry_fills) == 1
    assert bar3.entry_fills[0].fill_kind == FillKind.FULL
    assert portfolio.positions["AAA"].qty == pytest.approx(2_000.0, rel=1e-9)


def test_zero_fraction_continuation_drop_keeps_parent_eligible() -> None:
    """When a zero-fraction continuation hits a non-REQUEUE policy
    (DROP) and the order is removed, the parent must stay registered
    in ``OrderBook``'s eligible-parent set — its first slice already
    opened a position, so subsequent ``submit_attached`` calls must
    still work.

    Regression for a Codex P2 review note on PR #417: the previous
    branch called ``order_book.remove`` with the default
    ``was_filled=False`` and evicted the parent.
    """
    portfolio = Portfolio(initial_capital=10_000_000.0)
    order_book = OrderBook()
    sim = FillSimulator(
        portfolio=portfolio,
        order_book=order_book,
        risk_filter=RiskFilter(RiskLimits(max_position_pct=100, max_gross_leverage=10.0)),
        config=FillSimulatorConfig(slippage_bps=0.0, transaction_cost_bps=0.0),
        bar_safety=BarSafetyAssertion(),
        execution_model=_ScriptedFractionModel(
            {
                "2024-01-02": 0.5,  # bar 1: 50% partial
                "2024-01-03": 0.0,  # bar 2: zero-fraction continuation
            }
        ),
    )

    parent = order_book.submit(
        OrderRequest(
            client_order_id="parent-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=2_000,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
            unfilled_policy=UnfilledPolicy.DROP,
        ),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
        expect_brackets=True,
    )

    sim.process_bar(_bar("2024-01-02", price=100.0))
    sim.process_bar(_bar("2024-01-03", price=100.0))

    # Continuation dropped under DROP policy; first-slice position remains.
    assert portfolio.positions["AAA"].qty == pytest.approx(1_000.0, rel=1e-9)
    assert order_book.all_pending() == []

    # Parent must still be eligible for bracket attachment.
    order_book.submit_attached(
        OrderRequest(
            client_order_id="bracket-stop",
            symbol="AAA",
            side=OrderSide.SHORT,
            qty=1_000,
            order_type=OrderType.STOP,
            stop_price=95.0,
            tif=TimeInForce.GTC,
        ),
        submitted_at="2024-01-03",
        submitted_equity=10_000_000.0,
        parent_order_id=parent.order_id,
        oco_group_id="bracket-group-1",
    )


# ---------------------------------------------------------------------------
# TWAP_N unfilled_policy (#387)
# ---------------------------------------------------------------------------


def test_twap_n_three_bars_slices_sum_to_original() -> None:
    """``TWAP_N=3`` slices a cap-clipped entry across three bars. The first
    bar fills naturally under the participation cap (seeding
    ``slices_remaining = N - 1``); the intermediate bar targets
    ``remaining / slices_remaining``; the final bar force-flushes the
    residual regardless of the cap. Cumulative entry must equal the
    original requested qty.
    """
    sim, order_book, portfolio = _make_simulator()
    order_book.submit(
        _entry_order(2_000, policy=UnfilledPolicy.TWAP_N, twap_slices=3),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )

    # Bar 1: low ADV → 50% cap-clipped partial. 1_000 fills, 1_000 unfilled.
    # ``_handle_entry_remainder`` seeds ``slices_remaining = 3 - 1 = 2``.
    bar1 = sim.process_bar(_bar("2024-01-02", price=100.0, volume=10_000))
    assert len(bar1.entry_fills) == 1
    fill1 = bar1.entry_fills[0]
    assert fill1.fill_kind == FillKind.PARTIAL
    assert fill1.qty == pytest.approx(1_000.0, rel=1e-9)
    assert fill1.unfilled_qty == pytest.approx(1_000.0, rel=1e-9)
    pending_after_bar1 = order_book.all_pending()
    assert len(pending_after_bar1) == 1
    assert pending_after_bar1[0].twap_slices_remaining == 2

    # Bar 2: ample ADV → no cap clip; TWAP target = remaining / 2 = 500.
    # ``slices_remaining`` decrements 2 → 1; 500 stays pending.
    bar2 = sim.process_bar(_bar("2024-01-03", price=100.0, volume=10_000_000))
    assert len(bar2.entry_fills) == 1
    fill2 = bar2.entry_fills[0]
    assert fill2.fill_kind == FillKind.PARTIAL
    assert fill2.qty == pytest.approx(500.0, rel=1e-9)
    assert fill2.cumulative_filled_qty == pytest.approx(1_500.0, rel=1e-9)
    pending_after_bar2 = order_book.all_pending()
    assert len(pending_after_bar2) == 1
    assert pending_after_bar2[0].twap_slices_remaining == 1
    assert pending_after_bar2[0].remaining_qty == pytest.approx(500.0, rel=1e-9)

    # Bar 3: ``slices_remaining == 1`` → final slice force-flushes the full
    # remainder (500) bypassing the participation cap. ``unfilled == 0`` →
    # order is removed cleanly.
    bar3 = sim.process_bar(_bar("2024-01-04", price=100.0, volume=10_000))
    assert len(bar3.entry_fills) == 1
    fill3 = bar3.entry_fills[0]
    assert fill3.fill_kind == FillKind.FULL
    assert fill3.qty == pytest.approx(500.0, rel=1e-9)
    assert fill3.unfilled_qty == pytest.approx(0.0, abs=1e-9)
    assert fill3.cumulative_filled_qty == pytest.approx(2_000.0, rel=1e-9)
    assert order_book.all_pending() == []

    # Cumulative entry equals the original request; position holds the full
    # 2_000 shares across the three sliced fills.
    pos = portfolio.positions["AAA"]
    assert pos.qty == pytest.approx(2_000.0, rel=1e-9)
    assert pos.original_qty == pytest.approx(2_000.0, rel=1e-9)
    assert pos.partial_fill_count == 3


def test_twap_n_full_first_bar_clears_cleanly() -> None:
    """``TWAP_N=2`` on a high-ADV bar that absorbs the whole entry: bar 1
    emits a ``FULL`` fill, the order is removed (``unfilled == 0`` skips the
    TWAP requeue branch), and bar 2 sees an empty book — no spurious
    second-slice fill.
    """
    sim, order_book, portfolio = _make_simulator()
    order_book.submit(
        _entry_order(200, policy=UnfilledPolicy.TWAP_N, twap_slices=2),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )

    # Bar 1: ample volume → no cap clip → full fill on the first slice.
    bar1 = sim.process_bar(_bar("2024-01-02", price=100.0, volume=10_000_000))
    assert len(bar1.entry_fills) == 1
    fill = bar1.entry_fills[0]
    assert fill.fill_kind == FillKind.FULL
    assert fill.qty == pytest.approx(200.0, rel=1e-9)
    assert fill.unfilled_qty == pytest.approx(0.0, abs=1e-9)
    # Order removed; ``twap_slices_remaining`` was never seeded (no requeue).
    assert order_book.all_pending() == []

    pos = portfolio.positions["AAA"]
    assert pos.qty == pytest.approx(200.0, rel=1e-9)
    assert pos.partial_fill_count == 1

    # Bar 2: empty book — TWAP_N must not synthesize a second-slice fill
    # against an order that already terminated.
    bar2 = sim.process_bar(_bar("2024-01-03", price=100.0, volume=10_000_000))
    assert bar2.entry_fills == []
    assert bar2.exit_fills == []
    assert bar2.closed_trades == []
    assert order_book.all_pending() == []


def test_twap_n_validation_requires_two_or_more_slices() -> None:
    """``validate_prices`` (Step 1, #383) gates malformed TWAP_N submissions.

    Re-asserted here so the partial-fill test suite's own contract surface
    is exercised: a strategy can't submit ``TWAP_N`` with ``twap_slices < 2``
    or ``twap_slices is None``, and ``twap_slices`` is rejected when the
    policy is anything other than ``TWAP_N``.
    """
    # twap_slices < 2.
    with pytest.raises(ValueError, match="twap_n policy requires twap_slices >= 2"):
        OrderRequest(
            client_order_id="entry-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=100,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
            unfilled_policy=UnfilledPolicy.TWAP_N,
            twap_slices=1,
        ).validate_prices()

    # twap_slices missing entirely.
    with pytest.raises(ValueError, match="twap_n policy requires twap_slices >= 2"):
        OrderRequest(
            client_order_id="entry-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=100,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
            unfilled_policy=UnfilledPolicy.TWAP_N,
        ).validate_prices()

    # Sanity: TWAP_N + twap_slices=2 validates without raising.
    OrderRequest(
        client_order_id="entry-1",
        symbol="AAA",
        side=OrderSide.LONG,
        qty=100,
        order_type=OrderType.MARKET,
        tif=TimeInForce.DAY,
        unfilled_policy=UnfilledPolicy.TWAP_N,
        twap_slices=2,
    ).validate_prices()


def test_twap_n_terminal_slice_respects_zero_liquidity() -> None:
    """The TWAP_N final-slice force-flush bypasses the *participation cap*
    but must still respect a hard zero-liquidity signal
    (``qty_fraction == 0``) — otherwise custom execution models that use
    a zero fraction to encode "no fill possible on this bar" (halt,
    zero-volume bar) would produce impossible fills.

    Regression for a Codex P2 review note on PR #419: when the terminal
    slice's ``qty_fraction`` is 0 the order must drop cleanly via the
    ``slices_remaining <= 0`` branch in ``_handle_entry_remainder``
    rather than manufacturing a phantom fill against the bar.
    """
    portfolio = Portfolio(initial_capital=10_000_000.0)
    order_book = OrderBook()
    sim = FillSimulator(
        portfolio=portfolio,
        order_book=order_book,
        risk_filter=RiskFilter(RiskLimits(max_position_pct=100, max_gross_leverage=10.0)),
        config=FillSimulatorConfig(slippage_bps=0.0, transaction_cost_bps=0.0),
        bar_safety=BarSafetyAssertion(),
        execution_model=_ScriptedFractionModel(
            {
                "2024-01-02": 0.5,  # bar 1: 50% partial → seed slices_remaining=1
                "2024-01-03": 0.0,  # bar 2 (terminal): zero liquidity
            }
        ),
    )

    order_book.submit(
        _entry_order(2_000, policy=UnfilledPolicy.TWAP_N, twap_slices=2),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )

    # Bar 1: 50% partial. ``_handle_entry_remainder`` seeds
    # ``slices_remaining = 2 - 1 = 1`` (terminal-slice flag for bar 2).
    bar1 = sim.process_bar(_bar("2024-01-02", price=100.0))
    assert bar1.entry_fills[0].fill_kind == FillKind.PARTIAL
    assert portfolio.positions["AAA"].qty == pytest.approx(1_000.0, rel=1e-9)
    pending = order_book.all_pending()
    assert len(pending) == 1
    assert pending[0].twap_slices_remaining == 1

    # Bar 2: terminal slice, but qty_fraction=0 → no fill. The cap-bypass
    # must NOT manufacture a phantom 1_000-share fill against a halted
    # bar; the order drops cleanly via the slices_remaining<=0 branch.
    bar2 = sim.process_bar(_bar("2024-01-03", price=100.0))
    assert len(bar2.entry_fills) == 1
    assert bar2.entry_fills[0].fill_kind == FillKind.REJECTED
    assert bar2.entry_fills[0].qty == pytest.approx(0.0, abs=1e-9)
    assert bar2.entry_fills[0].unfilled_qty == pytest.approx(1_000.0, rel=1e-9)
    assert order_book.all_pending() == []

    # Position still reflects only what actually filled (bar 1's 1_000) —
    # no phantom shares from a manufactured terminal fill.
    assert portfolio.positions["AAA"].qty == pytest.approx(1_000.0, rel=1e-9)
    assert portfolio.positions["AAA"].partial_fill_count == 1
