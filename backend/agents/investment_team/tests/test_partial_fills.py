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


def _exit_order(
    qty: float,
    *,
    policy: UnfilledPolicy | None = None,
    twap_slices: int | None = None,
) -> OrderRequest:
    return OrderRequest(
        client_order_id="exit-1",
        symbol="AAA",
        side=OrderSide.SHORT,
        qty=qty,
        order_type=OrderType.MARKET,
        tif=TimeInForce.DAY,
        unfilled_policy=policy,
        twap_slices=twap_slices,
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


class _ScriptedTriggerModel:
    """Test stub: returns ``None`` (no-trigger) on bars not in the script.

    Lets us simulate a ``LIMIT``/``STOP`` order that doesn't cross on
    every bar — needed to exercise the elapsed-bar TWAP slice
    consumption path that fires when ``compute_fill_terms`` returns
    ``None``. Bars present in the dict return ``FillTerms`` with the
    given ``qty_fraction``.
    """

    name = "scripted-trigger"

    def __init__(self, triggers: dict[str, float]) -> None:
        self._triggers = triggers

    def compute_fill_terms(self, req, bar, next_bar):
        if bar.timestamp not in self._triggers:
            return None
        ref = bar.open
        return FillTerms(
            reference_price=ref,
            qty_fraction=self._triggers[bar.timestamp],
            extra_slip_bps=0.0,
        )


class _PerOrderTriggerModel:
    """Test stub: per-(client_order_id, timestamp) trigger script.

    Returns ``None`` on bars not in the order's script — needed to
    construct races where one order triggers on a bar while another
    on the same symbol does not (e.g. a stale ``TWAP_N`` exit that's
    untouched while a fresh entry order opens a new position).
    """

    name = "per-order-trigger"

    def __init__(self, triggers: dict[str, dict[str, float]]) -> None:
        # ``client_order_id`` → ``{bar.timestamp: qty_fraction}``.
        self._triggers = triggers

    def compute_fill_terms(self, req, bar, next_bar):
        order_triggers = self._triggers.get(req.client_order_id, {})
        if bar.timestamp not in order_triggers:
            return None
        return FillTerms(
            reference_price=bar.open,
            qty_fraction=order_triggers[bar.timestamp],
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

    # Bar 3: ``slices_remaining == 1`` → final slice. With ample
    # liquidity (``qty_fraction=1.0``), ``filled = target_qty * 1.0 =
    # 500`` fully clears the order. (Cap-clipped final slices don't
    # bypass the cap — see
    # ``test_twap_n_terminal_slice_drops_cap_clipped_residual``.)
    bar3 = sim.process_bar(_bar("2024-01-04", price=100.0, volume=10_000_000))
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


def test_twap_n_zero_fill_first_bar_then_slices_on_recovery() -> None:
    """A ``TWAP_N`` order that *fails to fill* on its first bar
    (``qty_fraction == 0``) must still honor the N-slice schedule on
    subsequent bars — not collapse into a single full fill the moment
    liquidity recovers.

    The post-rejection retry routes back through ``_fill_entry`` (no
    position has opened yet, so the per-bar dispatch sees no
    ``existing_pos`` and ``cumulative_filled_qty == 0``). Without TWAP-
    aware sizing in ``_fill_entry``, the next bar's full-volume fill
    would absorb the entire remainder and the strategy's TWAP horizon
    would be ignored. Regression for a Codex P1 review note on PR #419.
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
                "2024-01-02": 0.0,  # bar 1: zero-fill rejection — seed sr=2
                "2024-01-03": 1.0,  # bar 2: full liquidity → first real fill
                "2024-01-04": 1.0,  # bar 3: terminal slice (force-flush)
            }
        ),
    )

    order_book.submit(
        _entry_order(2_000, policy=UnfilledPolicy.TWAP_N, twap_slices=3),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )

    # Bar 1: zero-fill REJECTED. ``_handle_entry_remainder`` seeds
    # ``slices_remaining = 3 - 1 = 2``; remainder requeued unchanged.
    bar1 = sim.process_bar(_bar("2024-01-02", price=100.0))
    assert bar1.entry_fills[0].fill_kind == FillKind.REJECTED
    pending = order_book.all_pending()
    assert len(pending) == 1
    assert pending[0].twap_slices_remaining == 2
    assert pending[0].remaining_qty == pytest.approx(2_000.0, rel=1e-9)
    assert "AAA" not in portfolio.positions, "no position should have opened"

    # Bar 2: ``_fill_entry`` runs again (no existing position). With
    # TWAP-aware sizing in ``_fill_entry``, the slice target is
    # ``remaining / slices_remaining = 2_000 / 2 = 1_000`` — NOT the full
    # 2_000. ``slices_remaining`` decrements 2 → 1.
    bar2 = sim.process_bar(_bar("2024-01-03", price=100.0))
    assert len(bar2.entry_fills) == 1
    fill2 = bar2.entry_fills[0]
    assert fill2.fill_kind == FillKind.PARTIAL
    assert fill2.qty == pytest.approx(1_000.0, rel=1e-9)
    pending = order_book.all_pending()
    assert len(pending) == 1
    assert pending[0].twap_slices_remaining == 1
    assert portfolio.positions["AAA"].qty == pytest.approx(1_000.0, rel=1e-9)

    # Bar 3: terminal slice (sr == 1) → force-flush full remainder.
    bar3 = sim.process_bar(_bar("2024-01-04", price=100.0))
    fill3 = bar3.entry_fills[0]
    assert fill3.fill_kind == FillKind.FULL
    assert fill3.qty == pytest.approx(1_000.0, rel=1e-9)
    assert order_book.all_pending() == []
    assert portfolio.positions["AAA"].qty == pytest.approx(2_000.0, rel=1e-9)


def test_twap_n_exit_slices_remainder_across_n_bars() -> None:
    """Exit orders honor ``TWAP_N`` symmetrically with entry orders.

    The contract validator does not (and cannot) statically distinguish
    entries from exits — entry-vs-exit is determined at fill-time by
    whether a position exists. So accepting ``TWAP_N`` for any order
    means honoring it on both sides; otherwise an accepted exit-side
    ``TWAP_N`` would silently execute as ``DROP``. Regression for a
    Codex P1 review note on PR #419.
    """
    sim, order_book, portfolio = _make_simulator()

    # Open a 2_000-share LONG position via a normal high-volume entry.
    order_book.submit(
        _entry_order(2_000),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )
    sim.process_bar(_bar("2024-01-02", price=100.0, volume=10_000_000))
    assert portfolio.positions["AAA"].qty == pytest.approx(2_000.0, rel=1e-9)

    # Submit a TWAP_N=3 exit. First bar is low-ADV (cap-clipped to 50%);
    # subsequent bars slice the remainder per TWAP.
    order_book.submit(
        _exit_order(2_000, policy=UnfilledPolicy.TWAP_N, twap_slices=3),
        submitted_at="2024-01-02",
        submitted_equity=10_000_000.0,
    )

    # Bar A (exit slice 1): low volume → 50% cap-clip → 1_000 exits,
    # 1_000 stays open. Handler seeds slices_remaining = 3 - 1 = 2.
    bar_a = sim.process_bar(_bar("2024-01-03", price=105.0, volume=10_000))
    assert len(bar_a.exit_fills) == 1
    assert bar_a.exit_fills[0].fill_kind == FillKind.PARTIAL
    assert bar_a.exit_fills[0].qty == pytest.approx(1_000.0, rel=1e-9)
    assert portfolio.positions["AAA"].qty == pytest.approx(1_000.0, rel=1e-9)
    pending = order_book.all_pending()
    assert len(pending) == 1
    assert pending[0].twap_slices_remaining == 2

    # Bar B (exit slice 2): ample volume → no cap-clip; TWAP target =
    # remaining / 2 = 500. slices_remaining decrements 2 → 1.
    bar_b = sim.process_bar(_bar("2024-01-04", price=105.0, volume=10_000_000))
    assert len(bar_b.exit_fills) == 1
    assert bar_b.exit_fills[0].fill_kind == FillKind.PARTIAL
    assert bar_b.exit_fills[0].qty == pytest.approx(500.0, rel=1e-9)
    assert portfolio.positions["AAA"].qty == pytest.approx(500.0, rel=1e-9)
    pending = order_book.all_pending()
    assert len(pending) == 1
    assert pending[0].twap_slices_remaining == 1

    # Bar C (terminal slice): sr == 1 → with ample liquidity
    # (``qty_fraction=1.0``) the full remainder (500) clears; position
    # fully closes; TradeRecord emitted. Cap-clipped final slices drop
    # residual instead — see
    # ``test_twap_n_terminal_slice_drops_cap_clipped_residual``.
    bar_c = sim.process_bar(_bar("2024-01-05", price=105.0, volume=10_000_000))
    assert len(bar_c.exit_fills) == 1
    assert bar_c.exit_fills[0].fill_kind == FillKind.FULL
    assert bar_c.exit_fills[0].qty == pytest.approx(500.0, rel=1e-9)
    assert "AAA" not in portfolio.positions
    assert order_book.all_pending() == []
    assert len(bar_c.closed_trades) == 1
    record = bar_c.closed_trades[0]
    assert record.shares == pytest.approx(2_000.0, rel=1e-9)


def test_partial_exit_remainder_drops_when_position_vanished() -> None:
    """A partially-filled exit whose position has been closed by another
    order before the remainder executes must drop cleanly on the next
    bar — *not* be re-routed through ``_fill_entry`` as a fresh
    opposite-side entry. Regression for a Codex P1 review note on PR
    #419: TWAP_N exit support exposed this latent dispatch path
    (``existing_pos is None`` + ``cumulative_filled_qty > 0`` was
    falling through to ``is_entry``, opening a new short with the
    requeued sell-side remainder).
    """
    sim, order_book, portfolio = _make_simulator()

    # Setup: enter LONG 2_000 cleanly.
    order_book.submit(
        _entry_order(2_000),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )
    sim.process_bar(_bar("2024-01-02", price=100.0, volume=10_000_000))
    assert portfolio.positions["AAA"].qty == pytest.approx(2_000.0, rel=1e-9)

    # Submit a TWAP_N=3 SHORT exit. Bar 2 cap-clips to 50% → 1_000
    # closes, position=1_000, TWAP remainder pending with cumulative=
    # 1_000, remaining=1_000, slices_remaining=2.
    order_book.submit(
        _exit_order(2_000, policy=UnfilledPolicy.TWAP_N, twap_slices=3),
        submitted_at="2024-01-02",
        submitted_equity=10_000_000.0,
    )
    sim.process_bar(_bar("2024-01-03", price=100.0, volume=10_000))
    assert portfolio.positions["AAA"].qty == pytest.approx(1_000.0, rel=1e-9)
    pending = order_book.all_pending()
    assert len(pending) == 1
    assert pending[0].cumulative_filled_qty == pytest.approx(1_000.0, rel=1e-9)

    # Simulate "another order closed the position" by directly closing
    # the remaining 1_000. In production this would be e.g. a separate
    # MARKET exit or a stop trigger that processed earlier in the same
    # bar — same end-state from the TWAP remainder's perspective.
    portfolio.partial_close("AAA", 1_000.0, 100.0, 100.0)
    portfolio.close("AAA", 100.0)
    assert "AAA" not in portfolio.positions

    # Bar 4: process_bar snapshots the still-pending TWAP remainder.
    # ``existing_pos is None`` and ``cumulative_filled_qty > 0`` →
    # the dispatch must drop the order, NOT route it through
    # ``_fill_entry`` (which would open a brand-new short).
    out = sim.process_bar(_bar("2024-01-04", price=100.0, volume=10_000_000))

    # No phantom short entry; the TWAP remainder is removed cleanly.
    assert out.entry_fills == [], "stale TWAP remainder must NOT reopen as a fresh short"
    assert out.exit_fills == []
    assert "AAA" not in portfolio.positions
    assert order_book.all_pending() == []


def test_twap_n_consumes_slice_on_untriggered_bar() -> None:
    """``TWAP_N`` orders must honor an N-bar horizon measured in
    *elapsed* bars, not triggered bars. A LIMIT/STOP TWAP order whose
    trigger geometry skips intermediate bars (``compute_fill_terms``
    returns ``None``) must still consume a slice on those bars,
    otherwise the schedule can run for more than ``twap_slices`` bars
    and TWAP duration becomes a function of price action rather than
    wall time. Regression for a Codex P2 review note on PR #419.

    Scenario: TWAP_N=3 entry. Bar 1 partial fills (seeds slices_remaining
    = 2). Bar 2 is *untouched* (no trigger) → must decrement to 1. Bar
    3 triggers and force-flushes the remainder. Total bars used = 3.
    """
    portfolio = Portfolio(initial_capital=10_000_000.0)
    order_book = OrderBook()
    sim = FillSimulator(
        portfolio=portfolio,
        order_book=order_book,
        risk_filter=RiskFilter(RiskLimits(max_position_pct=100, max_gross_leverage=10.0)),
        config=FillSimulatorConfig(slippage_bps=0.0, transaction_cost_bps=0.0),
        bar_safety=BarSafetyAssertion(),
        execution_model=_ScriptedTriggerModel(
            {
                "2024-01-02": 0.5,  # bar 1: partial fill (seeds sr=2)
                # 2024-01-03 deliberately absent — untriggered, must
                # still consume a slice (sr 2 → 1).
                "2024-01-04": 1.0,  # bar 3: terminal slice, force-flush
            }
        ),
    )

    order_book.submit(
        _entry_order(2_000, policy=UnfilledPolicy.TWAP_N, twap_slices=3),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )

    # Bar 1: partial fill (1_000) → seeds slices_remaining = 2.
    bar1 = sim.process_bar(_bar("2024-01-02", price=100.0))
    assert bar1.entry_fills[0].fill_kind == FillKind.PARTIAL
    assert portfolio.positions["AAA"].qty == pytest.approx(1_000.0, rel=1e-9)
    pending = order_book.all_pending()
    assert pending[0].twap_slices_remaining == 2

    # Bar 2: untriggered (compute_fill_terms returns None). The TWAP
    # branch in process_bar must decrement slices_remaining anyway so
    # the N-bar horizon is honored.
    bar2 = sim.process_bar(_bar("2024-01-03", price=100.0))
    assert bar2.entry_fills == [], "untouched bar should not emit a fill"
    pending = order_book.all_pending()
    assert len(pending) == 1
    assert pending[0].twap_slices_remaining == 1, (
        "untouched bar must consume a TWAP slice — otherwise the "
        "schedule runs past its declared horizon"
    )
    # ``remaining_qty`` must be unchanged (no fill happened).
    assert pending[0].remaining_qty == pytest.approx(1_000.0, rel=1e-9)

    # Bar 3: trigger fires, terminal slice force-flushes the remainder.
    bar3 = sim.process_bar(_bar("2024-01-04", price=100.0))
    assert bar3.entry_fills[0].fill_kind == FillKind.FULL
    assert bar3.entry_fills[0].qty == pytest.approx(1_000.0, rel=1e-9)
    assert portfolio.positions["AAA"].qty == pytest.approx(2_000.0, rel=1e-9)
    assert order_book.all_pending() == []


def test_twap_n_drops_at_horizon_when_no_trigger_recovers() -> None:
    """A ``TWAP_N`` order whose trigger geometry never recovers after
    the first fill must drop at the declared N-bar horizon — the
    elapsed-bar slice consumption fires the ``slices_remaining <= 0``
    branch in ``process_bar`` even when the order is never triggered
    again. Companion to the consume-on-elapsed test above.
    """
    portfolio = Portfolio(initial_capital=10_000_000.0)
    order_book = OrderBook()
    sim = FillSimulator(
        portfolio=portfolio,
        order_book=order_book,
        risk_filter=RiskFilter(RiskLimits(max_position_pct=100, max_gross_leverage=10.0)),
        config=FillSimulatorConfig(slippage_bps=0.0, transaction_cost_bps=0.0),
        bar_safety=BarSafetyAssertion(),
        execution_model=_ScriptedTriggerModel(
            {
                "2024-01-02": 0.5,  # bar 1: partial fill (seeds sr=2)
                # bars 2 + 3 absent → both untriggered, sr ticks 2→1→0
            }
        ),
    )

    order_book.submit(
        _entry_order(2_000, policy=UnfilledPolicy.TWAP_N, twap_slices=3),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )

    sim.process_bar(_bar("2024-01-02", price=100.0))
    assert portfolio.positions["AAA"].qty == pytest.approx(1_000.0, rel=1e-9)

    # Bar 2: untouched → sr 2 → 1.
    sim.process_bar(_bar("2024-01-03", price=100.0))
    assert order_book.all_pending()[0].twap_slices_remaining == 1

    # Bar 3: untouched again → sr 1 → 0 → order dropped at horizon end.
    sim.process_bar(_bar("2024-01-04", price=100.0))
    assert order_book.all_pending() == []
    # Position reflects only what actually filled (the bar-1 1_000).
    assert portfolio.positions["AAA"].qty == pytest.approx(1_000.0, rel=1e-9)


def test_twap_n_terminal_slice_drops_cap_clipped_residual() -> None:
    """The TWAP horizon is bounded by ``slices_remaining``; the terminal
    slice does NOT bypass ``terms.qty_fraction`` (the issue offers two
    options — "force-flush regardless of cap" vs. "clip to cap and
    re-route" — and we pick the latter so custom execution models that
    use ``qty_fraction`` for hard liquidity constraints aren't
    overruled).

    Concretely: TWAP_N=2 with both bars cap-clipped to 50%. Bar 1 fills
    1_000 (of 2_000), seeds slices_remaining=1. Bar 2 (terminal) targets
    1_000 but the cap clips to 500. The remaining 500 is NOT
    force-flushed; it drops cleanly via ``slices_remaining <= 0`` and
    the strategy gets a partial fill report. Regression for Codex P1
    review notes (entry+exit) on PR #419.
    """
    sim, order_book, portfolio = _make_simulator()
    order_book.submit(
        _entry_order(2_000, policy=UnfilledPolicy.TWAP_N, twap_slices=2),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )

    # Bar 1: low ADV → 50% cap-clip; seeds slices_remaining=1.
    bar1 = sim.process_bar(_bar("2024-01-02", price=100.0, volume=10_000))
    assert bar1.entry_fills[0].qty == pytest.approx(1_000.0, rel=1e-9)
    pos = portfolio.positions["AAA"]
    assert pos.qty == pytest.approx(1_000.0, rel=1e-9)

    # Bar 2 (terminal slice, sr==1): target=remaining=1_000, but
    # ``qty_fraction`` of this bar is 0.5 → ``filled = target * 0.5 =
    # 500``. NOT force-flushed. ``unfilled=500`` → handler decrements
    # sr 1 → 0 → drops the order. Strategy sees a PARTIAL fill (500)
    # with 500 unfilled.
    bar2 = sim.process_bar(_bar("2024-01-03", price=100.0, volume=10_000))
    assert len(bar2.entry_fills) == 1
    fill2 = bar2.entry_fills[0]
    assert fill2.fill_kind == FillKind.PARTIAL
    assert fill2.qty == pytest.approx(500.0, rel=1e-9)
    assert fill2.unfilled_qty == pytest.approx(500.0, rel=1e-9)
    # Position reflects what actually filled (1_000 + 500 = 1_500),
    # NOT the original 2_000 — because the terminal slice was honest
    # about the cap.
    assert portfolio.positions["AAA"].qty == pytest.approx(1_500.0, rel=1e-9)
    assert order_book.all_pending() == []


def test_twap_n_intermediate_slice_does_not_inflate_unfilled_metrics() -> None:
    """Intermediate TWAP slices defer quantity to subsequent bars by
    design. That deferred quantity must NOT be counted as a liquidity
    shortfall in ``Position.participation_clipped`` /
    ``total_unfilled_qty`` — those metrics are reserved for actual
    execution-model cap-clips.

    Concretely: TWAP_N=2 first bar with ample liquidity → slice fully
    fills its target (no cap-clip). The deferred remainder (the half
    that goes to bar 2) is scheduled work, not a shortfall. Regression
    for Codex P2 review note on PR #419 (commit 1d92ae5).
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
                "2024-01-02": 1.0,  # ample liquidity — no cap clip on slice
                "2024-01-03": 1.0,
            }
        ),
    )
    order_book.submit(
        _entry_order(2_000, policy=UnfilledPolicy.TWAP_N, twap_slices=2),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )

    # Bar 1: sr=None → target = full request = 2_000. With qty_fraction=
    # 1.0, slice fills 2_000 in one bar (fill_kind=FULL, no remainder).
    # Wait — this is the test_twap_n_full_first_bar_clears_cleanly
    # path. We need a TWAP where the *first* bar is sliced — so seed
    # slices_remaining via a bar-1 rejection then a bar-2 ample fill.
    # Skip this approach; use the explicit-target path below.
    # Workaround: hand-mutate twap_slices_remaining after first
    # rejection so bar 2's _fill_entry hits the sliced path.

    # Bar 1: zero-fill rejection → seeds sr=1 (TWAP_N=2 - 1 = 1).
    sim.execution_model = _ScriptedFractionModel(
        {
            "2024-01-02": 0.0,  # bar 1: rejection — seeds sr=1
            "2024-01-03": 1.0,  # bar 2: ample liquidity, terminal slice
        }
    )
    bar1 = sim.process_bar(_bar("2024-01-02", price=100.0))
    assert bar1.entry_fills[0].fill_kind == FillKind.REJECTED
    pending = order_book.all_pending()
    assert pending[0].twap_slices_remaining == 1

    # Bar 2: terminal slice with ample liquidity — target=remaining=
    # 2_000, filled=2_000*1.0=2_000. Slice fully clears with NO cap-clip.
    # ``cap_clipped_qty = target - filled = 0`` → must NOT mark
    # participation_clipped or accumulate into total_unfilled_qty.
    bar2 = sim.process_bar(_bar("2024-01-03", price=100.0))
    assert bar2.entry_fills[0].fill_kind == FillKind.FULL
    pos = portfolio.positions["AAA"]
    assert pos.qty == pytest.approx(2_000.0, rel=1e-9)
    assert not pos.participation_clipped, (
        "fully-filled TWAP slice must not mark participation_clipped — "
        "the only liquidity event was a clean fill at full qty_fraction"
    )
    assert pos.total_unfilled_qty == pytest.approx(0.0, abs=1e-9), (
        "scheduled TWAP deferral must not pollute total_unfilled_qty — "
        "that metric tracks actual cap-clips only"
    )


