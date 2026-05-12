"""Bracket / OCO materialization tests (Trading 5/5 Step 7 — issue #389).

Covers the five acceptance bullets from the issue plus a regression guard
on the no-attachment path and two follow-ups from the PR review:

1. Entry fill materializes two children with shared ``oco_group_id`` and
   ``parent_order_id`` set.
2. Take-profit fills next bar → stop-loss removed from book (OCO).
3. Stop-loss fills first → take-profit removed.
4. Parent never fills (e.g. LIMIT not crossed) → no children materialized.
5. Children not eligible for fill on the same bar as parent entry (bar-safety).
6. No-attachment regression: plain entry round-trip is unchanged.
7. ``REQUEUE_NEXT_BAR`` partial-fill terminal slice materializes brackets
   sized to the cumulative position (default backtest policy).
8. Partial bracket-exit fill requeues the surviving leg (after OCO cancel
   removes the sibling) so residual position exposure stays protected.
9. Continuation rejection of a partial-fill bracket parent (risk-gate or
   capital check) still materializes legs for the open position.
10. Bracket children are bound to the parent position at materialization,
    so a separate exit that closes the position drops the children
    instead of letting a later trigger open a new opposite-side position.
11. TWAP_N bracket parent that ages out via the no-trigger counter still
    materializes protective legs for the partially-filled position.
12. DAY-TIF bracket parent that expires after a partial fill still
    materializes protective legs for the open position.
13. Bracket children materialized at expiry time defer past the bar that
    triggered the expiry — a gap-through opening must not abort the
    backtest via ``LookAheadError``.
14. Trailing-stop attachments (``StopAttachment.trail_offset``) materialize
    as TRAILING_STOP children with ratchet state pre-seeded from the
    parent's fill price (#390).
15. Trailing-stop bracket child ratchets across bars and OCO-cancels the
    take-profit sibling on first child fill.
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
    LimitAttachment,
    OrderRequest,
    OrderSide,
    OrderType,
    StopAttachment,
    TimeInForce,
    UnfilledPolicy,
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


# ---------------------------------------------------------------------------
# REQUEUE_NEXT_BAR partial-fill entries materialize brackets at the terminal
# slice (default backtest policy — Codex P1)
# ---------------------------------------------------------------------------


def test_requeue_partial_entry_materializes_brackets_on_terminal_fill() -> None:
    """A bracket entry that's clipped by the participation cap and requeued
    must still get its protective legs once the parent terminally fills.
    Children are sized to the *cumulative* opened position, not the first
    slice alone.
    """
    sim, order_book, portfolio = _make_simulator()
    parent = order_book.submit(
        OrderRequest(
            client_order_id="entry-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=2_000.0,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
            unfilled_policy=UnfilledPolicy.REQUEUE_NEXT_BAR,
            attached_stop_loss=StopAttachment(stop_price=95.0),
            attached_take_profit=LimitAttachment(limit_price=110.0),
        ),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
        expect_brackets=True,
    )

    # Bar 2: low ADV → 50% partial fill (notional 200k vs $1M bar = 0.20
    # raw participation, capped at 0.10 → qty_fraction=0.5 → 1_000 filled).
    sim.process_bar(_bar("2024-01-02", open_price=100.0, volume=10_000.0))
    assert parent.order_id in order_book, "parent should still be pending after partial fill"
    assert order_book.children_of(parent.order_id) == [], "no brackets yet — wait for terminal fill"

    # Bar 3: high ADV → remainder fills cleanly, parent terminally completes.
    sim.process_bar(_bar("2024-01-03", open_price=100.0, volume=10_000_000.0))
    assert parent.order_id not in order_book

    children = order_book.children_of(parent.order_id)
    assert len(children) == 2
    sl = next(c for c in children if c.request.order_type == OrderType.STOP)
    tp = next(c for c in children if c.request.order_type == OrderType.LIMIT)
    # Sized to the cumulative position (1_000 + 1_000 = 2_000), not just the
    # first slice's 1_000.
    assert sl.request.qty == pytest.approx(2_000.0, rel=1e-9)
    assert tp.request.qty == pytest.approx(2_000.0, rel=1e-9)
    # Anchored to the terminal-fill bar so bar-safety blocks same-bar fills.
    assert sl.submitted_at == "2024-01-03"
    assert tp.submitted_at == "2024-01-03"
    assert sl.armed is True and tp.armed is True
    assert portfolio.positions["AAA"].qty == pytest.approx(2_000.0, rel=1e-9)


# ---------------------------------------------------------------------------
# Partial bracket-exit fill must requeue the remainder so residual position
# exposure stays protected (Codex P1 follow-up on commit 5189795)
# ---------------------------------------------------------------------------


def test_partial_bracket_exit_requeues_remainder_after_oco_cancel() -> None:
    """When the realistic execution model only partially fills a protective
    leg (low-ADV bar clipped by the participation cap), the OCO cancel
    has already removed the sibling — so the surviving leg MUST stay
    alive for the residual position. Verifies the leg is requeued via
    ``REQUEUE_NEXT_BAR`` and the residual qty closes on a follow-up bar.
    """
    sim, order_book, portfolio = _make_simulator()
    parent = order_book.submit(
        OrderRequest(
            client_order_id="entry-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=2_000.0,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
            attached_stop_loss=StopAttachment(stop_price=95.0),
            attached_take_profit=LimitAttachment(limit_price=110.0),
        ),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
        expect_brackets=True,
    )

    # Bar 2: full entry fill on a deep bar → SL / TP children materialized.
    sim.process_bar(_bar("2024-01-02", open_price=100.0, volume=10_000_000.0))
    children = order_book.children_of(parent.order_id)
    assert len(children) == 2

    # Bar 3: TP triggers (high >= 110) on a low-ADV bar. Notional 2_000 * 110
    # = 220_000 vs bar dollar volume 10_000 * 110 = 1_100_000 → raw_participation
    # = 0.20, capped at 0.10 → qty_fraction 0.5 → 1_000 fills, 1_000 remains.
    bar3 = sim.process_bar(
        _bar("2024-01-03", open_price=109.0, high=112.0, low=107.0, close=110.0, volume=10_000.0)
    )
    assert len(bar3.exit_fills) == 1
    assert bar3.exit_fills[0].fill_kind == FillKind.PARTIAL
    assert bar3.exit_fills[0].qty == pytest.approx(1_000.0, rel=1e-9)

    # OCO sibling already cancelled on the first fill.
    surviving = order_book.children_of(parent.order_id)
    assert len(surviving) == 1, "stop sibling should be cancelled by OCO"
    assert surviving[0].request.order_type == OrderType.LIMIT
    # Remainder requeued (via REQUEUE_NEXT_BAR) so the residual position
    # stays protected; submitted_at advanced to the partial-fill bar.
    assert surviving[0].remaining_qty == pytest.approx(1_000.0, rel=1e-9)
    assert surviving[0].cumulative_filled_qty == pytest.approx(1_000.0, rel=1e-9)
    assert surviving[0].submitted_at == "2024-01-03"

    # Position still half-open, no closed trade yet.
    assert portfolio.positions["AAA"].qty == pytest.approx(1_000.0, rel=1e-9)
    assert bar3.closed_trades == []

    # Bar 4: deep bar with high >= 110 → residual TP fills, position closes.
    bar4 = sim.process_bar(
        _bar(
            "2024-01-04", open_price=110.0, high=112.0, low=109.0, close=111.0, volume=10_000_000.0
        )
    )
    assert len(bar4.exit_fills) == 1
    assert bar4.exit_fills[0].qty == pytest.approx(1_000.0, rel=1e-9)
    assert len(bar4.closed_trades) == 1
    assert "AAA" not in portfolio.positions
    assert order_book.all_pending() == []


# ---------------------------------------------------------------------------
# Bracket parent abandoned mid-flight (continuation fails capital / risk
# check after a partial entry fill) must still get protective legs for the
# already-opened position (Codex P1 follow-up on commit 2b390a5)
# ---------------------------------------------------------------------------


def test_continuation_risk_gate_rejection_materializes_brackets_for_open_position() -> None:
    """A 200-share bracket that fills 100 on bar 2, then has the
    continuation rejected by the risk gate on bar 3 (post-extend notional
    exceeds ``max_symbol_concentration_pct`` of equity), must still
    materialize protective legs sized to the 100 shares actually open —
    otherwise the residual position runs unprotected."""
    portfolio = Portfolio(initial_capital=12_000.0)
    order_book = OrderBook()
    sim = FillSimulator(
        portfolio=portfolio,
        order_book=order_book,
        # ``max_symbol_concentration_pct`` is what gates ``can_enter`` per
        # symbol; raise it to 100 so the 83% first slice is admitted but the
        # 167% post-extend continuation breaches the cap.
        risk_filter=RiskFilter(
            RiskLimits(
                max_position_pct=100,
                max_gross_leverage=10.0,
                max_symbol_concentration_pct=100,
            )
        ),
        config=FillSimulatorConfig(slippage_bps=0.0, transaction_cost_bps=0.0),
        bar_safety=BarSafetyAssertion(),
        execution_model=RealisticExecutionModel(participation_cap=0.10),
    )
    parent = order_book.submit(
        OrderRequest(
            client_order_id="entry-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=200.0,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
            unfilled_policy=UnfilledPolicy.REQUEUE_NEXT_BAR,
            attached_stop_loss=StopAttachment(stop_price=95.0),
            attached_take_profit=LimitAttachment(limit_price=110.0),
        ),
        submitted_at="2024-01-01",
        submitted_equity=12_000.0,
        expect_brackets=True,
    )

    # Bar 2: low ADV (1_000 * 100 = 100_000 dollar volume; 200 * 100 = 20_000
    # notional → raw 0.20 → cap clips to 0.5 → 100 shares fill, 100 requeued).
    # First slice's 10_000 notional is 83% of 12_000 equity → under the 100%
    # ``max_symbol_concentration_pct`` cap → admitted.
    sim.process_bar(_bar("2024-01-02", open_price=100.0, volume=1_000.0))
    assert portfolio.positions["AAA"].qty == pytest.approx(100.0, rel=1e-9)
    assert parent.order_id in order_book, "parent should still be pending after partial fill"
    assert order_book.children_of(parent.order_id) == []

    # Bar 3: ample liquidity but the post-extend notional (200 * 100 = 20_000)
    # is 167% of equity (~12_000) → above the 100% ``max_symbol_concentration_pct``
    # cap → the risk gate rejects the continuation slice. The fix materializes
    # brackets sized to the 100-share open position before returning.
    sim.process_bar(_bar("2024-01-03", open_price=100.0, volume=10_000_000.0))

    assert parent.order_id not in order_book
    children = order_book.children_of(parent.order_id)
    assert len(children) == 2, "abandoned bracket parent must still spawn protective legs"
    sl = next(c for c in children if c.request.order_type == OrderType.STOP)
    tp = next(c for c in children if c.request.order_type == OrderType.LIMIT)
    assert sl.request.qty == pytest.approx(100.0, rel=1e-9)
    assert tp.request.qty == pytest.approx(100.0, rel=1e-9)
    assert sl.armed is True and tp.armed is True
    assert sl.submitted_at == "2024-01-03"
    assert tp.submitted_at == "2024-01-03"
    # Position is still open — the 100 shares from bar 2 weren't unwound.
    assert portfolio.positions["AAA"].qty == pytest.approx(100.0, rel=1e-9)


# ---------------------------------------------------------------------------
# Bracket children must NOT outlive the position they protect: if a separate
# exit closes the position before either OCO leg triggers, the children must
# be dropped rather than open a brand-new opposite-side position on a later
# stop/limit hit (Codex P1 follow-up on commit 8502876)
# ---------------------------------------------------------------------------


def test_bracket_children_dropped_when_position_closed_by_separate_exit() -> None:
    """Without binding children to the parent position at materialization,
    a manual market exit that closes the LONG position before the SL/TP
    children fire would leave the children pending; a later bar whose low
    crossed the stop level would route the SHORT STOP through
    ``_fill_entry`` (existing_pos is None → ``is_entry=True``) and open a
    fresh SHORT position. Setting ``working_against_entry_order_id`` at
    materialization plus extending the stale-continuation guard to also
    fire on cumulative_filled_qty == 0 (when bound) drops the children
    cleanly."""
    sim, order_book, portfolio = _make_simulator()
    parent = order_book.submit(
        _bracket_entry(qty=10.0, stop_price=95.0, take_profit_price=110.0),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
        expect_brackets=True,
    )

    # Bar 2: entry fills, children materialized.
    sim.process_bar(_bar("2024-01-02", open_price=100.0))
    assert portfolio.positions["AAA"].qty == pytest.approx(10.0, rel=1e-9)
    assert len(order_book.children_of(parent.order_id)) == 2

    # A manual SHORT market exit submitted between bars (simulating a
    # separate strategy decision or an external position close) — note no
    # parent_order_id / oco_group_id, so the OCO cancel block in _fill_exit
    # does NOT fire and the bracket children stay in the book.
    order_book.submit(
        OrderRequest(
            client_order_id="manual-exit",
            symbol="AAA",
            side=OrderSide.SHORT,
            qty=10.0,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
        ),
        submitted_at="2024-01-02",
        submitted_equity=10_000_000.0,
    )

    # Bar 3: manual exit fills, position closes. Bracket children stay
    # pending in the book (they have no OCO relationship with the manual
    # exit).
    sim.process_bar(_bar("2024-01-03", open_price=100.0))
    assert "AAA" not in portfolio.positions
    survivors_before = order_book.children_of(parent.order_id)
    assert len(survivors_before) == 2, (
        "manual exit should not cancel bracket children — only the OCO "
        "relationship between siblings does"
    )

    # Bar 4: bar's low (93) crosses the SL stop_price (95). WITHOUT THE FIX
    # the SHORT STOP would route through _fill_entry (no position open) and
    # open a fresh SHORT position. WITH THE FIX the stale-continuation
    # guard drops both children and emits no fills.
    bar4 = sim.process_bar(_bar("2024-01-04", open_price=97.0, high=98.0, low=93.0, close=94.0))
    assert bar4.entry_fills == [], "stale bracket child must not open a new position"
    assert bar4.exit_fills == []
    assert bar4.closed_trades == []
    assert "AAA" not in portfolio.positions
    assert order_book.children_of(parent.order_id) == []


# ---------------------------------------------------------------------------
# TWAP_N bracket parent that ages out via the no-trigger counter must still
# materialize protective legs for the partially-filled position (Codex P2
# follow-up on commit 9b7888b)
# ---------------------------------------------------------------------------


def test_twap_age_out_bracket_parent_materializes_brackets_for_open_position() -> None:
    """A bracketed TWAP_N LIMIT entry that fills its first slice (seeding
    ``twap_slices_remaining``) and then has its remaining slices all expire
    on no-trigger bars must still materialize protective legs sized to the
    open position. The ``process_bar`` TWAP-tick branch removes the parent
    with ``was_filled=True`` outside ``_handle_entry_remainder`` /
    ``_continue_entry``, so the post-handler materializer never runs
    without an explicit hook in that branch."""
    sim, order_book, portfolio = _make_simulator()
    parent = order_book.submit(
        OrderRequest(
            client_order_id="entry-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=200.0,
            order_type=OrderType.LIMIT,
            limit_price=100.0,
            tif=TimeInForce.DAY,
            unfilled_policy=UnfilledPolicy.TWAP_N,
            twap_slices=2,
            attached_stop_loss=StopAttachment(stop_price=95.0),
            attached_take_profit=LimitAttachment(limit_price=110.0),
        ),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
        expect_brackets=True,
    )

    # Bar 2: low (98) ≤ limit_price (100) → trigger fires → first slice
    # partially fills against a low-ADV bar (200 * 100 = 20_000 notional vs
    # 1_000 * 100 = 100_000 dollar volume → raw 0.20 → cap clips to 0.5 →
    # 100 fills, 100 requeued; ``twap_slices_remaining`` seeded to 1).
    sim.process_bar(
        _bar("2024-01-02", open_price=99.0, high=101.0, low=98.0, close=100.0, volume=1_000.0)
    )
    assert portfolio.positions["AAA"].qty == pytest.approx(100.0, rel=1e-9)
    assert parent.order_id in order_book
    assert order_book.children_of(parent.order_id) == []

    # Bar 3: bar.low (104) > limit_price (100) for LONG LIMIT → no trigger
    # → ``compute_fill_terms`` returns None → process_bar's TWAP_N tick
    # decrements ``twap_slices_remaining`` from 1 to 0 → parent removed
    # with ``was_filled=True``. The fix materializes brackets sized to the
    # 100-share open position before the loop continues.
    sim.process_bar(_bar("2024-01-03", open_price=105.0, high=107.0, low=104.0, close=106.0))

    assert parent.order_id not in order_book
    children = order_book.children_of(parent.order_id)
    assert len(children) == 2, "TWAP-aged-out bracket parent must still spawn protective legs"
    sl = next(c for c in children if c.request.order_type == OrderType.STOP)
    tp = next(c for c in children if c.request.order_type == OrderType.LIMIT)
    assert sl.request.qty == pytest.approx(100.0, rel=1e-9)
    assert tp.request.qty == pytest.approx(100.0, rel=1e-9)
    assert sl.armed is True and tp.armed is True
    assert sl.submitted_at == "2024-01-03"
    assert tp.submitted_at == "2024-01-03"
    assert portfolio.positions["AAA"].qty == pytest.approx(100.0, rel=1e-9)


# ---------------------------------------------------------------------------
# DAY-TIF bracket parent that expires after a partial fill must still
# materialize protective legs for the open position (Codex P1 follow-up on
# commit 90b5753)
# ---------------------------------------------------------------------------


def test_day_expiry_partial_bracket_parent_materializes_brackets() -> None:
    """A DAY-TIF bracket entry that fills its first slice on day D and is
    requeued must still get protective legs when the parent expires on
    day D+1's session boundary. The expiry routes through
    ``FillSimulator.expire_day_orders``, which calls
    ``_maybe_materialize_brackets_on_abandon`` for partial bracket
    parents — the order_book uses ``was_filled=True`` for those so the
    eligible-parent registration survives long enough for
    ``submit_attached`` to succeed."""
    sim, order_book, portfolio = _make_simulator()
    parent = order_book.submit(
        OrderRequest(
            client_order_id="entry-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=200.0,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
            unfilled_policy=UnfilledPolicy.REQUEUE_NEXT_BAR,
            attached_stop_loss=StopAttachment(stop_price=95.0),
            attached_take_profit=LimitAttachment(limit_price=110.0),
        ),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
        expect_brackets=True,
    )

    # Bar on day D (the day after submission): low ADV → 50% partial fill
    # (100 shares), remainder requeued with ``submitted_at`` advanced to
    # the fill bar; ``original_submitted_at`` stays at "2024-01-01".
    sim.process_bar(_bar("2024-01-02", open_price=100.0, volume=1_000.0))
    assert portfolio.positions["AAA"].qty == pytest.approx(100.0, rel=1e-9)
    assert parent.order_id in order_book
    assert order_book.children_of(parent.order_id) == []

    # Day boundary: simulate the date-change expiry hook the service
    # invokes before processing day D+1's first bar.
    next_session_bar = _bar("2024-01-03", open_price=100.0)
    expired = sim.expire_day_orders(next_session_bar)

    assert len(expired) == 1
    assert expired[0].order_id == parent.order_id
    assert parent.order_id not in order_book

    # Brackets materialized for the still-open 100-share position.
    children = order_book.children_of(parent.order_id)
    assert len(children) == 2, "expired partial bracket parent must spawn protective legs"
    sl = next(c for c in children if c.request.order_type == OrderType.STOP)
    tp = next(c for c in children if c.request.order_type == OrderType.LIMIT)
    assert sl.request.qty == pytest.approx(100.0, rel=1e-9)
    assert tp.request.qty == pytest.approx(100.0, rel=1e-9)
    assert sl.armed is True and tp.armed is True
    assert sl.submitted_at == "2024-01-03"
    assert tp.submitted_at == "2024-01-03"
    # Position untouched by the expiry; the bracket now covers it.
    assert portfolio.positions["AAA"].qty == pytest.approx(100.0, rel=1e-9)


# ---------------------------------------------------------------------------
# Expiry-created bracket children defer past the bar that triggered the
# expiry — a gap-through opening on that same bar must not abort the run
# via LookAheadError (Codex P1 follow-up on commit f5d535e)
# ---------------------------------------------------------------------------


def test_expiry_bar_gap_through_does_not_abort_via_lookahead() -> None:
    """``FillSimulator.expire_day_orders(cur_bar)`` materializes bracket
    children with ``submitted_at=cur_bar.timestamp`` BEFORE the service
    calls ``process_bar(cur_bar)``, so the children land in this bar's
    pending snapshot. If that bar's range crosses a child's stop or
    limit price, the engine-internal soft-skip in ``process_bar``
    must defer the child to the next bar instead of triggering
    ``bar_safety.check_fill``'s ``LookAheadError``. Without the skip,
    a normal overnight gap would abort the backtest."""
    sim, order_book, portfolio = _make_simulator()
    parent = order_book.submit(
        OrderRequest(
            client_order_id="entry-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=200.0,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
            unfilled_policy=UnfilledPolicy.REQUEUE_NEXT_BAR,
            attached_stop_loss=StopAttachment(stop_price=95.0),
            attached_take_profit=LimitAttachment(limit_price=110.0),
        ),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
        expect_brackets=True,
    )

    # Day D (2024-01-02): partial fill (100 shares), remainder requeued.
    sim.process_bar(_bar("2024-01-02", open_price=100.0, volume=1_000.0))
    assert portfolio.positions["AAA"].qty == pytest.approx(100.0, rel=1e-9)

    # Day D+1 (2024-01-03) opens with a gap-down through the stop level
    # (low=92 < stop_price=95). The service calls expire_day_orders FIRST
    # (with cur_bar=2024-01-03), then process_bar(cur_bar). Without the
    # soft-skip the SHORT STOP child — submitted at 2024-01-03 — would
    # trip ``bar_safety.check_fill`` and raise ``LookAheadError``.
    next_session_bar = _bar("2024-01-03", open_price=93.0, high=94.0, low=92.0, close=93.0)
    sim.expire_day_orders(next_session_bar)

    # Children materialized on the expiry bar.
    children_after_expire = order_book.children_of(parent.order_id)
    assert len(children_after_expire) == 2
    for child in children_after_expire:
        assert child.submitted_at == "2024-01-03"

    # process_bar on the same bar must NOT raise LookAheadError despite the
    # gap-through — the soft-skip defers the children to the next bar.
    outcome = sim.process_bar(next_session_bar)
    assert outcome.entry_fills == []
    assert outcome.exit_fills == []
    assert outcome.closed_trades == []

    # Children still pending and bound, ready to fire on the next bar.
    children_after_process = order_book.children_of(parent.order_id)
    assert len(children_after_process) == 2

    # Day D+2 (2024-01-04): another bar with low ≤ stop. Now bar.timestamp
    # > children's submitted_at → soft-skip doesn't fire → SHORT STOP
    # fires as a normal exit, OCO cancels the take-profit.
    bar_after = _bar("2024-01-04", open_price=94.0, high=95.0, low=93.0, close=94.0)
    final_outcome = sim.process_bar(bar_after)
    assert len(final_outcome.exit_fills) == 1
    assert final_outcome.exit_fills[0].fill_kind == FillKind.FULL
    assert "AAA" not in portfolio.positions
    assert order_book.children_of(parent.order_id) == []


# ---------------------------------------------------------------------------
# Trailing-stop attachments are runtime-supported as of #390
# ---------------------------------------------------------------------------


def test_trailing_stop_attachment_validates_and_materializes_as_trailing_child() -> None:
    """``StopAttachment(trail_offset=...)`` materializes as a TRAILING_STOP
    child with the ratchet pre-seeded from the parent's fill price (#390)."""
    # Both abs and bps variants now validate at submission time.
    OrderRequest(
        client_order_id="entry-1",
        symbol="AAA",
        side=OrderSide.LONG,
        qty=10.0,
        order_type=OrderType.MARKET,
        attached_stop_loss=StopAttachment(stop_price=95.0, trail_offset=2.0),
    ).validate_prices()
    OrderRequest(
        client_order_id="entry-1",
        symbol="AAA",
        side=OrderSide.LONG,
        qty=10.0,
        order_type=OrderType.MARKET,
        attached_stop_loss=StopAttachment(
            stop_price=95.0, trail_offset=20.0, trail_offset_kind="bps"
        ),
    ).validate_prices()

    # Plain fixed-stop attachment (no trail_offset) is unaffected.
    OrderRequest(
        client_order_id="entry-1",
        symbol="AAA",
        side=OrderSide.LONG,
        qty=10.0,
        order_type=OrderType.MARKET,
        attached_stop_loss=StopAttachment(stop_price=95.0),
    ).validate_prices()

    # End-to-end: entry fill materializes a TRAILING_STOP child whose
    # ``trailing_water`` / ``effective_stop_price`` are pre-seeded from
    # the parent's fill price (so the first eligible bar trails from
    # the entry, not from that bar's high).
    sim, order_book, _portfolio = _make_simulator()
    parent = order_book.submit(
        OrderRequest(
            client_order_id="entry-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=10.0,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
            attached_stop_loss=StopAttachment(stop_price=95.0, trail_offset=2.0),
            attached_take_profit=LimitAttachment(limit_price=110.0),
        ),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
        expect_brackets=True,
    )
    sim.process_bar(_bar("2024-01-02", open_price=100.0))

    children = order_book.children_of(parent.order_id)
    sl = next(c for c in children if c.request.order_type == OrderType.TRAILING_STOP)
    assert sl.request.trail_offset == pytest.approx(2.0, rel=1e-9)
    assert sl.request.trail_offset_kind == "abs"
    assert sl.trailing_water == pytest.approx(100.0, rel=1e-9)
    assert sl.effective_stop_price == pytest.approx(98.0, rel=1e-9)


