"""Trailing-stop runtime tests (Trading 5/5 Step 8 — issue #390).

Covers the six new test cases for the standalone ``TRAILING_STOP`` order
type and the ratchet mechanics. Bracket-child trailing stops are tested
separately in ``test_bracket_orders.py``.

Triggers follow the existing STOP geometry once the simulator's
effective-request shim swaps ``po.effective_stop_price`` in for
``req.stop_price``:

* A SHORT stop closing a LONG position triggers when ``bar.low <= eff_stop``
  and fills at ``min(bar.open, eff_stop)``.
* A LONG stop closing a SHORT position triggers when ``bar.high >= eff_stop``
  and fills at ``max(bar.open, eff_stop)``.
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


def _open_long(
    sim: FillSimulator,
    order_book: OrderBook,
    *,
    qty: float = 10.0,
    entry_bar_ts: str = "2024-01-02",
    entry_open: float = 100.0,
) -> str:
    """Open a LONG position via a MARKET entry order that fills on
    ``entry_bar_ts``. Returns the position's entry ``order_id``."""
    order_book.submit(
        OrderRequest(
            client_order_id="entry-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=qty,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
        ),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )
    sim.process_bar(_bar(entry_bar_ts, open_price=entry_open, high=entry_open, low=entry_open))
    return "entry-1"


def _open_short(
    sim: FillSimulator,
    order_book: OrderBook,
    *,
    qty: float = 10.0,
    entry_bar_ts: str = "2024-01-02",
    entry_open: float = 100.0,
) -> str:
    order_book.submit(
        OrderRequest(
            client_order_id="entry-1",
            symbol="AAA",
            side=OrderSide.SHORT,
            qty=qty,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
        ),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )
    sim.process_bar(_bar(entry_bar_ts, open_price=entry_open, high=entry_open, low=entry_open))
    return "entry-1"


def _trailing_stop_po(order_book: OrderBook, client_order_id: str):
    """Find a pending trailing-stop order by client_order_id."""
    for po in order_book.all_pending():
        if po.request.client_order_id == client_order_id:
            return po
    raise AssertionError(f"trailing stop {client_order_id!r} not found in book")


# ---------------------------------------------------------------------------
# 1. LONG trailing stop ratchets then fills (abs offset)
# ---------------------------------------------------------------------------


def test_long_trailing_stop_ratchets_then_fills() -> None:
    """LONG position closed by SHORT TRAILING_STOP, abs offset=5.

    Bars present highs of 100 → 110 → 107. Eff stop ratchets:
    bar 1 → 95 (no trigger), bar 2 → 105 (no trigger), bar 3 stays 105
    (water=110 sticky), and bar 3 low=104 ≤ 105 triggers the stop at 105.
    """
    sim, order_book, portfolio = _make_simulator()
    _open_long(sim, order_book, qty=10.0, entry_bar_ts="2024-01-02", entry_open=100.0)
    assert "AAA" in portfolio.positions

    order_book.submit(
        OrderRequest(
            client_order_id="trail-1",
            symbol="AAA",
            side=OrderSide.SHORT,
            qty=10.0,
            order_type=OrderType.TRAILING_STOP,
            stop_price=95.0,  # initial water seed
            trail_offset=5.0,
            trail_offset_kind="abs",
            tif=TimeInForce.GTC,
        ),
        submitted_at="2024-01-02",
        submitted_equity=10_000_000.0,
    )

    # Bar 2: high=100, low=98 — water=100 (max(95, 100)), eff=95. low > 95.
    sim.process_bar(_bar("2024-01-03", open_price=100.0, high=100.0, low=98.0, close=99.5))
    sl = _trailing_stop_po(order_book, "trail-1")
    assert sl.trailing_water == pytest.approx(100.0, rel=1e-9)
    assert sl.effective_stop_price == pytest.approx(95.0, rel=1e-9)

    # Bar 3: favorable — high=110, low=106. water=110, eff=105. low > 105.
    sim.process_bar(_bar("2024-01-04", open_price=101.0, high=110.0, low=106.0, close=109.0))
    sl = _trailing_stop_po(order_book, "trail-1")
    assert sl.trailing_water == pytest.approx(110.0, rel=1e-9)
    assert sl.effective_stop_price == pytest.approx(105.0, rel=1e-9)

    # Bar 4: retrace — high=107, low=104. water=110 stays, eff=105. low ≤ 105: trigger.
    outcome = sim.process_bar(
        _bar("2024-01-05", open_price=107.0, high=107.0, low=104.0, close=105.0)
    )
    assert len(outcome.exit_fills) == 1
    assert outcome.exit_fills[0].price == pytest.approx(105.0, rel=1e-9)
    assert "AAA" not in portfolio.positions


