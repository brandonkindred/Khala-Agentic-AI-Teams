"""Unit tests for :class:`TradingService`'s private exit-rule helpers
(``_update_position_tracker`` / ``_maybe_emit_engine_exits``).

These exercise behaviours hard to construct via the streaming subprocess
end-to-end:

* Tracker resets when the position identity (``entry_order_id``)
  changes — a same-bar exit + re-entry must not inherit stale
  trailing watermarks.
* Engine emits at full ``pos.qty`` regardless of any same-bar strategy
  close (the fill simulator does dedup at fill time via the binding).
* In-flight engine markets prevent re-emission so the book doesn't
  stack redundant engine closes across bars.
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
from investment_team.strategy_lab.spec_dsl import StopLossRule
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
    new ``entry_order_id``. The tracker must reset trailing watermarks
    against the new entry — not carry the prior trade's stale state,
    which could fire a trailing-stop immediately.
    """
    tracker: Dict[str, _TrackedPosition] = {
        "AAA": _TrackedPosition(
            side="long",
            entry_price=100.0,
            entry_order_id="o1",
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
    assert state.high_since_entry == 112.0
    assert state.low_since_entry == 109.0


def test_tracker_carries_over_when_entry_order_id_unchanged() -> None:
    """Same ``entry_order_id`` → same trade. Weighted-average entry refresh
    from scale-ins; watermarks extend.
    """
    tracker: Dict[str, _TrackedPosition] = {
        "AAA": _TrackedPosition(
            side="long",
            entry_price=100.0,
            entry_order_id="o1",
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
    assert state.entry_price == 102.5  # scale-in refresh
    assert state.high_since_entry == 108.0  # extends
    assert state.low_since_entry == 97.0  # extends


def test_tracker_drops_entry_when_position_closed() -> None:
    tracker: Dict[str, _TrackedPosition] = {
        "AAA": _TrackedPosition(
            side="long",
            entry_price=100.0,
            entry_order_id="o1",
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


def test_engine_always_emits_at_full_position_qty() -> None:
    """The engine always emits its close at ``pos.qty`` and lets the fill
    simulator handle dedup against any strategy same-bar close. This
    keeps enforcement robust against participation-cap clipping and
    FOK/IOC rejection on the strategy's own order — see the docstring
    of ``_maybe_emit_engine_exits``.

    Far-from-market limit close from the strategy: not guaranteed to
    fill, so the engine emits its full-size market close regardless.
    """
    # bar.low=95 trips a StopLossRule(pct=0.02) (floor=98).
    svc = _service(exit_rules=[StopLossRule(pct=0.02)])
    tracker, portfolio, order_book = _populate_tracker_and_portfolio()
    bar = _bar()
    result = TradingServiceResult()

    pending = [
        OrderRequest(
            client_order_id="c1",
            symbol="AAA",
            side=OrderSide.SHORT,
            qty=100.0,
            order_type=OrderType.LIMIT,
            limit_price=200.0,
            tif=TimeInForce.DAY,
            reason="strategy_limit_exit",
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
    engine_orders = [r for r in pending if (r.reason or "").startswith(ENGINE_EXIT_REASON_PREFIX)]
    assert len(engine_orders) == 1
    assert engine_orders[0].qty == 100.0
    assert engine_orders[0].order_type == OrderType.MARKET


def test_engine_emits_even_when_strategy_submits_full_market_close() -> None:
    """A strategy full-size market close is not a coverage guarantee —
    it can be FOK-rejected, IOC-clipped, or participation-cap-clipped at
    fill time. The engine emits its full-qty market regardless; the fill
    simulator's stale-continuation guard (via binding) drops the engine
    close at fill time if the strategy's order fully closed the position
    first, and clips the engine close to the residual qty otherwise.
    """
    svc = _service(exit_rules=[StopLossRule(pct=0.02)])
    tracker, portfolio, order_book = _populate_tracker_and_portfolio()
    bar = _bar()  # low=95 trips the 98 floor
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
    engine_orders = [r for r in pending if (r.reason or "").startswith(ENGINE_EXIT_REASON_PREFIX)]
    assert len(engine_orders) == 1
    assert engine_orders[0].qty == 100.0
    assert result.execution_diagnostics.exit_rule_firings.get("stop_loss") == 1


def test_engine_skips_when_prior_engine_exit_still_pending() -> None:
    """A prior bar's engine market exit (e.g. ``REQUEUE_NEXT_BAR`` residual
    that didn't fully fill on the next bar) is still pending on the order
    book. The engine must NOT re-emit while it's still in flight —
    otherwise every subsequent bar where the rule re-triggers stacks
    another engine market, polluting the book and the diagnostics.
    """
    svc = _service(exit_rules=[StopLossRule(pct=0.02)])
    tracker, portfolio, order_book = _populate_tracker_and_portfolio()
    bar = _bar()
    result = TradingServiceResult()

    # Simulate a prior bar's engine exit sitting on the book.
    prior_engine_exit = OrderRequest(
        client_order_id="e1",
        symbol="AAA",
        side=OrderSide.SHORT,
        qty=100.0,
        order_type=OrderType.MARKET,
        tif=TimeInForce.DAY,
        reason=f"{ENGINE_EXIT_REASON_PREFIX}stop_loss",
    )
    order_book.submit(
        prior_engine_exit, submitted_at="2024-01-09T00:00:00", submitted_equity=100_000.0
    )

    pending: list[OrderRequest] = []
    svc._maybe_emit_engine_exits(
        cur_bar=bar,
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
        engine_order_seq=1,
        engine_exit_bindings={},
    )

    # No new engine emission — the prior one is still in flight.
    assert pending == []
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


def test_engine_binds_unbound_resting_strategy_exits_to_position() -> None:
    """A strategy GTC/limit close resting on the book BEFORE the engine
    fires a stop-loss must be retired alongside the engine's market close,
    or it survives the position-close and a later trigger opens an
    unintended reverse position (``FillSimulator.process_bar`` routes the
    unbound order to ``_fill_entry`` because ``existing_pos is None``).
    The engine binds the resting order to the same ``entry_order_id`` so
    the stale-continuation guard drops it when the engine close fires.
    """
    svc = _service(exit_rules=[StopLossRule(pct=0.02)])
    tracker, portfolio, order_book = _populate_tracker_and_portfolio()
    bar = _bar(high=105, low=95)  # bar.low=95 trips the 98 stop floor
    result = TradingServiceResult()

    # Strategy has a resting GTC SELL LIMIT at 200 (a take-profit far
    # above market) — opposite side to the long position, unbound.
    resting_strategy_exit = OrderRequest(
        client_order_id="c1",
        symbol="AAA",
        side=OrderSide.SHORT,
        qty=100.0,
        order_type=OrderType.LIMIT,
        limit_price=200.0,
        tif=TimeInForce.GTC,
        reason="strategy_take_profit",
    )
    resting_po = order_book.submit(
        resting_strategy_exit,
        submitted_at="2024-01-05T00:00:00",
        submitted_equity=100_000.0,
    )
    # Sanity: starts unbound.
    assert resting_po.working_against_entry_order_id is None

    pending: list[OrderRequest] = []
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

    # Engine emitted its stop-loss close…
    engine_orders = [r for r in pending if (r.reason or "").startswith(ENGINE_EXIT_REASON_PREFIX)]
    assert len(engine_orders) == 1
    # …and the resting strategy GTC limit is now bound to the same
    # position. When the engine close fills next bar and removes the
    # position, the fill simulator's stale guard will drop the GTC at
    # its next trigger instead of opening a reverse short.
    assert resting_po.working_against_entry_order_id == "o1"


def test_engine_does_not_rebind_already_bound_resting_orders() -> None:
    """The binding pass must leave already-bound orders alone — e.g. a
    prior bar's engine exit or a strategy partial that's already in
    flight against the position. Re-binding to a fresh ``entry_order_id``
    on every subsequent bar would be a no-op here (same id), but the
    invariant is what we're guarding.
    """
    svc = _service(exit_rules=[StopLossRule(pct=0.02)])
    tracker, portfolio, order_book = _populate_tracker_and_portfolio()
    bar = _bar(high=105, low=95)
    result = TradingServiceResult()

    prior = OrderRequest(
        client_order_id="c-prior",
        symbol="AAA",
        side=OrderSide.SHORT,
        qty=100.0,
        order_type=OrderType.LIMIT,
        limit_price=150.0,
        tif=TimeInForce.GTC,
        reason="strategy_other",
    )
    prior_po = order_book.submit(
        prior, submitted_at="2024-01-05T00:00:00", submitted_equity=100_000.0
    )
    prior_po.working_against_entry_order_id = "o-other"  # pre-bound elsewhere

    pending: list[OrderRequest] = []
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

    # Binding preserved — engine binding loop must not stomp prior bindings.
    assert prior_po.working_against_entry_order_id == "o-other"


def test_engine_does_not_bind_same_side_resting_orders() -> None:
    """A resting same-side order is an add, not a close — it has nothing
    to do with the engine's exit and must not be bound to the position.
    """
    svc = _service(exit_rules=[StopLossRule(pct=0.02)])
    tracker, portfolio, order_book = _populate_tracker_and_portfolio()
    bar = _bar(high=105, low=95)
    result = TradingServiceResult()

    # Same-side (LONG) limit — strategy intends to add to the position.
    resting_add = OrderRequest(
        client_order_id="c-add",
        symbol="AAA",
        side=OrderSide.LONG,
        qty=50.0,
        order_type=OrderType.LIMIT,
        limit_price=80.0,
        tif=TimeInForce.GTC,
        reason="strategy_scale_in",
    )
    add_po = order_book.submit(
        resting_add, submitted_at="2024-01-05T00:00:00", submitted_equity=100_000.0
    )

    pending: list[OrderRequest] = []
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

    # Same-side resting order stays unbound — it isn't a close.
    assert add_po.working_against_entry_order_id is None