# ---------------------------------------------------------------------------
# 15. Trailing-stop bracket child ratchets across bars; TP fill cancels SL via OCO
# ---------------------------------------------------------------------------


def test_trailing_stop_bracket_child_ratchets_and_oco_cancels_on_tp_fill() -> None:
    """Trailing-stop bracket child must (a) ratchet ``trailing_water`` and
    ``effective_stop_price`` favorably across bars, (b) leave them
    untouched on an adverse bar, and (c) be removed via the existing OCO
    cancellation when its take-profit sibling fills first."""
    sim, order_book, portfolio = _make_simulator()
    parent = order_book.submit(
        OrderRequest(
            client_order_id="entry-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=10.0,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
            attached_stop_loss=StopAttachment(stop_price=95.0, trail_offset=2.0),
            attached_take_profit=LimitAttachment(limit_price=110.0),
        ),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
        expect_brackets=True,
    )

    # Bar 1: entry fills at 100 → trailing child pre-seeded to (100, 98).
    sim.process_bar(_bar("2024-01-02", open_price=100.0))
    children = order_book.children_of(parent.order_id)
    sl = next(c for c in children if c.request.order_type == OrderType.TRAILING_STOP)
    tp = next(c for c in children if c.request.order_type == OrderType.LIMIT)
    assert sl.trailing_water == pytest.approx(100.0, rel=1e-9)
    assert sl.effective_stop_price == pytest.approx(98.0, rel=1e-9)

    # Bar 2: favorable move (high 108, low 107 — above the new eff=106 so SL
    # doesn't fire) → ratchet up.
    sim.process_bar(_bar("2024-01-03", open_price=105.0, high=108.0, low=107.0, close=107.5))
    assert sl.trailing_water == pytest.approx(108.0, rel=1e-9)
    assert sl.effective_stop_price == pytest.approx(106.0, rel=1e-9)

    # Bar 3: adverse retrace (high 107.5, low 106.5 — still above eff=106) →
    # no change to ratchet.
    sim.process_bar(_bar("2024-01-04", open_price=107.0, high=107.5, low=106.5, close=106.8))
    assert sl.trailing_water == pytest.approx(108.0, rel=1e-9)
    assert sl.effective_stop_price == pytest.approx(106.0, rel=1e-9)

    # Bar 4: high 112 crosses TP limit 110; low 111 stays above SL eff_stop
    # (which ratchets to 112-2=110 on this bar). TP fills first; OCO cancels
    # the trailing SL sibling.
    outcome = sim.process_bar(
        _bar("2024-01-05", open_price=109.0, high=112.0, low=111.0, close=111.5)
    )
    assert len(outcome.exit_fills) == 1
    assert outcome.exit_fills[0].price == pytest.approx(110.0, rel=1e-9)
    assert outcome.exit_fills[0].order_id == tp.order_id
    assert "AAA" not in portfolio.positions
    assert order_book.children_of(parent.order_id) == []
    assert order_book.all_pending() == []