# ---------------------------------------------------------------------------
# 2. Retrace fixture — water=120 sticks, fills at 115 (not 95)
# ---------------------------------------------------------------------------


def test_long_trailing_stop_holds_through_retrace() -> None:
    """Confirms the ratchet only moves favorably. After a peak at 120 the
    eff_stop locks at 115 and survives a retrace; it must NOT relax back
    toward the initial 95 just because the price came back down."""
    sim, order_book, portfolio = _make_simulator()
    _open_long(sim, order_book, qty=10.0, entry_bar_ts="2024-01-02", entry_open=100.0)

    order_book.submit(
        OrderRequest(
            client_order_id="trail-1",
            symbol="AAA",
            side=OrderSide.SHORT,
            qty=10.0,
            order_type=OrderType.TRAILING_STOP,
            stop_price=95.0,
            trail_offset=5.0,
            tif=TimeInForce.GTC,
        ),
        submitted_at="2024-01-02",
        submitted_equity=10_000_000.0,
    )

    # Bar 2: high=100. water=100, eff=95.
    sim.process_bar(_bar("2024-01-03", open_price=100.0, high=100.0, low=99.0, close=100.0))
    # Bar 3: peak — high=120, low=118. water=120, eff=115. low > 115.
    sim.process_bar(_bar("2024-01-04", open_price=110.0, high=120.0, low=118.0, close=119.0))
    sl = _trailing_stop_po(order_book, "trail-1")
    assert sl.trailing_water == pytest.approx(120.0, rel=1e-9)
    assert sl.effective_stop_price == pytest.approx(115.0, rel=1e-9)

    # Bar 4: retrace — high=117 (below previous water), low=116. water STAYS at 120,
    # eff STAYS at 115 (this is the ratchet-only-favorable invariant).
    sim.process_bar(_bar("2024-01-05", open_price=117.0, high=117.0, low=116.0, close=116.5))
    sl = _trailing_stop_po(order_book, "trail-1")
    assert sl.trailing_water == pytest.approx(120.0, rel=1e-9)
    assert sl.effective_stop_price == pytest.approx(115.0, rel=1e-9)

    # Bar 5: low=114 ≤ 115 → trigger. ref = min(open=116, 115) = 115. NOT 95.
    outcome = sim.process_bar(
        _bar("2024-01-06", open_price=116.0, high=116.0, low=114.0, close=115.0)
    )
    assert len(outcome.exit_fills) == 1
    assert outcome.exit_fills[0].price == pytest.approx(115.0, rel=1e-9)
    assert "AAA" not in portfolio.positions


# ---------------------------------------------------------------------------
# 3. SHORT trailing stop ratchets DOWN only
# ---------------------------------------------------------------------------


