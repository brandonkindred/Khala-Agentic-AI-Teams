"""Tests for DSL-authored stop-limit exits on the structured-exit path.

These cover the engine wiring that lets a ``StopLossRule(style="limit")`` rest
as a structured exit:

* the rule evaluator resolves the stop level and carries ``style`` /
  ``limit_offset_pct`` / ``stop_price`` onto the ``ExitIntent``;
* ``_build_close_order`` emits a STOP_LIMIT (GTC, ``REQUEUE_NEXT_BAR``) with the
  limit on the protective side, for both long and short positions;
* the in-flight guard treats a resting engine STOP_LIMIT as a "resting
  structured exit" and stands the dispatcher down (no duplicate emission);
* retirement / cancellation of competing orders is deferred to the actual fill
  for a limit-style stop, but still runs immediately for a market stop;
* the emission lifecycle event records the real STOP_LIMIT order type.

The STOP_LIMIT fill / gap-through / latch mechanics themselves are already
covered deterministically by ``tests/test_stop_limit.py`` — these tests assert
the *dispatcher* hands the simulator a correct, resting STOP_LIMIT.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from investment_team.strategy_lab.executor.rule_compiler import (
    BarSnapshot,
    PositionState,
    evaluate_exit_rules,
)
from investment_team.strategy_lab.spec_dsl import StopLossRule, TakeProfitRule
from investment_team.trading_service.engine.order_book import OrderBook
from investment_team.trading_service.engine.portfolio import Portfolio, Position
from investment_team.trading_service.service import (
    ENGINE_EXIT_REASON_PREFIX,
    TradingServiceResult,
    _EngineExitDispatcher,
    _TrackedPosition,
)
from investment_team.trading_service.strategy.contract import (
    OrderRequest,
    OrderSide,
    OrderType,
    TimeInForce,
    UnfilledPolicy,
)


@dataclass
class _MockBar:
    symbol: str
    timestamp: str
    high: float
    low: float
    close: float


def _dispatcher(*, exit_rules) -> _EngineExitDispatcher:
    return _EngineExitDispatcher(exit_rules=exit_rules, engine_exit_bindings={})


def _portfolio_with(
    *, symbol: str, side: OrderSide, qty: float, entry_price: float, entry_order_id: str
) -> Portfolio:
    p = Portfolio(initial_capital=100_000.0)
    p.positions[symbol] = Position(
        symbol=symbol,
        side=side,
        qty=qty,
        entry_price=entry_price,
        entry_bid_price=entry_price,
        entry_timestamp="2024-01-01",
        entry_order_id=entry_order_id,
        entry_client_order_id=f"c-{entry_order_id}",
        original_qty=qty,
        entry_order_type="market",
    )
    return p


def _long_setup(
    *, entry_price: float = 100.0
) -> tuple[Dict[str, _TrackedPosition], Portfolio, OrderBook]:
    tracker = {
        "AAA": _TrackedPosition(
            side=OrderSide.LONG,
            entry_price=entry_price,
            entry_order_id="o1",
            just_opened=False,
            high_since_entry=110.0,
            low_since_entry=95.0,
        )
    }
    portfolio = _portfolio_with(
        symbol="AAA", side=OrderSide.LONG, qty=100, entry_price=entry_price, entry_order_id="o1"
    )
    return tracker, portfolio, OrderBook()


def _short_setup(
    *, entry_price: float = 100.0
) -> tuple[Dict[str, _TrackedPosition], Portfolio, OrderBook]:
    tracker = {
        "AAA": _TrackedPosition(
            side=OrderSide.SHORT,
            entry_price=entry_price,
            entry_order_id="o1",
            just_opened=False,
            high_since_entry=105.0,
            low_since_entry=90.0,
        )
    }
    portfolio = _portfolio_with(
        symbol="AAA", side=OrderSide.SHORT, qty=100, entry_price=entry_price, entry_order_id="o1"
    )
    return tracker, portfolio, OrderBook()


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


def _engine_orders(pending):
    return [r for r in pending if (r.reason or "").startswith(ENGINE_EXIT_REASON_PREFIX)]


# ---------------------------------------------------------------------------
# Evaluator: ExitIntent carries style / limit_offset_pct / resolved stop_price.
# ---------------------------------------------------------------------------


def test_intent_carries_limit_style_and_resolved_stop_for_long():
    rule = StopLossRule(pct=0.02, style="limit", limit_offset_pct=0.01)
    pos = PositionState(
        symbol="AAA",
        side="long",
        qty=100,
        entry_price=100.0,
        high_since_entry=110.0,
        low_since_entry=95.0,
    )
    # bar.low=97 crosses the 98 floor (100 * (1 - 0.02)).
    bars = {"AAA": BarSnapshot(high=105.0, low=97.0, close=99.0)}
    intents = evaluate_exit_rules([rule], {"AAA": pos}, bars)
    assert len(intents) == 1
    intent = intents[0]
    assert intent.rule_kind == "stop_loss"
    assert intent.style == "limit"
    assert intent.stop_price == 98.0  # 100 * (1 - 0.02)
    # Closing a long is a sell → limit on the protective side BELOW the stop.
    assert intent.limit_price == 98.0 - 98.0 * 0.01  # 97.02
    assert intent.limit_price < intent.stop_price


def test_intent_carries_resolved_stop_for_short():
    rule = StopLossRule(pct=0.02, style="limit", limit_offset_pct=0.01)
    pos = PositionState(
        symbol="AAA",
        side="short",
        qty=100,
        entry_price=100.0,
        high_since_entry=105.0,
        low_since_entry=90.0,
    )
    # bar.high=103 crosses the 102 ceiling (100 * (1 + 0.02)).
    bars = {"AAA": BarSnapshot(high=103.0, low=99.0, close=101.0)}
    intent = evaluate_exit_rules([rule], {"AAA": pos}, bars)[0]
    assert intent.stop_price == 102.0  # 100 * (1 + 0.02)
    # Closing a short is a buy → limit on the protective side ABOVE the stop.
    assert intent.limit_price == 102.0 + 102.0 * 0.01  # 103.02
    assert intent.limit_price > intent.stop_price


def test_market_style_intent_has_no_stop_price():
    rule = StopLossRule(pct=0.02)
    pos = PositionState(
        symbol="AAA",
        side="long",
        qty=100,
        entry_price=100.0,
        high_since_entry=110.0,
        low_since_entry=95.0,
    )
    bars = {"AAA": BarSnapshot(high=105.0, low=97.0, close=99.0)}
    intent = evaluate_exit_rules([rule], {"AAA": pos}, bars)[0]
    assert intent.style == "market"
    assert intent.stop_price is None
    assert intent.limit_price is None


# ---------------------------------------------------------------------------
# _build_close_order: emits a protective-side STOP_LIMIT for a limit-style stop.
# ---------------------------------------------------------------------------


def test_emits_stop_limit_close_for_long_position():
    disp = _dispatcher(
        exit_rules=[StopLossRule(pct=0.02, style="limit", limit_offset_pct=0.01)]
    )
    tracker, portfolio, order_book = _long_setup()
    bar = _bar(low=95)  # trips the 98 floor
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

    engine = _engine_orders(pending)
    assert len(engine) == 1
    req = engine[0]
    assert req.order_type == OrderType.STOP_LIMIT
    assert req.side == OrderSide.SHORT  # closing a long sells
    assert req.stop_price == 98.0  # 100 * (1 - 0.02)
    assert req.limit_price == 98.0 * (1 - 0.01)  # protective: limit below stop
    assert req.limit_price <= req.stop_price
    assert req.tif == TimeInForce.GTC
    assert req.qty == 100.0
    # Emission counts as a firing; fills are counted separately at fill time.
    assert result.execution_diagnostics.exit_rule_firings.get("stop_loss") == 1


def test_emits_stop_limit_close_for_short_position():
    disp = _dispatcher(
        exit_rules=[StopLossRule(pct=0.02, style="limit", limit_offset_pct=0.01)]
    )
    tracker, portfolio, order_book = _short_setup()
    bar = _bar(high=105)  # trips the 102 ceiling
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

    req = _engine_orders(pending)[0]
    assert req.order_type == OrderType.STOP_LIMIT
    assert req.side == OrderSide.LONG  # closing a short buys
    assert req.stop_price == 102.0  # 100 * (1 + 0.02)
    assert req.limit_price == 102.0 * (1 + 0.01)  # protective: limit above stop
    assert req.limit_price >= req.stop_price


def test_emission_event_records_stop_limit_order_type():
    disp = _dispatcher(
        exit_rules=[StopLossRule(pct=0.02, style="limit", limit_offset_pct=0.01)]
    )
    tracker, portfolio, order_book = _long_setup()
    result = TradingServiceResult()
    pending: list[OrderRequest] = []

    disp.maybe_emit(
        cur_bar=_bar(low=95),
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )

    events = result.execution_diagnostics.last_order_events
    emitted = [e for e in events if e.event_type == "emitted"]
    assert emitted, "expected an 'emitted' lifecycle event"
    assert emitted[-1].order_type == OrderType.STOP_LIMIT.value


# ---------------------------------------------------------------------------
# Resting structured exit: a resting engine STOP_LIMIT stands the dispatcher
# down (no duplicate emission while it rests/latches across bars).
# ---------------------------------------------------------------------------


def test_does_not_re_emit_while_engine_stop_limit_rests():
    disp = _dispatcher(
        exit_rules=[StopLossRule(pct=0.02, style="limit", limit_offset_pct=0.01)]
    )
    tracker, portfolio, order_book = _long_setup()
    result = TradingServiceResult()

    # A prior bar's engine STOP_LIMIT is resting on the book (e.g. armed but
    # gapped through unfilled). It is opposite-side and engine-stamped.
    resting = OrderRequest(
        client_order_id="e1",
        symbol="AAA",
        side=OrderSide.SHORT,
        qty=100.0,
        order_type=OrderType.STOP_LIMIT,
        stop_price=98.0,
        limit_price=97.0,
        tif=TimeInForce.GTC,
        reason=f"{ENGINE_EXIT_REASON_PREFIX}stop_loss",
    )
    order_book.submit(resting, submitted_at="2024-01-09T00:00:00", submitted_equity=100_000.0)

    pending: list[OrderRequest] = []
    disp.maybe_emit(
        cur_bar=_bar(low=95),  # rule re-triggers, but the stop-limit rests
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )

    # No new engine emission — the resting structured exit owns the close.
    assert _engine_orders(pending) == []
    assert result.execution_diagnostics.exit_rule_firings.get("stop_loss") in (None, 0)


def test_fallback_limit_stop_emits_while_tighter_one_rests():
    """With two limit-style stops (tight + wider fallback), a resting STOP_LIMIT
    for the tight stop must NOT suppress the wider fallback when it triggers: only
    the limit stop whose own level is already resting is skipped, identified by
    stop price.
    """
    disp = _dispatcher(
        exit_rules=[
            StopLossRule(pct=0.02, style="limit", limit_offset_pct=0.01),  # tight, floor 98
            StopLossRule(pct=0.05, style="limit", limit_offset_pct=0.01),  # wider, floor 95
        ]
    )
    tracker, portfolio, order_book = _long_setup()
    result = TradingServiceResult()

    # The TIGHT stop's STOP_LIMIT (stop_price 98) is resting (gapped through).
    resting = OrderRequest(
        client_order_id="e1",
        symbol="AAA",
        side=OrderSide.SHORT,
        qty=100.0,
        order_type=OrderType.STOP_LIMIT,
        stop_price=98.0,
        limit_price=97.0,
        tif=TimeInForce.GTC,
        reason=f"{ENGINE_EXIT_REASON_PREFIX}stop_loss",
    )
    order_book.submit(resting, submitted_at="2024-01-09T00:00:00", submitted_equity=100_000.0)

    pending: list[OrderRequest] = []
    # low=94 trips BOTH floors (98 and 95). The tight stop (98) is already
    # resting → skipped; the wider fallback (95) is not → it must emit.
    disp.maybe_emit(
        cur_bar=_bar(low=94),
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )

    engine = _engine_orders(pending)
    assert len(engine) == 1
    req = engine[0]
    assert req.order_type == OrderType.STOP_LIMIT
    assert req.stop_price == 95.0  # the wider fallback, not the resting 98


def test_no_re_emit_when_all_limit_stops_already_rest():
    """When every triggered limit stop's level is already resting, nothing new is
    emitted (no duplicates)."""
    disp = _dispatcher(
        exit_rules=[
            StopLossRule(pct=0.02, style="limit", limit_offset_pct=0.01),  # floor 98
            StopLossRule(pct=0.05, style="limit", limit_offset_pct=0.01),  # floor 95
        ]
    )
    tracker, portfolio, order_book = _long_setup()
    result = TradingServiceResult()

    for cid, stop in (("e1", 98.0), ("e2", 95.0)):
        order_book.submit(
            OrderRequest(
                client_order_id=cid,
                symbol="AAA",
                side=OrderSide.SHORT,
                qty=100.0,
                order_type=OrderType.STOP_LIMIT,
                stop_price=stop,
                limit_price=stop - 1.0,
                tif=TimeInForce.GTC,
                reason=f"{ENGINE_EXIT_REASON_PREFIX}stop_loss",
            ),
            submitted_at="2024-01-09T00:00:00",
            submitted_equity=100_000.0,
        )

    pending: list[OrderRequest] = []
    disp.maybe_emit(
        cur_bar=_bar(low=94),  # both floors tripped, but both already rest
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )

    assert _engine_orders(pending) == []


def test_other_rule_still_emits_while_stop_limit_rests():
    """A resting limit-style stop-limit must NOT block a DIFFERENT, higher-priority
    rule. A take-profit that triggers while the stop-limit rests still emits its
    (market) close — its fill will retire the resting stop-limit via the binding.
    """
    # take_profit listed first so it wins when both could trigger; here only TP
    # triggers (price spiked up), the stop is resting from a prior gap-through.
    disp = _dispatcher(
        exit_rules=[
            TakeProfitRule(pct=0.05),
            StopLossRule(pct=0.02, style="limit", limit_offset_pct=0.01),
        ]
    )
    tracker, portfolio, order_book = _long_setup()
    result = TradingServiceResult()

    resting = OrderRequest(
        client_order_id="e1",
        symbol="AAA",
        side=OrderSide.SHORT,
        qty=100.0,
        order_type=OrderType.STOP_LIMIT,
        stop_price=98.0,
        limit_price=97.0,
        tif=TimeInForce.GTC,
        reason=f"{ENGINE_EXIT_REASON_PREFIX}stop_loss",
    )
    order_book.submit(resting, submitted_at="2024-01-09T00:00:00", submitted_equity=100_000.0)

    pending: list[OrderRequest] = []
    # high=106 clears the 105 take-profit target (entry 100, +5%); low stays
    # above the stop floor so only the take-profit triggers this bar.
    disp.maybe_emit(
        cur_bar=_bar(high=106, low=104),
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )

    engine = _engine_orders(pending)
    assert len(engine) == 1
    assert engine[0].order_type == OrderType.MARKET  # take-profit market close
    assert f"{ENGINE_EXIT_REASON_PREFIX}take_profit" == engine[0].reason
    assert result.execution_diagnostics.exit_rule_firings.get("take_profit") == 1


def test_lower_priority_rule_emits_when_limit_stop_first_and_both_trigger():
    """Starvation guard: when the limit stop is listed BEFORE another rule and a
    wide bar triggers both while the stop-limit rests, the lower rule must still
    emit. The dispatcher skips the already-resting limit stop during evaluation
    rather than standing the whole bar down (which would suppress the take-profit
    on every bar the stop re-triggers).
    """
    disp = _dispatcher(
        exit_rules=[
            StopLossRule(pct=0.02, style="limit", limit_offset_pct=0.01),  # first
            TakeProfitRule(pct=0.05),  # lower priority
        ]
    )
    tracker, portfolio, order_book = _long_setup()
    result = TradingServiceResult()

    resting = OrderRequest(
        client_order_id="e1",
        symbol="AAA",
        side=OrderSide.SHORT,
        qty=100.0,
        order_type=OrderType.STOP_LIMIT,
        stop_price=98.0,
        limit_price=97.0,
        tif=TimeInForce.GTC,
        reason=f"{ENGINE_EXIT_REASON_PREFIX}stop_loss",
    )
    order_book.submit(resting, submitted_at="2024-01-09T00:00:00", submitted_equity=100_000.0)

    pending: list[OrderRequest] = []
    # Wide bar: low=95 re-triggers the stop (floor 98) AND high=106 clears the
    # take-profit target (105). The stop is first in spec order, so without the
    # skip the stop intent would win and the bar would be suppressed.
    disp.maybe_emit(
        cur_bar=_bar(high=106, low=95),
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )

    engine = _engine_orders(pending)
    assert len(engine) == 1
    assert engine[0].reason == f"{ENGINE_EXIT_REASON_PREFIX}take_profit"


# ---------------------------------------------------------------------------
# Competing-order retirement: a limit-style stop still BINDS competing exits
# (binding only retires them once the position actually closes), so an unbound
# resting exit can't survive the close and open a reverse position. Only the
# active entry-continuation cancel is deferred (it assumes the close fills).
# ---------------------------------------------------------------------------


def _resting_take_profit(order_book) -> object:
    resting = OrderRequest(
        client_order_id="c1",
        symbol="AAA",
        side=OrderSide.SHORT,
        qty=100.0,
        order_type=OrderType.LIMIT,
        limit_price=200.0,
        tif=TimeInForce.GTC,
        reason="strategy_take_profit",
    )
    return order_book.submit(
        resting, submitted_at="2024-01-05T00:00:00", submitted_equity=100_000.0
    )


def test_limit_style_binds_competing_resting_orders():
    disp = _dispatcher(
        exit_rules=[StopLossRule(pct=0.02, style="limit", limit_offset_pct=0.01)]
    )
    tracker, portfolio, order_book = _long_setup()
    result = TradingServiceResult()

    resting_po = _resting_take_profit(order_book)
    assert resting_po.working_against_entry_order_id is None

    disp.maybe_emit(
        cur_bar=_bar(low=95),
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=[],
        order_book=order_book,
        result=result,
    )

    # The competing take-profit is now bound to the position. When the
    # stop-limit eventually fills and closes the position, the stale-continuation
    # guard drops the bound take-profit — without this it would survive the
    # close and later fire as a fresh reverse short. (Binding does not retire it
    # early: while the position stays open the guard leaves a bound order alone.)
    assert resting_po.working_against_entry_order_id == "o1"


def test_market_style_binds_competing_resting_orders():
    disp = _dispatcher(exit_rules=[StopLossRule(pct=0.02)])
    tracker, portfolio, order_book = _long_setup()
    result = TradingServiceResult()

    resting_po = _resting_take_profit(order_book)

    disp.maybe_emit(
        cur_bar=_bar(low=95),
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=[],
        order_book=order_book,
        result=result,
    )

    assert resting_po.working_against_entry_order_id == "o1"


def _partial_entry_continuation(order_book):
    """A partially-filled entry whose REQUEUE remainder is still on the book —
    a continuation of the position's own entry order (id ``o1``)."""
    entry = OrderRequest(
        client_order_id="c-o1",
        symbol="AAA",
        side=OrderSide.LONG,
        qty=100.0,
        order_type=OrderType.MARKET,
        tif=TimeInForce.DAY,
        unfilled_policy=UnfilledPolicy.REQUEUE_NEXT_BAR,
        reason="strategy_entry",
    )
    po = order_book.submit(entry, submitted_at="2024-01-08T00:00:00", submitted_equity=100_000.0)
    po.order_id = "o1"  # matches the position's entry_order_id
    po.cumulative_filled_qty = 60.0  # partially filled; 40 remainder requeued
    # _fill_entry self-binds a partially-filled entry to its own id, which makes
    # _sum_same_side_resting exclude it from the close oversize — so the close
    # must pick up the remainder via _sum_entry_continuation_remainder instead.
    po.working_against_entry_order_id = "o1"
    return po


