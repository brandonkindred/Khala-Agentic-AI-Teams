"""Unit tests for :class:`TradingService`'s private exit-rule helpers
(``_update_position_tracker`` / ``_maybe_emit_engine_exits``).

These exercise behaviours hard to construct via the streaming subprocess
end-to-end:

* Tracker resets when the position identity (``entry_order_id``)
  changes — a same-bar exit + re-entry must not inherit stale
  ``bars_held`` / watermarks.
* Engine dedup ignores non-market strategy orders (limit/stop closes
  far from the market aren't guaranteed to fill).
* Engine emissions bind to ``pos.entry_order_id`` via
  ``engine_exit_bindings`` so the fill simulator's stale-continuation
  guard can drop them when a prior strategy exit closes the position
  first.

Drives the helpers with hand-built ``Portfolio`` / ``OrderBook`` /
``OrderRequest`` fixtures so the assertions are precise.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from investment_team.models import BacktestConfig
from investment_team.strategy_lab.spec_dsl import (
    StopLossRule,
    TimeStopRule,
)
from investment_team.trading_service.engine.order_book import OrderBook
from investment_team.trading_service.engine.portfolio import Portfolio, Position
from investment_team.trading_service.service import (
    ENGINE_EXIT_REASON_PREFIX,
    TradingService,
    TradingServiceResult,
    _TrackedPosition,
)
from investment_team.trading_service.strategy.contract import (
    OrderRequest,
    OrderSide,
    OrderType,
    TimeInForce,
)


@dataclass
class _MockBar:
    symbol: str
    timestamp: str
    high: float
    low: float
    close: float


def _service(*, exit_rules) -> TradingService:
    return TradingService(
        strategy_code="from contract import Strategy\nclass S(Strategy):\n    pass\n",
        config=BacktestConfig(start_date="2024-01-01", end_date="2024-12-31"),
        exit_rules=exit_rules,
    )


def _portfolio_with(
    *,
    symbol: str,
    side: OrderSide,
    qty: float,
    entry_price: float,
    entry_order_id: str,
    entry_timestamp: str = "2024-01-01",
) -> Portfolio:
    p = Portfolio(initial_capital=100_000.0)
    p.positions[symbol] = Position(
        symbol=symbol,
        side=side,
        qty=qty,
        entry_price=entry_price,
        entry_bid_price=entry_price,
        entry_timestamp=entry_timestamp,
        entry_order_id=entry_order_id,
        entry_client_order_id=f"c-{entry_order_id}",
        original_qty=qty,
    )
    return p


def _bar(symbol: str = "AAA", **kwargs) -> _MockBar:
    defaults = {
        "symbol": symbol,
        "timestamp": "2024-01-10T00:00:00",
        "high": 105.0,
        "low": 95.0,
        "close": 100.0,
    }
    defaults.update(kwargs)
    return _MockBar(**defaults)


# ---------------------------------------------------------------------------
# _update_position_tracker
# ---------------------------------------------------------------------------


def test_tracker_resets_when_position_identity_changes() -> None:
    """Same-bar exit + re-entry replaces the underlying ``Position`` with a
    new ``entry_order_id``. The tracker must reset to ``bars_held=1`` /
    fresh watermarks against the new entry — not carry the prior trade's
    stale state, which could fire a ``TimeStopRule`` or trailing-stop
    immediately.
    """
    tracker: Dict[str, _TrackedPosition] = {
        "AAA": _TrackedPosition(
            side="long",
            entry_price=100.0,
            entry_order_id="o1",
            bars_held=9,
            high_since_entry=120.0,
            low_since_entry=95.0,
        )
    }
    # Same-bar exit/re-entry — new Position, different entry_order_id.
    portfolio = _portfolio_with(
        symbol="AAA",
        side=OrderSide.LONG,
        qty=10,
        entry_price=110.0,
        entry_order_id="o2",
    )
    bar = _bar(high=112.0, low=109.0)

    TradingService._update_position_tracker(
        tracker=tracker,
        cur_bar=bar,
        portfolio=portfolio,
    )

    state = tracker["AAA"]
    assert state.entry_order_id == "o2"
    assert state.entry_price == 110.0
    assert state.bars_held == 1
    assert state.high_since_entry == 112.0
    assert state.low_since_entry == 109.0


def test_tracker_carries_over_when_entry_order_id_unchanged() -> None:
    """Same ``entry_order_id`` → same trade. bars_held++; weighted-average
    entry refresh from scale-ins; watermarks extend.
    """
    tracker: Dict[str, _TrackedPosition] = {
        "AAA": _TrackedPosition(
            side="long",
            entry_price=100.0,
            entry_order_id="o1",
            bars_held=3,
            high_since_entry=105.0,
            low_since_entry=98.0,
        )
    }
    # Same entry_order_id but ``Portfolio.extend`` would have updated
    # ``entry_price`` to a weighted average.
    portfolio = _portfolio_with(
        symbol="AAA",
        side=OrderSide.LONG,
        qty=20,
        entry_price=102.5,
        entry_order_id="o1",
    )
    bar = _bar(high=108.0, low=97.0)

    TradingService._update_position_tracker(
        tracker=tracker,
        cur_bar=bar,
        portfolio=portfolio,
    )

    state = tracker["AAA"]
    assert state.entry_order_id == "o1"
    assert state.bars_held == 4
    assert state.entry_price == 102.5  # scale-in refresh
    assert state.high_since_entry == 108.0  # extends
    assert state.low_since_entry == 97.0  # extends


def test_tracker_drops_entry_when_position_closed() -> None:
    tracker: Dict[str, _TrackedPosition] = {
        "AAA": _TrackedPosition(
            side="long",
            entry_price=100.0,
            entry_order_id="o1",
            bars_held=5,
            high_since_entry=110.0,
            low_since_entry=95.0,
        )
    }
    portfolio = Portfolio(initial_capital=100_000.0)  # empty positions
    bar = _bar()

    TradingService._update_position_tracker(
        tracker=tracker,
        cur_bar=bar,
        portfolio=portfolio,
    )

    assert "AAA" not in tracker


# ---------------------------------------------------------------------------
# _maybe_emit_engine_exits
# ---------------------------------------------------------------------------


def _populate_tracker_and_portfolio() -> tuple[Dict[str, _TrackedPosition], Portfolio, OrderBook]:
    tracker = {
        "AAA": _TrackedPosition(
            side="long",
            entry_price=100.0,
            entry_order_id="o1",
            bars_held=10,
            high_since_entry=110.0,
            low_since_entry=95.0,
        )
    }
    portfolio = _portfolio_with(
        symbol="AAA",
        side=OrderSide.LONG,
        qty=100,
        entry_price=100.0,
        entry_order_id="o1",
    )
    order_book = OrderBook()
    return tracker, portfolio, order_book


def test_dedup_ignores_limit_close_far_from_market() -> None:
    """A strategy's full-size limit close far from market is not guaranteed
    to fill — it must NOT suppress the engine's market emission. Otherwise
    the rule is silently skipped while the limit hangs and the position
    stays open past the structured-exit threshold.
    """
    svc = _service(exit_rules=[TimeStopRule(n_bars=10)])
    tracker, portfolio, order_book = _populate_tracker_and_portfolio()
    bar = _bar()
    result = TradingServiceResult()

    # Strategy emits a far-from-market LIMIT sell for 100 shares.
    pending = [
        OrderRequest(
            client_order_id="c1",
            symbol="AAA",
            side=OrderSide.SHORT,
            qty=100.0,
            order_type=OrderType.LIMIT,
            limit_price=200.0,  # well above; won't fill on next bar
            tif=TimeInForce.DAY,
            reason="strategy_limit_exit",
        )
    ]
    engine_exit_bindings: Dict[str, str] = {}

    svc._maybe_emit_engine_exits(
        cur_bar=bar,
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
        engine_order_seq=0,
        engine_exit_bindings=engine_exit_bindings,
    )

    # Engine MUST still emit its market close — the limit isn't guaranteed.
    engine_orders = [r for r in pending if (r.reason or "").startswith(ENGINE_EXIT_REASON_PREFIX)]
    assert len(engine_orders) == 1
    assert engine_orders[0].qty == 100.0
    assert engine_orders[0].order_type == OrderType.MARKET
    assert result.execution_diagnostics.exit_rule_firings.get("time_stop") == 1


def test_dedup_skips_engine_emission_on_full_market_strategy_close() -> None:
    """Full-size market close from strategy on the same bar → engine
    emission is redundant and must be skipped.
    """
    svc = _service(exit_rules=[TimeStopRule(n_bars=10)])
    tracker, portfolio, order_book = _populate_tracker_and_portfolio()
    bar = _bar()
    result = TradingServiceResult()

    pending = [
        OrderRequest(
            client_order_id="c1",
            symbol="AAA",
            side=OrderSide.SHORT,
            qty=100.0,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
            reason="strategy_full_exit",
        )
    ]
    svc._maybe_emit_engine_exits(
        cur_bar=bar,
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
        engine_order_seq=0,
        engine_exit_bindings={},
    )

    # No engine emission — strategy fully covered the close.
    engine_orders = [r for r in pending if (r.reason or "").startswith(ENGINE_EXIT_REASON_PREFIX)]
    assert engine_orders == []
    assert result.execution_diagnostics.exit_rule_firings == {}


def test_engine_emission_registers_binding_to_entry_order_id() -> None:
    """The binding lets the fill simulator's stale-continuation guard drop
    the engine close when a prior strategy exit closes the position
    first. Without it, the engine market falls through to ``_fill_entry``
    and opens a brand-new opposite-side position.
    """
    svc = _service(exit_rules=[StopLossRule(pct=0.02)])  # entry=100 → floor=98; bar.low=95 fires
    tracker, portfolio, order_book = _populate_tracker_and_portfolio()
    bar = _bar(high=105, low=95)
    result = TradingServiceResult()

    engine_exit_bindings: Dict[str, str] = {}
    pending: list[OrderRequest] = []

    svc._maybe_emit_engine_exits(
        cur_bar=bar,
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
        engine_order_seq=0,
        engine_exit_bindings=engine_exit_bindings,
    )

    assert len(pending) == 1
    cid = pending[0].client_order_id
    assert cid in engine_exit_bindings
    assert engine_exit_bindings[cid] == "o1"