def test_short_trailing_stop_ratchets_down_only() -> None:
    """Mirror of test 1 on the short side. SHORT position closed by LONG
    TRAILING_STOP — water tracks bar.low and eff = water + offset.
    Ratchet only moves down (favorable for a short)."""
    sim, order_book, portfolio = _make_simulator()
    _open_short(sim, order_book, qty=10.0, entry_bar_ts="2024-01-02", entry_open=100.0)

    order_book.submit(
        OrderRequest(
            client_order_id="trail-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=10.0,
            order_type=OrderType.TRAILING_STOP,
            stop_price=105.0,  # initial water seed for SHORT (above current price)
            trail_offset=5.0,
            tif=TimeInForce.GTC,
        ),
        submitted_at="2024-01-02",
        submitted_equity=10_000_000.0,
    )

    # Bar 2: high=101, low=100. water = min(105, 100) = 100, eff=105. high=101 < 105.
    sim.process_bar(_bar("2024-01-03", open_price=100.0, high=101.0, low=100.0, close=100.0))
    sl = _trailing_stop_po(order_book, "trail-1")
    assert sl.trailing_water == pytest.approx(100.0, rel=1e-9)
    assert sl.effective_stop_price == pytest.approx(105.0, rel=1e-9)

    # Bar 3: favorable — high=92, low=90. water=90, eff=95. high=92 < 95.
    sim.process_bar(_bar("2024-01-04", open_price=99.0, high=92.0, low=90.0, close=91.0))
    sl = _trailing_stop_po(order_book, "trail-1")
    assert sl.trailing_water == pytest.approx(90.0, rel=1e-9)
    assert sl.effective_stop_price == pytest.approx(95.0, rel=1e-9)

    # Bar 4: adverse retrace — high=94, low=93. water STAYS 90, eff STAYS 95.
    sim.process_bar(_bar("2024-01-05", open_price=92.0, high=94.0, low=93.0, close=93.5))
    sl = _trailing_stop_po(order_book, "trail-1")
    assert sl.trailing_water == pytest.approx(90.0, rel=1e-9)
    assert sl.effective_stop_price == pytest.approx(95.0, rel=1e-9)

    # Bar 5: high=95 ≥ 95 → trigger. ref = max(open=94, 95) = 95.
    outcome = sim.process_bar(_bar("2024-01-06", open_price=94.0, high=95.0, low=93.0, close=94.5))
    assert len(outcome.exit_fills) == 1
    assert outcome.exit_fills[0].price == pytest.approx(95.0, rel=1e-9)
    assert "AAA" not in portfolio.positions


# ---------------------------------------------------------------------------
# 4. Zero offset degenerates to break-even after favorable bar
# ---------------------------------------------------------------------------


def test_zero_offset_degenerates_to_breakeven_after_favorable_bar() -> None:
    """``trail_offset=0`` means the eff_stop equals the running high — the
    instant price retraces from the high (or even closes at it), the stop
    fires. On a flat bar that opens at the previous high, this fills at
    the bar's open (the "break-even" interpretation)."""
    sim, order_book, portfolio = _make_simulator()
    _open_long(sim, order_book, qty=10.0, entry_bar_ts="2024-01-02", entry_open=100.0)

    order_book.submit(
        OrderRequest(
            client_order_id="trail-1",
            symbol="AAA",
            side=OrderSide.SHORT,
            qty=10.0,
            order_type=OrderType.TRAILING_STOP,
            stop_price=100.0,
            trail_offset=0.0,
            tif=TimeInForce.GTC,
        ),
        submitted_at="2024-01-02",
        submitted_equity=10_000_000.0,
    )

    # Degenerate flat bar at 100: water=100, eff=100. low=100 ≤ 100 → triggers
    # at min(open=100, 100) = 100. Position closes at break-even with entry.
    outcome = sim.process_bar(
        _bar("2024-01-03", open_price=100.0, high=100.0, low=100.0, close=100.0)
    )
    assert len(outcome.exit_fills) == 1
    assert outcome.exit_fills[0].price == pytest.approx(100.0, rel=1e-9)
    assert "AAA" not in portfolio.positions
    # Net P&L is zero — break-even round-trip.
    assert portfolio.cumulative_pnl == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# 5. Standalone TRAILING_STOP uses stop_price as initial water seed