def test_market_style_cancels_entry_continuation():
    disp = _dispatcher(exit_rules=[StopLossRule(pct=0.02)])
    tracker, portfolio, order_book = _long_setup()
    result = TradingServiceResult()
    _partial_entry_continuation(order_book)

    disp.maybe_emit(
        cur_bar=_bar(low=95),
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=[],
        order_book=order_book,
        result=result,
    )

    # The guaranteed market close cancels the in-flight entry remainder so the
    # position can't grow past what the close covers.
    assert "o1" not in order_book


def test_limit_style_does_not_cancel_entry_continuation():
    disp = _dispatcher(
        exit_rules=[StopLossRule(pct=0.02, style="limit", limit_offset_pct=0.01)]
    )
    tracker, portfolio, order_book = _long_setup()
    result = TradingServiceResult()
    _partial_entry_continuation(order_book)

    disp.maybe_emit(
        cur_bar=_bar(low=95),
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=[],
        order_book=order_book,
        result=result,
    )

    # The limit-style stop may gap through unfilled, so the entry continuation
    # is NOT cancelled at emission — the scale-in is preserved for the still-open
    # position.
    assert "o1" in order_book


def test_limit_style_oversizes_close_to_cover_entry_continuation():
    """Because the entry continuation is not cancelled for a limit-style stop, the
    close is oversized to cover its still-unfilled remainder. If the continuation
    fills before the stop-limit, the close still covers the grown position
    (_fill_exit clips to live qty), so no residual exposure is stranded.
    """
    disp = _dispatcher(
        exit_rules=[StopLossRule(pct=0.02, style="limit", limit_offset_pct=0.01)]
    )
    tracker, portfolio, order_book = _long_setup()
    result = TradingServiceResult()
    _partial_entry_continuation(order_book)  # 40-share remainder still working

    pending: list[OrderRequest] = []
    disp.maybe_emit(
        cur_bar=_bar(low=95),
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )

    req = _engine_orders(pending)[0]
    # pos.qty (100) + continuation remainder (100 - 60 = 40) = 140, counted once
    # (the self-bound continuation is excluded from _sum_same_side_resting).
    assert req.qty == 140.0


def test_market_style_does_not_oversize_for_entry_continuation():
    """Control: the market path cancels the continuation instead of oversizing,
    so its close stays at pos.qty."""
    disp = _dispatcher(exit_rules=[StopLossRule(pct=0.02)])
    tracker, portfolio, order_book = _long_setup()
    result = TradingServiceResult()
    _partial_entry_continuation(order_book)

    pending: list[OrderRequest] = []
    disp.maybe_emit(
        cur_bar=_bar(low=95),
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )

    req = _engine_orders(pending)[0]
    assert req.qty == 100.0
    assert "o1" not in order_book  # continuation cancelled
