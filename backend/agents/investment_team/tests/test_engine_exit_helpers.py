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

from investment_team.strategy_lab.spec_dsl import StopLossRule, TakeProfitRule
from investment_team.trading_service.engine.order_book import OrderBook
from investment_team.trading_service.engine.portfolio import Portfolio, Position
from investment_team.trading_service.service import (
    ENGINE_EXIT_REASON_PREFIX,
    TradingService,
    TradingServiceResult,
    _EngineExitDispatcher,
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


def _dispatcher(*, exit_rules, bindings=None) -> _EngineExitDispatcher:
    """Build a dispatcher with optional pre-seeded bindings. Tests that
    don't seed bindings get an empty dict by default; ``bindings is not
    None`` lets a test pass in its own mutable dict to observe writes.
    """
    return _EngineExitDispatcher(
        exit_rules=exit_rules,
        engine_exit_bindings={} if bindings is None else bindings,
    )


def _portfolio_with(
    *,
    symbol: str,
    side: OrderSide,
    qty: float,
    entry_price: float,
    entry_order_id: str,
    entry_timestamp: str = "2024-01-01",
    entry_order_type: str = "market",
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
        entry_order_type=entry_order_type,
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


def test_tracker_resets_on_limit_entry_with_just_opened_and_entry_price_watermarks() -> None:
    """Same-bar exit + re-entry with a NON-market entry. The tracker must
    reset trailing watermarks against the new entry — and because the
    fill could have landed anywhere inside the bar, flag
    ``just_opened=True`` so rule eval skips this bar entirely.
    Watermarks initialise at ``entry_price`` (same as the market-entry
    case — see :func:`_extend_watermarks` for the lookahead rationale).
    """
    tracker: Dict[str, _TrackedPosition] = {
        "AAA": _TrackedPosition(
            side=OrderSide.LONG,
            entry_price=100.0,
            entry_order_id="o1",
            just_opened=False,
            high_since_entry=120.0,
            low_since_entry=95.0,
        )
    }
    # Same-bar exit/re-entry — new Position, different entry_order_id, LIMIT type.
    portfolio = _portfolio_with(
        symbol="AAA",
        side=OrderSide.LONG,
        qty=10,
        entry_price=110.0,
        entry_order_id="o2",
        entry_order_type="limit",
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
    assert state.just_opened is True
    # Watermarks initialised at entry_price, NOT the bar's full OHLC, so
    # pre-entry extremes can't drive same-bar rule evaluation later.
    assert state.high_since_entry == 110.0
    assert state.low_since_entry == 110.0


def test_tracker_resets_on_market_entry_with_entry_price_watermarks() -> None:
    """Market entries fill at the bar's open. Rule eval is NOT gated
    (``just_opened=False``) so a same-bar entry_price stop / take-profit
    can fire — those rules consult ``entry_price`` not the trailing
    watermark. The trailing watermark itself initialises at
    ``entry_price`` to avoid intrabar lookahead: if it initialised at
    ``cur_bar.high``/``cur_bar.low`` a trailing stop could trigger on
    the same bar's low from a floor that was raised by the same bar's
    high (regardless of which printed first). ``_extend_watermarks``
    runs AFTER rule evaluation so the next bar reads the bar's range.
    """
    tracker: Dict[str, _TrackedPosition] = {}
    portfolio = _portfolio_with(
        symbol="AAA",
        side=OrderSide.LONG,
        qty=10,
        entry_price=100.0,
        entry_order_id="o-mkt",
        entry_order_type="market",
    )
    bar = _bar(high=112.0, low=88.0)

    TradingService._update_position_tracker(
        tracker=tracker,
        cur_bar=bar,
        portfolio=portfolio,
    )

    state = tracker["AAA"]
    assert state.entry_order_id == "o-mkt"
    assert state.just_opened is False  # rule eval runs on the entry bar
    # Watermarks at entry_price — NOT the bar's OHLC — to keep the
    # entry bar's eval lookahead-free. ``_extend_watermarks`` runs
    # AFTER rule eval and updates them for the next bar.
    assert state.high_since_entry == 100.0
    assert state.low_since_entry == 100.0


def test_tracker_carries_over_when_entry_order_id_unchanged() -> None:
    """Same ``entry_order_id`` → same trade. Weighted-average entry refresh
    from scale-ins; ``just_opened`` flips to False on the first carry-
    over bar. Watermarks are NOT extended here — ``_extend_watermarks``
    runs separately AFTER rule evaluation so trailing-stop rules can't
    fire from a floor that the current bar's high just raised.
    """
    tracker: Dict[str, _TrackedPosition] = {
        "AAA": _TrackedPosition(
            side=OrderSide.LONG,
            entry_price=100.0,
            entry_order_id="o1",
            just_opened=True,
            high_since_entry=100.0,
            low_since_entry=100.0,
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
    assert state.just_opened is False  # first post-entry bar
    assert state.entry_price == 102.5  # scale-in refresh
    # Watermarks unchanged here — extension is now done by the
    # separate ``_extend_watermarks`` call after rule eval. Updating
    # them here would let a trailing stop fire on the same bar's low
    # from a floor raised by the same bar's high (intrabar lookahead).
    assert state.high_since_entry == 100.0
    assert state.low_since_entry == 100.0


def test_extend_watermarks_picks_up_bar_extremes() -> None:
    """``_extend_watermarks`` runs after rule eval and pushes
    high_since_entry / low_since_entry out to the current bar's
    extremes so the NEXT bar's eval sees the latest trailing floor.
    """
    tracker: Dict[str, _TrackedPosition] = {
        "AAA": _TrackedPosition(
            side=OrderSide.LONG,
            entry_price=100.0,
            entry_order_id="o1",
            just_opened=False,
            high_since_entry=100.0,
            low_since_entry=100.0,
        )
    }
    bar = _bar(high=108.0, low=97.0)

    TradingService._extend_watermarks(tracker=tracker, cur_bar=bar)

    state = tracker["AAA"]
    assert state.high_since_entry == 108.0
    assert state.low_since_entry == 97.0


def test_extend_watermarks_no_op_when_symbol_absent() -> None:
    """``_extend_watermarks`` is a no-op when the tracker has no entry
    for the bar's symbol — guards against KeyError after a same-bar
    exit drops the position before watermark extension runs.
    """
    tracker: Dict[str, _TrackedPosition] = {}
    bar = _bar(high=108.0, low=97.0)

    TradingService._extend_watermarks(tracker=tracker, cur_bar=bar)

    assert tracker == {}


def test_tracker_drops_entry_when_position_closed() -> None:
    tracker: Dict[str, _TrackedPosition] = {
        "AAA": _TrackedPosition(
            side=OrderSide.LONG,
            entry_price=100.0,
            entry_order_id="o1",
            just_opened=False,
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
            side=OrderSide.LONG,
            entry_price=100.0,
            entry_order_id="o1",
            just_opened=False,
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
    disp = _dispatcher(exit_rules=[StopLossRule(pct=0.02)])
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
    disp.maybe_emit(
        cur_bar=bar,
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
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
    disp = _dispatcher(exit_rules=[StopLossRule(pct=0.02)])
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
    disp.maybe_emit(
        cur_bar=bar,
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
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
    disp = _dispatcher(exit_rules=[StopLossRule(pct=0.02)])
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
    disp.maybe_emit(
        cur_bar=bar,
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
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
    engine_exit_bindings: Dict[str, str] = {}
    disp = _dispatcher(
        exit_rules=[StopLossRule(pct=0.02)], bindings=engine_exit_bindings
    )  # entry=100 → floor=98; bar.low=95 fires
    tracker, portfolio, order_book = _populate_tracker_and_portfolio()
    bar = _bar(high=105, low=95)
    result = TradingServiceResult()

    pending: list[OrderRequest] = []

    disp.maybe_emit(
        cur_bar=bar,
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
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
    disp = _dispatcher(exit_rules=[StopLossRule(pct=0.02)])
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
    disp.maybe_emit(
        cur_bar=bar,
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
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
    disp = _dispatcher(exit_rules=[StopLossRule(pct=0.02)])
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
    disp.maybe_emit(
        cur_bar=bar,
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )

    # Binding preserved — engine binding loop must not stomp prior bindings.
    assert prior_po.working_against_entry_order_id == "o-other"


def test_engine_does_not_bind_same_side_resting_orders() -> None:
    """A resting same-side order is an add, not a close — it has nothing
    to do with the engine's exit and must not be bound to the position.
    """
    disp = _dispatcher(exit_rules=[StopLossRule(pct=0.02)])
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
    disp.maybe_emit(
        cur_bar=bar,
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )

    # Same-side resting order stays unbound — it isn't a close.
    assert add_po.working_against_entry_order_id is None


def test_engine_binds_same_bar_queued_strategy_exits_too() -> None:
    """A strategy that emits an opposite-side GTC/limit close on the SAME
    bar the engine fires a stop-loss queues the order in
    ``pending_for_prev`` — it isn't on the book yet, so the resting-order
    binding loop misses it. Without binding here too, the strategy's
    close gets submitted unbound on the next bar, survives the engine's
    close-fill, and can later open a reverse position. The fix routes
    the binding through ``engine_exit_bindings`` so the submit step
    stamps ``working_against_entry_order_id`` on its ``PendingOrder``.
    """
    engine_exit_bindings: Dict[str, str] = {}
    disp = _dispatcher(exit_rules=[StopLossRule(pct=0.02)], bindings=engine_exit_bindings)
    tracker, portfolio, order_book = _populate_tracker_and_portfolio()
    bar = _bar(high=105, low=95)  # trips the 98 stop floor
    result = TradingServiceResult()

    # Strategy queues a SELL LIMIT at 150 on the same bar — not yet on the book.
    queued_strategy_exit = OrderRequest(
        client_order_id="c-queued",
        symbol="AAA",
        side=OrderSide.SHORT,
        qty=100.0,
        order_type=OrderType.LIMIT,
        limit_price=150.0,
        tif=TimeInForce.GTC,
        reason="strategy_take_profit",
    )
    pending: list[OrderRequest] = [queued_strategy_exit]

    disp.maybe_emit(
        cur_bar=bar,
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )

    # Engine emitted its stop-loss close AND registered a binding for the
    # strategy's same-bar queued limit. When the submit step processes
    # ``pending_for_prev`` on the next bar, both orders get
    # ``working_against_entry_order_id`` stamped → the stale-continuation
    # guard drops the survivor when the engine close fills first.
    assert "c-queued" in engine_exit_bindings
    assert engine_exit_bindings["c-queued"] == "o1"


def test_engine_does_not_bind_same_bar_same_side_queued_orders() -> None:
    """Symmetric to the resting-order carve-out: a same-bar same-side
    queued order (a scale-in entry) must NOT be bound to the position.
    """
    engine_exit_bindings: Dict[str, str] = {}
    disp = _dispatcher(exit_rules=[StopLossRule(pct=0.02)], bindings=engine_exit_bindings)
    tracker, portfolio, order_book = _populate_tracker_and_portfolio()
    bar = _bar(high=105, low=95)
    result = TradingServiceResult()

    queued_add = OrderRequest(
        client_order_id="c-add",
        symbol="AAA",
        side=OrderSide.LONG,
        qty=50.0,
        order_type=OrderType.LIMIT,
        limit_price=80.0,
        tif=TimeInForce.GTC,
        reason="strategy_scale_in",
    )
    pending: list[OrderRequest] = [queued_add]

    disp.maybe_emit(
        cur_bar=bar,
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )

    # The engine's own emission gets a binding; the same-side scale-in does not.
    engine_orders = [r for r in pending if (r.reason or "").startswith(ENGINE_EXIT_REASON_PREFIX)]
    engine_cid = engine_orders[0].client_order_id
    assert engine_cid in engine_exit_bindings
    assert "c-add" not in engine_exit_bindings


def test_engine_skips_rule_eval_on_entry_bar() -> None:
    """A limit-filled entry that lands mid-bar shares an OHLC bar with
    price action from BEFORE the fill. If the engine evaluated rules on
    that bar against ``bar.high`` / ``bar.low``, a take-profit could
    fire from a pre-entry high (e.g. buy limit at 95 filled by a bar
    with high=110 instantly trips TakeProfit(pct=0.05) → target=99.75).
    ``just_opened=True`` skips rule eval entirely on the entry bar; the
    next bar's tracker update clears the flag and rule eval resumes.
    """
    disp = _dispatcher(exit_rules=[TakeProfitRule(pct=0.05)])
    # Position entered at 95, bar.high=110 would trip a take-profit at
    # 95 * 1.05 = 99.75 if not gated by just_opened.
    tracker = {
        "AAA": _TrackedPosition(
            side=OrderSide.LONG,
            entry_price=95.0,
            entry_order_id="o1",
            just_opened=True,
            high_since_entry=95.0,
            low_since_entry=95.0,
        )
    }
    portfolio = _portfolio_with(
        symbol="AAA",
        side=OrderSide.LONG,
        qty=100,
        entry_price=95.0,
        entry_order_id="o1",
    )
    order_book = OrderBook()
    bar = _bar(high=110.0, low=95.0, close=105.0)
    result = TradingServiceResult()

    pending: list[OrderRequest] = []
    disp.maybe_emit(
        cur_bar=bar,
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )

    # No engine emission — the entry bar is gated even though bar.high
    # nominally clears the take-profit target. Rule eval resumes once
    # the next bar flips just_opened to False.
    assert pending == []
    assert result.execution_diagnostics.exit_rule_firings == {}


def test_engine_runs_rules_normally_after_first_post_entry_bar() -> None:
    """Counterpart to ``test_engine_skips_rule_eval_on_entry_bar``: once
    ``just_opened`` is False, take-profit / stop-loss evaluate against
    the bar's OHLC as expected.
    """
    disp = _dispatcher(exit_rules=[TakeProfitRule(pct=0.05)])
    tracker = {
        "AAA": _TrackedPosition(
            side=OrderSide.LONG,
            entry_price=95.0,
            entry_order_id="o1",
            just_opened=False,  # first post-entry bar
            high_since_entry=95.0,
            low_since_entry=95.0,
        )
    }
    portfolio = _portfolio_with(
        symbol="AAA",
        side=OrderSide.LONG,
        qty=100,
        entry_price=95.0,
        entry_order_id="o1",
    )
    order_book = OrderBook()
    # Same bar shape as the entry-bar test, but now post-entry.
    bar = _bar(high=110.0, low=95.0, close=105.0)
    result = TradingServiceResult()

    pending: list[OrderRequest] = []
    disp.maybe_emit(
        cur_bar=bar,
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )

    # Take-profit fires off the bar's high.
    assert result.execution_diagnostics.exit_rule_firings.get("take_profit") == 1


def test_engine_cancels_pending_entry_continuation_when_exit_fires() -> None:
    """A partial-fill entry remainder (REQUEUE_NEXT_BAR / TWAP slice) is
    still pending on the book when the engine fires its stop-loss /
    take-profit. Without cancelling, the continuation could fill on the
    next bar before the engine's market close, growing the position
    past the engine's sized close (which clips at the snapshot ``pos.qty``
    via ``_fill_exit``'s ``min(req.qty, existing_pos.qty)`` invariant).
    The fix scans ``order_book.pending_for_symbol(sym)`` for orders
    matching ``pos.entry_order_id`` with ``cumulative_filled_qty > 0``
    and cancels them so the rule's close is the last word on the
    position.
    """
    disp = _dispatcher(exit_rules=[StopLossRule(pct=0.02)])
    tracker, portfolio, order_book = _populate_tracker_and_portfolio()
    bar = _bar(high=105, low=95)  # bar.low=95 trips floor=98
    result = TradingServiceResult()

    # Build a pending entry continuation: same side as position, marked
    # as partially filled, ``order_id == pos.entry_order_id`` so the
    # engine recognises it as a continuation.
    entry_continuation = OrderRequest(
        client_order_id="c-entry",
        symbol="AAA",
        side=OrderSide.LONG,
        qty=100.0,
        order_type=OrderType.MARKET,
        tif=TimeInForce.DAY,
        reason="entry_continuation",
    )
    submitted_po = order_book.submit(
        entry_continuation,
        submitted_at="2024-01-05T00:00:00",
        submitted_equity=100_000.0,
    )
    # Force the order_id to match the position's entry_order_id (the
    # real engine assigns this on first submission; we splice it in for
    # the test since the order book auto-assigns an "o2"-style id).
    order_book._pending[submitted_po.order_id] = submitted_po
    submitted_po.order_id = "o1"
    order_book._pending["o1"] = submitted_po
    submitted_po.cumulative_filled_qty = 40.0  # 40 of 100 already filled
    # Map the order under the position's symbol bucket so
    # ``pending_for_symbol`` returns it under the matched id.
    order_book._by_symbol["AAA"].append("o1")

    pending: list[OrderRequest] = []
    disp.maybe_emit(
        cur_bar=bar,
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )

    # Engine emitted its stop-loss close AND cancelled the pending
    # entry continuation so it can't fill on the next bar.
    engine_orders = [r for r in pending if (r.reason or "").startswith(ENGINE_EXIT_REASON_PREFIX)]
    assert len(engine_orders) == 1
    assert "o1" not in order_book._pending


def test_trailing_stop_does_not_lookahead_within_a_bar() -> None:
    """Regression: trailing-stop rules must not see the current bar's
    high in the trailing floor while evaluating against the current
    bar's low. A long entered at 100 with
    ``StopLossRule(pct=0.05, basis="trailing_high")``: if today prints
    high=120 and low=110, the trailing floor would be 120*(1-0.05)=114
    and bar.low=110 would trip it — even though we don't know whether
    the high or the low printed first. The fix initialises watermarks
    at ``entry_price`` and defers watermark extension to
    ``_extend_watermarks`` which runs AFTER rule evaluation.
    """
    disp = _dispatcher(exit_rules=[StopLossRule(pct=0.05, basis="trailing_high")])
    # Fresh entry, watermarks at entry_price=100.
    tracker: Dict[str, _TrackedPosition] = {
        "AAA": _TrackedPosition(
            side=OrderSide.LONG,
            entry_price=100.0,
            entry_order_id="o1",
            just_opened=False,
            high_since_entry=100.0,
            low_since_entry=100.0,
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
    # Today: high=120 (would push trailing floor to 114), low=110.
    # 110 > 100*(1-0.05)=95, so the trailing stop must NOT fire.
    bar = _bar(high=120.0, low=110.0, close=115.0)
    result = TradingServiceResult()

    pending: list[OrderRequest] = []
    disp.maybe_emit(
        cur_bar=bar,
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )

    # No engine emission — the trailing floor is 95 (from entry_price),
    # bar.low=110 is well clear.
    assert pending == []
    assert result.execution_diagnostics.exit_rule_firings == {}

    # NOW extend watermarks (as the bar-loop does after rule eval).
    # The high gets baked in for the next bar.
    TradingService._extend_watermarks(tracker=tracker, cur_bar=bar)
    assert tracker["AAA"].high_since_entry == 120.0


def test_engine_oversizes_close_to_cover_same_bar_scale_ins() -> None:
    """A strategy scale-in queued on the SAME bar the engine fires its
    stop-loss submits BEFORE the engine close on the next bar (its
    ``client_order_id`` is earlier in ``pending_for_prev``). When that
    scale-in fills first, ``pos.qty`` grows past the snapshot the
    engine saw at emission time, and ``_fill_exit`` clips the engine
    close at ``min(req.qty, existing_pos.qty)`` — leaving the scale-in
    residual exposure open even though the structured stop already
    fired. Fix: sum same-side queued qtys and oversize the engine
    close by that amount; the clip-down then nets to zero exposure.
    """
    disp = _dispatcher(exit_rules=[StopLossRule(pct=0.02)])
    tracker, portfolio, order_book = _populate_tracker_and_portfolio()
    bar = _bar(high=105, low=95)  # bar.low=95 trips floor=98
    result = TradingServiceResult()

    # Strategy queues a same-side LONG scale-in on this bar — fills
    # before the engine close on the next bar.
    queued_add = OrderRequest(
        client_order_id="c-scale-in",
        symbol="AAA",
        side=OrderSide.LONG,
        qty=50.0,
        order_type=OrderType.MARKET,
        tif=TimeInForce.DAY,
        reason="strategy_scale_in",
    )
    pending: list[OrderRequest] = [queued_add]

    disp.maybe_emit(
        cur_bar=bar,
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )

    engine_orders = [r for r in pending if (r.reason or "").startswith(ENGINE_EXIT_REASON_PREFIX)]
    assert len(engine_orders) == 1
    # Engine close sized to cover pos.qty (100) + scale-in (50) = 150.
    # If the scale-in is rejected/clipped at fill time, ``_fill_exit``
    # clips the engine close back down to existing_pos.qty.
    assert engine_orders[0].qty == 150.0


def test_engine_does_not_oversize_for_opposite_side_queued_orders() -> None:
    """An opposite-side queued order is a strategy exit, not a
    scale-in. It must NOT inflate the engine's close qty (the binding
    machinery will dedup it at fill time instead).
    """
    disp = _dispatcher(exit_rules=[StopLossRule(pct=0.02)])
    tracker, portfolio, order_book = _populate_tracker_and_portfolio()
    bar = _bar(high=105, low=95)
    result = TradingServiceResult()

    queued_strategy_exit = OrderRequest(
        client_order_id="c-strategy-exit",
        symbol="AAA",
        side=OrderSide.SHORT,
        qty=50.0,
        order_type=OrderType.MARKET,
        tif=TimeInForce.DAY,
        reason="strategy_close",
    )
    pending: list[OrderRequest] = [queued_strategy_exit]

    disp.maybe_emit(
        cur_bar=bar,
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )

    engine_orders = [r for r in pending if (r.reason or "").startswith(ENGINE_EXIT_REASON_PREFIX)]
    assert len(engine_orders) == 1
    # No oversize — exits aren't scale-ins.
    assert engine_orders[0].qty == 100.0


def test_engine_does_not_oversize_for_other_symbol_queued_orders() -> None:
    """``_sum_same_side_queued`` is symbol-scoped — a same-side queued
    order for a different symbol must not inflate the close qty.
    """
    disp = _dispatcher(exit_rules=[StopLossRule(pct=0.02)])
    tracker, portfolio, order_book = _populate_tracker_and_portfolio()
    bar = _bar(high=105, low=95)
    result = TradingServiceResult()

    queued_other = OrderRequest(
        client_order_id="c-other-sym",
        symbol="BBB",
        side=OrderSide.LONG,
        qty=200.0,
        order_type=OrderType.MARKET,
        tif=TimeInForce.DAY,
        reason="strategy_other_symbol",
    )
    pending: list[OrderRequest] = [queued_other]

    disp.maybe_emit(
        cur_bar=bar,
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )

    engine_orders = [r for r in pending if (r.reason or "").startswith(ENGINE_EXIT_REASON_PREFIX)]
    assert len(engine_orders) == 1
    assert engine_orders[0].qty == 100.0