def test_invalid_twap_order_error_is_unsupported_feature_subclass() -> None:
    """Malformed ``TWAP_N`` shapes (``twap_slices`` missing, < 2, or set
    without ``TWAP_N``) raise ``InvalidTWAPOrderError`` — a subclass of
    both ``UnsupportedOrderFeatureError`` and ``ValueError`` so
    ``TradingService.run`` surfaces them as structured runtime errors
    via the existing ``except UnsupportedOrderFeatureError`` branch
    rather than letting them get silently swallowed by the broad
    ``except Exception`` malformed-order handler. Regression for a
    Codex P2 review note on PR #419.
    """
    from investment_team.trading_service.strategy.contract import (
        InvalidTWAPOrderError,
        UnsupportedOrderFeatureError,
    )

    # twap_slices < 2.
    with pytest.raises(InvalidTWAPOrderError, match="twap_slices >= 2"):
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

    # The error is also a NotImplementedError subclass via
    # UnsupportedOrderFeatureError, so the existing TradingService
    # handler that catches the latter picks it up.
    try:
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
    except UnsupportedOrderFeatureError:
        pass
    else:
        pytest.fail("InvalidTWAPOrderError must be caught by UnsupportedOrderFeatureError")

    # Existing ValueError-catching call sites still work (multi-inherit).
    with pytest.raises(ValueError):
        OrderRequest(
            client_order_id="entry-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=100,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
            twap_slices=3,  # set without TWAP_N → InvalidTWAPOrderError
        ).validate_prices()


