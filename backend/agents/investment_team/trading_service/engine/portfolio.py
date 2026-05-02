"""Portfolio state: capital, positions, mark-to-market equity, drawdown tracking.

Extracted from the equivalent inline state in
``TradeSimulationEngine._run_lookahead_safe``
(``backend/agents/investment_team/trade_simulator.py:466+``) so the new
streaming engine and the legacy engine remain byte-parity during PR 1's
parity-test window (legacy is removed in PR 3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..strategy.contract import Fill, OrderSide
from .order_book import FILL_QTY_REL_TOL


@dataclass
class Position:
    """An open position tracked by the engine."""

    symbol: str
    side: OrderSide
    qty: float
    entry_price: float  # post-slippage fill price
    entry_bid_price: float  # raw reference close / open before slippage
    entry_timestamp: str
    entry_order_id: str
    entry_client_order_id: str
    entry_order_type: str = "market"

    # Partial-fill accounting (#386). ``original_qty`` is the qty the strategy
    # asked for; ``qty`` is the *currently open* qty (decreases on partial
    # exits). The cumulative-exit fields drive the weighted-average exit price
    # used when the final ``TradeRecord`` is emitted.
    original_qty: float = 0.0
    cumulative_exit_qty: float = 0.0
    weighted_exit_price_sum: float = 0.0
    # Weighted exit *bid* price (the pre-slippage reference price used on
    # each exit slice). Mirrors ``weighted_exit_price_sum`` so multi-bar
    # partial exits can report a coherent weighted ``exit_bid_price`` on
    # the final TradeRecord — without this, ``exit_price`` (weighted) and
    # ``exit_bid_price`` (only the closing bar) get mismatched and skew
    # slippage diagnostics.
    weighted_exit_bid_sum: float = 0.0
    partial_fill_count: int = 0
    participation_clipped: bool = False
    total_unfilled_qty: float = 0.0

    @property
    def position_value(self) -> float:
        return self.entry_price * self.qty

    def reduce(self, qty: float, exit_price: float, exit_bid_price: float = 0.0) -> None:
        """Apply a partial exit: shrink ``qty`` and accumulate weighted exit.

        ``partial_fill_count`` is intentionally not bumped here — it counts
        the number of fills the *entry* side required (initial + each
        ``REQUEUE_NEXT_BAR`` continuation). Exit-side slice count is
        captured implicitly via ``cumulative_exit_qty`` /
        ``weighted_exit_price_sum``.

        ``exit_bid_price`` defaults to ``0.0`` for legacy / direct callers
        that don't track a reference price; the fill simulator always
        passes the bar's pre-slippage reference price so multi-bar
        partial exits accumulate a true weighted bid average.
        """
        self.cumulative_exit_qty += qty
        self.weighted_exit_price_sum += qty * exit_price
        self.weighted_exit_bid_sum += qty * exit_bid_price
        self.qty -= qty

    @property
    def is_closed(self) -> bool:
        # FP-safe terminal check: a sub-ULP residual on ``cumulative_exit_qty``
        # vs ``original_qty`` shouldn't keep a position open. Reuse the same
        # relative tolerance ``OrderBook.requeue`` uses for remainder clamping
        # so the two terminal-fill checks agree.
        if self.original_qty <= 0:
            return False
        return self.cumulative_exit_qty + self.original_qty * FILL_QTY_REL_TOL >= self.original_qty

    @property
    def weighted_avg_exit_price(self) -> float:
        if self.cumulative_exit_qty <= 0:
            return 0.0
        return self.weighted_exit_price_sum / self.cumulative_exit_qty

    @property
    def weighted_avg_exit_bid_price(self) -> float:
        if self.cumulative_exit_qty <= 0:
            return 0.0
        return self.weighted_exit_bid_sum / self.cumulative_exit_qty


@dataclass
class Portfolio:
    """Tracks capital, open positions, cumulative P&L, and equity peak."""

    initial_capital: float
    capital: float = 0.0
    peak_equity: float = 0.0
    cumulative_pnl: float = 0.0
    positions: Dict[str, Position] = field(default_factory=dict)
    last_price: Dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.capital == 0.0:
            self.capital = self.initial_capital
        if self.peak_equity == 0.0:
            self.peak_equity = self.initial_capital

    # ------------------------------------------------------------------
    # Position lifecycle
    # ------------------------------------------------------------------

    def open(self, position: Position) -> None:
        if position.symbol in self.positions:
            raise ValueError(f"position already open for {position.symbol}")
        # Pin ``original_qty`` to the first-fill qty when the caller didn't set
        # it. The fill simulator builds Position objects with the strategy's
        # requested qty in ``original_qty`` and the actually-filled qty in
        # ``qty``; this fallback keeps direct callers (tests, legacy paths)
        # working without forcing them to set both fields.
        if position.original_qty == 0.0:
            position.original_qty = position.qty
        self.capital -= position.position_value
        self.positions[position.symbol] = position

    def extend(
        self,
        symbol: str,
        additional_qty: float,
        fill_price: float,
        fill_bid_price: float | None = None,
    ) -> Position:
        """Apply a follow-on entry fill to an already-open position.

        Used by the partial-fill simulator (#386) when a requeued partial
        entry's remainder fills on a later bar. ``entry_price`` becomes the
        qty-weighted average across all entry partials so downstream P&L
        math (``position_value``, gross/net in TradeRecord) stays correct.

        ``fill_bid_price`` is the pre-slippage reference price for this
        slice. When provided, ``entry_bid_price`` is also updated to a
        qty-weighted average so it stays coherent with the weighted
        ``entry_price`` (otherwise the trade record would mix weighted
        fill pricing with a stale first-slice reference, skewing entry-
        side slippage diagnostics). Defaults to ``None`` for legacy
        callers; the fill simulator always passes the bar's reference.
        """
        pos = self.positions[symbol]
        old_notional = pos.entry_price * pos.qty
        new_qty = pos.qty + additional_qty
        pos.entry_price = (old_notional + additional_qty * fill_price) / new_qty
        if fill_bid_price is not None:
            old_bid_notional = pos.entry_bid_price * pos.qty
            pos.entry_bid_price = (old_bid_notional + additional_qty * fill_bid_price) / new_qty
        pos.qty = new_qty
        # Don't round per-slice — fragmented partial fills would let the
        # rounding error accumulate. ``Portfolio.open`` is also raw; the only
        # rounding happens at terminal ``close()`` (consistent with legacy
        # single-shot behavior). This keeps zero-cost flat round-trips
        # cash-conservative even across hundreds of partials.
        self.capital -= additional_qty * fill_price
        return pos

    def partial_close(
        self,
        symbol: str,
        exit_qty: float,
        exit_price: float,
        exit_bid_price: float = 0.0,
    ) -> Optional[Position]:
        """Apply a partial exit: shrink position qty, credit cash, keep open.

        The position remains in ``self.positions``; ``close()`` is called
        separately by the fill simulator once ``pos.is_closed``.

        ``exit_bid_price`` is the bar's pre-slippage reference; when
        provided it accumulates into the weighted exit-bid average so the
        final TradeRecord's ``exit_bid_price`` is coherent with the
        weighted ``exit_price`` across multi-bar partial exits.
        """
        pos = self.positions.get(symbol)
        if pos is None:
            return None
        pos.reduce(exit_qty, exit_price, exit_bid_price)
        # See ``extend`` — no per-slice rounding. The terminal ``close()``
        # rounds once on the residual (which is ~0 by then, so a no-op),
        # matching the legacy single-shot behavior without introducing
        # cumulative drift on multi-slice exits.
        self.capital += exit_qty * exit_price
        return pos

    def close(self, symbol: str, exit_price: float) -> Optional[Position]:
        pos = self.positions.pop(symbol, None)
        if pos is None:
            return None
        # Cash returned uses the exit fill price × current open qty. Realized
        # P&L is owned by the FillSimulator so it can apply slippage / fees;
        # the Portfolio just moves cash. When ``partial_close`` already
        # credited earlier slices, ``pos.qty`` is the residual being closed
        # here so the cash math stays balanced across the lifecycle.
        self.capital += round(pos.qty * exit_price, 2)
        return pos

    def record_pnl(self, net_pnl: float) -> None:
        self.cumulative_pnl = round(self.cumulative_pnl + net_pnl, 2)

    # ------------------------------------------------------------------
    # Price / equity tracking
    # ------------------------------------------------------------------

    def update_last_price(self, symbol: str, price: float) -> None:
        self.last_price[symbol] = price

    def mark_to_market(self) -> float:
        """Return current equity (capital + M2M of all open positions)."""
        mtm = 0.0
        for sym, pos in self.positions.items():
            price_now = self.last_price.get(sym, pos.entry_price)
            if pos.side == OrderSide.LONG:
                mtm += pos.qty * price_now
            else:
                mtm += pos.qty * (2 * pos.entry_price - price_now)
        equity = self.capital + mtm
        if equity > self.peak_equity:
            self.peak_equity = equity
        return equity

    # ------------------------------------------------------------------
    # Snapshot for strategy context (parent → child stdin payload)
    # ------------------------------------------------------------------

    def position_snapshots(self) -> List[Dict[str, object]]:
        return [
            {
                "symbol": p.symbol,
                "side": p.side.value,
                "qty": p.qty,
                "entry_price": p.entry_price,
                "entry_timestamp": p.entry_timestamp,
            }
            for p in self.positions.values()
        ]

    # ------------------------------------------------------------------
    # Fill-style diagnostic builder — bridges Position → Fill protocol.
    # ------------------------------------------------------------------

    def make_entry_fill(self, pos: Position) -> Fill:
        return Fill(
            order_id=pos.entry_order_id,
            client_order_id=pos.entry_client_order_id,
            symbol=pos.symbol,
            side=pos.side,
            qty=pos.qty,
            price=pos.entry_price,
            timestamp=pos.entry_timestamp,
            reason="entry",
        )