# ---------------------------------------------------------------------------


def test_standalone_trailing_stop_uses_stop_price_as_initial_water() -> None:
    """If a bar's high is BELOW the strategy's ``stop_price`` on first
    observation, the water mark must stay at ``stop_price`` (the explicit
    seed) rather than collapse to the bar's high. Confirms the
    ``trailing_water or req.stop_price or bar.high`` fallback chain in
    ``_update_trailing``."""
    sim, order_book, _portfolio = _make_simulator()
    _open_long(sim, order_book, qty=10.0, entry_bar_ts="2024-01-02", entry_open=100.0)

    order_book.submit(
        OrderRequest(
            client_order_id="trail-1",
            symbol="AAA",
            side=OrderSide.SHORT,
            qty=10.0,
            order_type=OrderType.TRAILING_STOP,
            stop_price=100.0,  # explicit initial water
            trail_offset=5.0,
            tif=TimeInForce.GTC,
        ),
        submitted_at="2024-01-02",
        submitted_equity=10_000_000.0,
    )

    # Bar 2: high=98 (below the 100 seed). water = max(100, 98) = 100 — NOT 98.
    sim.process_bar(_bar("2024-01-03", open_price=98.0, high=98.0, low=97.0, close=97.5))
    sl = _trailing_stop_po(order_book, "trail-1")
    assert sl.trailing_water == pytest.approx(100.0, rel=1e-9)
    assert sl.effective_stop_price == pytest.approx(95.0, rel=1e-9)


# ---------------------------------------------------------------------------
# 6. bps trail_offset variant
# ---------------------------------------------------------------------------


def test_bps_trail_offset_variant() -> None:
    """``trail_offset_kind="bps"`` computes the offset as a percentage of
    the current water (200 bps = 2%). Eff_stop drifts with the water mark
    rather than being a fixed dollar distance."""
    sim, order_book, portfolio = _make_simulator()
    _open_long(sim, order_book, qty=10.0, entry_bar_ts="2024-01-02", entry_open=100.0)

    order_book.submit(
        OrderRequest(
            client_order_id="trail-1",
            symbol="AAA",
            side=OrderSide.SHORT,
            qty=10.0,
            order_type=OrderType.TRAILING_STOP,
            stop_price=95.0,
            trail_offset=200.0,
            trail_offset_kind="bps",
            tif=TimeInForce.GTC,
        ),
        submitted_at="2024-01-02",
        submitted_equity=10_000_000.0,
    )

    # Bar 2: high=100. water=100, eff = 100 * 0.98 = 98. low > 98.
    sim.process_bar(_bar("2024-01-03", open_price=100.0, high=100.0, low=99.0, close=100.0))
    sl = _trailing_stop_po(order_book, "trail-1")
    assert sl.trailing_water == pytest.approx(100.0, rel=1e-9)
    assert sl.effective_stop_price == pytest.approx(98.0, rel=1e-9)

    # Bar 3: favorable — high=120. water=120, eff = 120 * 0.98 = 117.6.
    sim.process_bar(_bar("2024-01-04", open_price=110.0, high=120.0, low=118.0, close=119.0))
    sl = _trailing_stop_po(order_book, "trail-1")
    assert sl.trailing_water == pytest.approx(120.0, rel=1e-9)
    assert sl.effective_stop_price == pytest.approx(117.6, rel=1e-9)

    # Bar 4: low=117 ≤ 117.6 → trigger. ref = min(open=119, 117.6) = 117.6.
    outcome = sim.process_bar(
        _bar("2024-01-05", open_price=119.0, high=119.0, low=117.0, close=117.5)
    )
    assert len(outcome.exit_fills) == 1
    assert outcome.exit_fills[0].price == pytest.approx(117.6, rel=1e-9)
    assert "AAA" not in portfolio.positions