def test_stale_partial_exit_dropped_on_untriggered_bar() -> None:
    """A partial-exit remainder whose original position has vanished
    must drop on the next bar — even if that bar is *untriggered* for
    the order. Without the early stale-continuation guard, the TWAP
    elapsed-bar tick keeps the remainder alive across no-trigger bars,
    which lets a later trigger fire the stale exit against a newly-
    opened position on the same symbol. Regression for a Codex P1
    review note on PR #419 (commit f71a882).

    Reproduction: TWAP_N=4 partial exit. Bar B partials, another order
    closes the rest. Bar C is untouched for the exit. Bar D opens a
    new entry (different ``client_order_id``). Bar E would be the next
    triggered bar for the stale exit. With the bug, bar E fires the
    stale exit against the new entry's position. With the fix, the
    stale exit is dropped on bar C and bar E sees nothing.
    """
    portfolio = Portfolio(initial_capital=10_000_000.0)
    order_book = OrderBook()
    sim = FillSimulator(
        portfolio=portfolio,
        order_book=order_book,
        risk_filter=RiskFilter(RiskLimits(max_position_pct=100, max_gross_leverage=10.0)),
        config=FillSimulatorConfig(slippage_bps=0.0, transaction_cost_bps=0.0),
        bar_safety=BarSafetyAssertion(),
        execution_model=_PerOrderTriggerModel(
            {
                "entry-1": {"2024-01-02": 1.0},
                "exit-1": {"2024-01-03": 0.5, "2024-01-06": 1.0},
                "exit-cleanup": {"2024-01-03": 1.0},
                "entry-2": {"2024-01-05": 1.0},
            }
        ),
    )

    # Bar A: open LONG 4_000.
    order_book.submit(
        _entry_order(4_000),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )
    sim.process_bar(_bar("2024-01-02", price=100.0))
    assert portfolio.positions["AAA"].qty == pytest.approx(4_000.0, rel=1e-9)

    # Submit TWAP_N=4 SHORT exit AND a separate MARKET cleanup exit.
    order_book.submit(
        _exit_order(4_000, policy=UnfilledPolicy.TWAP_N, twap_slices=4),
        submitted_at="2024-01-02",
        submitted_equity=10_000_000.0,
    )
    order_book.submit(
        OrderRequest(
            client_order_id="exit-cleanup",
            symbol="AAA",
            side=OrderSide.SHORT,
            qty=2_000,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
        ),
        submitted_at="2024-01-02",
        submitted_equity=10_000_000.0,
    )

    # Bar B: TWAP exit partials (qty_fraction=0.5 → 2_000 of 4_000),
    # cleanup closes the remaining 2_000. Position gone. TWAP remainder
    # pending with cumulative=2_000, slices_remaining=3.
    sim.process_bar(_bar("2024-01-03", price=100.0))
    assert "AAA" not in portfolio.positions
    twap_pending = [p for p in order_book.all_pending() if p.request.client_order_id == "exit-1"]
    assert len(twap_pending) == 1
    assert twap_pending[0].cumulative_filled_qty == pytest.approx(2_000.0, rel=1e-9)
    assert twap_pending[0].twap_slices_remaining == 3

    # Submit entry-2 (will trigger on 2024-01-05).
    order_book.submit(
        OrderRequest(
            client_order_id="entry-2",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=1_000,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
        ),
        submitted_at="2024-01-04",
        submitted_equity=10_000_000.0,
    )

    # Bar C: untouched for the TWAP exit AND for entry-2. Without the
    # fix, the TWAP elapsed-bar tick decrements ``slices_remaining``
    # 3→2 and keeps the order alive. With the fix, the early stale-
    # continuation guard runs first and drops it.
    sim.process_bar(_bar("2024-01-04", price=100.0))
    twap_pending = [p for p in order_book.all_pending() if p.request.client_order_id == "exit-1"]
    assert twap_pending == [], (
        "stale TWAP exit must drop on untriggered bar — otherwise it "
        "would survive to fire against a later-opened position"
    )

    # Bar D: entry-2 fires, opens a NEW LONG position.
    sim.process_bar(_bar("2024-01-05", price=100.0))
    assert portfolio.positions["AAA"].qty == pytest.approx(1_000.0, rel=1e-9)

    # Bar E: TWAP exit was scripted to trigger here. With the bug it
    # would route through ``_fill_exit`` and close the new 1_000-share
    # position. With the fix, the order was dropped on bar C, so this
    # bar produces no exit fill and the new position stays intact.
    bar_e = sim.process_bar(_bar("2024-01-06", price=100.0))
    assert bar_e.exit_fills == [], "stale TWAP exit must NOT fire against the newly-opened position"
    assert portfolio.positions["AAA"].qty == pytest.approx(1_000.0, rel=1e-9)


def test_stale_partial_exit_dropped_when_position_replaced_same_bar() -> None:
    """A partial-exit remainder must drop when its original target
    position has been *replaced* by a different one on the same symbol
    — covered by the ``working_against_entry_order_id`` mismatch path,
    not the simpler ``existing_pos is None`` guard.

    Concrete race: an entry order with a *lower* ``order_id`` (e.g. a
    STOP entry submitted before the TWAP exit) sits in the book and
    only triggers later. Between the partial fill and the trigger, the
    original position is closed externally (no intermediate
    ``process_bar`` runs in this scenario, so the existing_pos-is-None
    guard never fires). Then a single bar runs where BOTH the stop
    entry triggers AND the TWAP exit's terms come back: snapshot
    iterates in id order, stop-entry fires first → opens a brand-new
    position, TWAP exit fires next → sees ``existing_pos`` populated
    but with a *different* ``entry_order_id`` than its bound id →
    must drop via the new mismatch branch.

    Regression for a Codex P1 review note on PR #419.
    """
    portfolio = Portfolio(initial_capital=10_000_000.0)
    order_book = OrderBook()
    sim = FillSimulator(
        portfolio=portfolio,
        order_book=order_book,
        risk_filter=RiskFilter(RiskLimits(max_position_pct=100, max_gross_leverage=10.0)),
        config=FillSimulatorConfig(slippage_bps=0.0, transaction_cost_bps=0.0),
        bar_safety=BarSafetyAssertion(),
        execution_model=_PerOrderTriggerModel(
            {
                "entry-1": {"2024-01-02": 1.0},
                "exit-1": {"2024-01-03": 0.5, "2024-01-04": 1.0},
                # ``stop-entry`` is submitted FIRST (lower order_id) but
                # only triggers on bar D (after the original position is
                # gone), simulating a STOP that finally crosses.
                "stop-entry": {"2024-01-04": 1.0},
            }
        ),
    )

    # Order submission sequence dictates ``order_id`` ordering, which
    # dictates snapshot iteration on the contested bar. ``stop-entry``
    # MUST be submitted first so it processes BEFORE the TWAP exit on
    # bar D — that's what creates the new position before the stale
    # exit fires and exposes the mismatch path.
    order_book.submit(
        OrderRequest(
            client_order_id="stop-entry",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=1_000,
            order_type=OrderType.STOP,
            stop_price=110.0,
            tif=TimeInForce.GTC,
        ),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )
    # Then the entry that fills bar A.
    order_book.submit(
        _entry_order(4_000),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )
    sim.process_bar(_bar("2024-01-02", price=100.0))
    assert portfolio.positions["AAA"].qty == pytest.approx(4_000.0, rel=1e-9)

    # Submit TWAP_N=4 exit AFTER the stop-entry (so its order_id is
    # higher and it processes second on bar D's snapshot).
    order_book.submit(
        _exit_order(4_000, policy=UnfilledPolicy.TWAP_N, twap_slices=4),
        submitted_at="2024-01-02",
        submitted_equity=10_000_000.0,
    )

    # Bar B: TWAP exit partials (qty_fraction=0.5 → 2_000 of 4_000).
    # Position now 2_000. TWAP remainder bound to entry-1's position
    # via ``working_against_entry_order_id`` on its first fill.
    sim.process_bar(_bar("2024-01-03", price=100.0))
    assert portfolio.positions["AAA"].qty == pytest.approx(2_000.0, rel=1e-9)
    twap_pending = [p for p in order_book.all_pending() if p.request.client_order_id == "exit-1"]
    assert len(twap_pending) == 1
    assert twap_pending[0].cumulative_filled_qty == pytest.approx(2_000.0, rel=1e-9)

    # Simulate "another order closed the rest of the position" between
    # bars B and D — directly close the remaining 2_000 via the
    # portfolio. Crucially, do NOT call ``process_bar`` between this
    # close and bar D, otherwise the existing_pos-is-None guard would
    # fire and drop the TWAP exit before bar D's snapshot is taken.
    portfolio.partial_close("AAA", 2_000.0, 100.0, 100.0)
    portfolio.close("AAA", 100.0)
    assert "AAA" not in portfolio.positions

    # Bar D: snapshot iterates [stop-entry (low id), exit-1 (high id)].
    # stop-entry processes first → ``existing_pos is None`` and
    # cumulative=0 → ``is_entry=True`` → opens a brand-new LONG
    # position with ``entry_order_id`` != entry-1's id. Then exit-1
    # processes → ``existing_pos`` is the new position, cumulative=
    # 2_000 > 0, but ``working_against_entry_order_id`` (entry-1) !=
    # ``existing_pos.entry_order_id`` (stop-entry) → MUST drop via the
    # new mismatch branch.
    bar_d = sim.process_bar(_bar("2024-01-04", price=111.0))

    # The stop-entry's new position is intact (1_000 shares).
    assert portfolio.positions["AAA"].qty == pytest.approx(1_000.0, rel=1e-9)
    assert len(bar_d.entry_fills) == 1
    assert bar_d.entry_fills[0].client_order_id == "stop-entry"
    # Critical: no exit fill — the stale TWAP remainder must NOT have
    # fired against the brand-new position from stop-entry.
    assert bar_d.exit_fills == [], (
        "stale TWAP exit (bound to entry-1's position) must NOT fire "
        "against the new position opened by stop-entry — the mismatch "
        "in working_against_entry_order_id should drop it"
    )
    # Stale exit-1 was removed; stop-entry's post-fill cleanup also
    # removes its order. Book is empty.
    assert order_book.all_pending() == []
