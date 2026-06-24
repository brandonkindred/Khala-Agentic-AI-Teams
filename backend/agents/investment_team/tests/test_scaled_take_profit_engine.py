"""Engine-side tests for laddered (scaled) take-profit scale-outs.

Two layers:

* **Dispatcher** (``_EngineExitDispatcher.maybe_emit``) — a scaled rung emits a
  PARTIAL market close sized ``qty_fraction * original_qty``, leaves the position
  open (no continuation cancel / competing-order retirement), fires each rung at
  most once per position, and records per-rung diagnostics. A stop listed ahead
  of the ladder still wins a full close.
* **Fill simulator** — a partial MARKET close reduces the position qty and keeps
  it open (no ``TradeRecord`` until the remainder closes), under BOTH execution
  models.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Dict, Literal, get_type_hints

import pytest

from investment_team.execution.bar_safety import BarSafetyAssertion
from investment_team.execution.risk_filter import RiskFilter, RiskLimits
from investment_team.strategy_lab.executor.rule_compiler import PositionState
from investment_team.strategy_lab.spec_dsl import ScaledTakeProfitRule, StopLossRule
from investment_team.trading_service.engine.execution_model import (
    OptimisticExecutionModel,
    RealisticExecutionModel,
)
from investment_team.trading_service.engine.fill_simulator import (
    ENGINE_EXIT_REASON_PREFIX,
    FillSimulator,
    FillSimulatorConfig,
)
from investment_team.trading_service.engine.order_book import OrderBook, PendingOrder
from investment_team.trading_service.engine.portfolio import Portfolio, Position
from investment_team.trading_service.service import (
    TradingService,
    TradingServiceResult,
    _engine_exit_kind,
    _EngineExitDispatcher,
    _PositionStateView,
    _ScaledLadderCursor,
    _TrackedPosition,
)
from investment_team.trading_service.strategy.contract import (
    Bar,
    OrderRequest,
    OrderSide,
    OrderType,
    TimeInForce,
    UnfilledPolicy,
)


@dataclass
class _MockBar:
    """Minimal structural bar exposing exactly the fields the dispatcher reads
    (``symbol``/``timestamp``/``high``/``low``/``close``).

    The dispatcher is duck-typed over the bar — it never touches ``open`` /
    ``volume`` / ``timeframe`` — so a stand-in keeps these unit tests focused on
    rule/cursor behavior without an unrelated price series. The REAL ``Bar``
    contract (open/volume/timeframe and all) is exercised end-to-end by the
    ``FillSimulator`` tests below via :func:`_full_bar`, so a future dispatcher
    dependence on a ``Bar``-only field would surface there.
    """

    symbol: str
    timestamp: str
    high: float
    low: float
    close: float


def _ladder() -> ScaledTakeProfitRule:
    """A two-rung ladder for tests: 50% at +5%, 30% at +10% (sums to 0.8)."""
    return ScaledTakeProfitRule(
        levels=[{"pct": 0.05, "qty_fraction": 0.5}, {"pct": 0.10, "qty_fraction": 0.3}]
    )


def _dispatcher(*, exit_rules) -> _EngineExitDispatcher:
    """An ``_EngineExitDispatcher`` over ``exit_rules`` with empty bindings."""
    return _EngineExitDispatcher(exit_rules=exit_rules, engine_exit_bindings={})


def _portfolio_with(
    *, side: OrderSide, qty: float, entry_price: float, entry_order_id: str = "o1"
) -> Portfolio:
    """A portfolio holding a single open ``AAA`` position (``original_qty == qty``)."""
    p = Portfolio(initial_capital=1_000_000.0)
    p.positions["AAA"] = Position(
        symbol="AAA",
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


def _tracker(
    side: OrderSide, entry_price: float = 100.0, entry_order_id: str = "o1"
) -> Dict[str, _TrackedPosition]:
    """A position-tracker map with one settled ``AAA`` tracker (watermarks at entry)."""
    return {
        "AAA": _TrackedPosition(
            side=side,
            entry_price=entry_price,
            entry_order_id=entry_order_id,
            just_opened=False,
            high_since_entry=entry_price,
            low_since_entry=entry_price,
        )
    }


def _bar(**kw) -> _MockBar:
    """A mock bar for ``AAA`` (flat at 100 unless overridden via kwargs)."""
    defaults = {
        "symbol": "AAA",
        "timestamp": "2024-01-10T00:00:00",
        "high": 100.0,
        "low": 100.0,
        "close": 100.0,
    }
    defaults.update(kw)
    return _MockBar(**defaults)


def test_cursor_advance_rejects_out_of_order_rung() -> None:
    """advance() enforces fire-in-cursor-order with an explicit raise (holds under -O)."""
    cursor = _ScaledLadderCursor()
    cursor.advance(0, 0)  # rung 0 fires → cursor at 1
    assert cursor.mapping == {0: 1}
    with pytest.raises(ValueError, match="cursor order"):
        cursor.advance(0, 0)  # rung 0 again (cursor is 1) → rejected
    with pytest.raises(ValueError, match="cursor order"):
        cursor.advance(0, 5)  # skip ahead → rejected
    cursor.advance(0, 1)  # the in-order rung still advances
    assert cursor.mapping == {0: 2}


def test_cursor_mapping_is_read_only_but_live() -> None:
    """mapping is a read-only view: mutation raises, but it still reflects a later
    advance() (a live proxy, not a snapshot copy)."""
    cursor = _ScaledLadderCursor()
    view = cursor.mapping
    # The ``type: ignore`` is intentional: this line asserts the RUNTIME ``TypeError``
    # a read-only ``MappingProxyType`` raises on item assignment — it is the behavior
    # under test, not a static type violation to be fixed.
    with pytest.raises(TypeError):
        view[0] = 99  # type: ignore[index]
    cursor.advance(0, 0)
    assert view == {0: 1}  # the same view reflects the advance


def test_tracked_position_caches_side_conversions() -> None:
    """side_str (evaluator input) and close_side (engine close) are derived once."""
    long_t = _tracker(OrderSide.LONG)["AAA"]
    short_t = _tracker(OrderSide.SHORT)["AAA"]
    assert long_t.side_str == "long" and long_t.close_side == OrderSide.SHORT
    assert short_t.side_str == "short" and short_t.close_side == OrderSide.LONG


def test_snapshot_reuses_one_view_and_reflects_updates() -> None:
    """The hot path reuses a single evaluator view, mutated in place each bar."""
    tracked = _tracker(OrderSide.LONG, entry_price=100.0)["AAA"]
    first = tracked.snapshot("AAA", 50.0)
    assert (first.symbol, first.side, first.qty, first.entry_price) == ("AAA", "long", 50.0, 100.0)
    tracked.high_since_entry = 110.0
    second = tracked.snapshot("AAA", 40.0)
    assert second is first  # same instance reused, not reallocated
    assert second.qty == 40.0 and second.high_since_entry == 110.0


def test_snapshot_reuse_refreshes_entry_price_after_scale_in() -> None:
    """entry_price is NOT fixed: a scale-in refreshes the tracker's weighted-average
    entry in place (see _update_position_tracker). The reused view must reflect it,
    else take-profit / stop / scaled-rung targets evaluate against a stale entry."""
    tracked = _tracker(OrderSide.LONG, entry_price=100.0)["AAA"]
    first = tracked.snapshot("AAA", 100.0)
    assert first.entry_price == 100.0
    tracked.entry_price = 102.0  # scale-in weighted-average refresh
    second = tracked.snapshot("AAA", 150.0)
    assert second is first
    assert second.entry_price == 102.0  # reused view sees the new basis


def test_position_state_view_matches_position_state_fields() -> None:
    """_PositionStateView is a mutable structural twin of PositionState consumed by
    the evaluator across a duck-typed boundary. If a field is added/renamed/retyped
    on one and not the other, the reused hot-path view silently feeds the evaluator a
    missing/stale/wrong-typed field with no type error. Lock the two shapes together
    so drift fails loudly here instead.

    Both field NAMES (and order) and their resolved TYPES must match — so a
    ``float``↔``str`` swap is caught, not just an add/rename — with ONE deliberate
    exception: the view widens ``side`` from ``Literal["long", "short"]`` to plain
    ``str``. The dispatcher has already validated the side before building the view,
    so the read-only view need not re-narrow it; that single widening is asserted
    explicitly rather than waved through."""
    view_fields = [f.name for f in dataclasses.fields(_PositionStateView)]
    canonical_fields = [f.name for f in dataclasses.fields(PositionState)]
    assert view_fields == canonical_fields  # same names, same order
    # Resolve string annotations (``from __future__ import annotations``) to real
    # types so the comparison is robust to annotation formatting.
    view_types = get_type_hints(_PositionStateView)
    canonical_types = get_type_hints(PositionState)
    # The one intentional divergence: ``side`` is widened Literal -> str in the view.
    assert view_types["side"] is str
    assert canonical_types["side"] == Literal["long", "short"]
    # Every OTHER field must match by type, so a str/float swap can't slip through.
    for name in view_fields:
        if name == "side":
            continue
        assert view_types[name] == canonical_types[name], name


# ---------------------------------------------------------------------------
# Dispatcher: partial-close sizing + at-most-once firing + diagnostics.
# ---------------------------------------------------------------------------


def test_first_rung_emits_partial_close_sized_to_original_qty() -> None:
    """The first rung of a scaled ladder emits a PARTIAL market close sized to
    qty_fraction * original_qty (50 of 100), leaving the position open."""
    disp = _dispatcher(exit_rules=[_ladder()])
    tracker = _tracker(OrderSide.LONG)
    portfolio = _portfolio_with(side=OrderSide.LONG, qty=100.0, entry_price=100.0)
    order_book = OrderBook()
    result = TradingServiceResult()
    pending: list[OrderRequest] = []

    disp.maybe_emit(
        cur_bar=_bar(high=106.0, low=100.0, close=105.0),  # +5% only
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )

    assert len(pending) == 1
    req = pending[0]
    assert req.order_type == OrderType.MARKET
    assert req.side == OrderSide.SHORT  # closing a long sells
    assert req.qty == 50.0  # 0.5 * original_qty(100)
    assert req.reason == f"{ENGINE_EXIT_REASON_PREFIX}scaled_take_profit"
    assert tracker["AAA"].scaled_cursor.mapping == {0: 1}  # rung 0 fired → cursor advanced
    diag = result.execution_diagnostics
    assert diag.exit_rule_firings.get("scaled_take_profit") == 1
    assert diag.scaled_take_profit_level_firings == {"0:0": 1}


def test_each_rung_fires_at_most_once_and_in_order() -> None:
    """Three successive ``maybe_emit`` calls (standing in for three consecutive
    bars with identical price data — the engine calls ``maybe_emit`` once per bar)
    fire each rung exactly once in cursor order (0.5 then 0.3 of original_qty),
    then nothing; per-rung diagnostics record both firings and the cursor walks
    off the end."""
    disp = _dispatcher(exit_rules=[_ladder()])
    tracker = _tracker(OrderSide.LONG)
    portfolio = _portfolio_with(side=OrderSide.LONG, qty=100.0, entry_price=100.0)
    order_book = OrderBook()
    result = TradingServiceResult()
    bar = _bar(high=111.0, low=100.0, close=110.0)  # crosses both rungs

    # Three successive evaluations on the same bar: rung 0, then rung 1, then none.
    seen = []
    for _ in range(3):
        pending: list[OrderRequest] = []
        disp.maybe_emit(
            cur_bar=bar,
            position_tracker=tracker,
            portfolio=portfolio,
            pending_for_prev=pending,
            order_book=order_book,
            result=result,
        )
        seen.append([r.qty for r in pending])

    assert seen == [[50.0], [30.0], []]  # 0.5*100, then 0.3*100, then exhausted
    assert tracker["AAA"].scaled_cursor.mapping == {0: 2}  # both rungs fired → cursor off the end
    assert result.execution_diagnostics.scaled_take_profit_level_firings == {"0:0": 1, "0:1": 1}


def test_single_call_on_multi_rung_bar_fires_only_first_rung() -> None:
    """One ``maybe_emit`` call on a bar that crosses BOTH rungs emits only the
    lowest un-fired rung (one tranche per bar — the documented limitation), advancing
    the cursor by one and recording a single rung firing."""
    disp = _dispatcher(exit_rules=[_ladder()])
    tracker = _tracker(OrderSide.LONG)
    portfolio = _portfolio_with(side=OrderSide.LONG, qty=100.0, entry_price=100.0)
    order_book = OrderBook()
    result = TradingServiceResult()
    pending: list[OrderRequest] = []
    disp.maybe_emit(
        cur_bar=_bar(high=111.0, low=100.0, close=110.0),  # crosses +5% AND +10%
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )
    assert len(pending) == 1
    assert pending[0].qty == 50.0  # only rung 0 (0.5 * original_qty), not both rungs
    assert tracker["AAA"].scaled_cursor.mapping == {0: 1}  # advanced by exactly one
    assert result.execution_diagnostics.scaled_take_profit_level_firings == {"0:0": 1}


def test_partial_scale_out_does_not_cancel_competing_resting_order() -> None:
    """A resting opposite-side protective order must survive a partial scale-out —
    the remainder of the position still needs it. (A full close would retire it.)"""
    disp = _dispatcher(exit_rules=[_ladder()])
    tracker = _tracker(OrderSide.LONG)
    portfolio = _portfolio_with(side=OrderSide.LONG, qty=100.0, entry_price=100.0)
    order_book = OrderBook()
    order_book.submit(
        OrderRequest(
            client_order_id="rest-stop",
            symbol="AAA",
            side=OrderSide.SHORT,
            qty=100.0,
            order_type=OrderType.STOP,
            stop_price=90.0,
            tif=TimeInForce.GTC,
            reason=f"{ENGINE_EXIT_REASON_PREFIX}stop_loss",
        ),
        submitted_at="2024-01-09",
        submitted_equity=1_000_000.0,
    )
    result = TradingServiceResult()
    disp.maybe_emit(
        cur_bar=_bar(high=106.0, low=100.0, close=105.0),
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=[],
        order_book=order_book,
        result=result,
    )
    # The resting protective stop is still on the book (not cancelled/retired).
    assert any(
        po.request.client_order_id == "rest-stop" for po in order_book.pending_for_symbol("AAA")
    )


def test_partial_scale_out_does_not_cancel_resting_limit_stop() -> None:
    """A spec with BOTH a limit-style stop and a ladder: when the engine's resting
    STOP_LIMIT is on the book and a scale-out rung fires, the partial close must NOT
    cancel the stop — the remainder of the position still needs it. (A full close,
    by contrast, cancels the now-redundant resting stop-limit.)"""
    disp = _dispatcher(
        exit_rules=[StopLossRule(pct=0.04, style="limit", limit_offset_pct=0.01), _ladder()]
    )
    tracker = _tracker(OrderSide.LONG)
    portfolio = _portfolio_with(side=OrderSide.LONG, qty=100.0, entry_price=100.0)
    order_book = OrderBook()
    # The engine's resting protective STOP_LIMIT (closes the long; limit below stop).
    order_book.submit(
        OrderRequest(
            client_order_id="rest-stoplimit",
            symbol="AAA",
            side=OrderSide.SHORT,
            qty=100.0,
            order_type=OrderType.STOP_LIMIT,
            stop_price=96.0,
            limit_price=95.0,
            tif=TimeInForce.GTC,
            reason=f"{ENGINE_EXIT_REASON_PREFIX}stop_loss",
        ),
        submitted_at="2024-01-09",
        submitted_equity=1_000_000.0,
    )
    result = TradingServiceResult()
    pending: list[OrderRequest] = []
    # Bar reaches the +5% rung target (high 106) but stays above the 96 stop floor
    # (low 100), so only the scale-out rung fires while the STOP_LIMIT rests.
    disp.maybe_emit(
        cur_bar=_bar(high=106.0, low=100.0, close=105.0),
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )
    # The rung scaled out (50% partial close). The ladder is exit_rules[1] (the
    # limit-stop is [0]), so its cursor is keyed by rule_index 1.
    assert [r.qty for r in pending] == [50.0]
    assert tracker["AAA"].scaled_cursor.mapping == {1: 1}
    # ...and the resting protective STOP_LIMIT is STILL on the book — not cancelled.
    assert any(
        po.request.client_order_id == "rest-stoplimit"
        for po in order_book.pending_for_symbol("AAA")
    )


def test_stop_loss_listed_first_takes_full_close_over_ladder() -> None:
    """When a stop-loss is listed ahead of the ladder and both trigger, the
    higher-priority stop wins and emits a FULL-position close, not a rung."""
    disp = _dispatcher(exit_rules=[StopLossRule(pct=0.03), _ladder()])
    tracker = _tracker(OrderSide.LONG)
    portfolio = _portfolio_with(side=OrderSide.LONG, qty=100.0, entry_price=100.0)
    order_book = OrderBook()
    result = TradingServiceResult()
    pending: list[OrderRequest] = []
    disp.maybe_emit(
        cur_bar=_bar(high=106.0, low=96.0, close=100.0),  # trips stop (97) AND +5%
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )
    assert len(pending) == 1
    assert pending[0].reason == f"{ENGINE_EXIT_REASON_PREFIX}stop_loss"
    assert pending[0].qty == 100.0  # full close
    assert tracker["AAA"].scaled_cursor.mapping == {}  # ladder did not fire


def test_ladder_listed_first_fires_partial_over_same_bar_stop() -> None:
    """Reverse priority: with the ladder listed AHEAD of the stop and the entry
    already settled (rung NOT deferred), a bar that both reaches the first rung and
    breaches the stop fires the higher-priority rung as a PARTIAL close — the stop is
    suppressed for this bar (it stays working for the remainder on later bars). The
    contrast to ``test_deferred_scale_out_still_lets_a_same_bar_stop_fire``, where an
    in-flight entry continuation defers the rung and lets the same-bar stop win."""
    disp = _dispatcher(exit_rules=[_ladder(), StopLossRule(pct=0.03)])
    tracker = _tracker(OrderSide.LONG)  # entry settled — no continuation in flight
    portfolio = _portfolio_with(side=OrderSide.LONG, qty=100.0, entry_price=100.0)
    order_book = OrderBook()
    result = TradingServiceResult()
    pending: list[OrderRequest] = []
    disp.maybe_emit(
        cur_bar=_bar(high=106.0, low=96.0, close=100.0),  # +5% rung AND -3% stop (97)
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )
    assert len(pending) == 1
    assert pending[0].reason == f"{ENGINE_EXIT_REASON_PREFIX}scaled_take_profit"
    assert pending[0].qty == 50.0  # partial rung, NOT the full-close stop
    assert pending[0].side == OrderSide.SHORT
    assert tracker["AAA"].scaled_cursor.mapping == {0: 1}  # rung fired, cursor advanced
    # The stop did not fire this bar (only the rung emitted).
    assert result.execution_diagnostics.exit_rule_firings.get("stop_loss") is None


def test_short_first_rung_emits_partial_buy_close() -> None:
    """Symmetric for shorts: a rung's target is reached on bar.low, and the
    scale-out is a partial BUY close sized to qty_fraction * original_qty."""
    disp = _dispatcher(exit_rules=[_ladder()])
    tracker = _tracker(OrderSide.SHORT)
    portfolio = _portfolio_with(side=OrderSide.SHORT, qty=100.0, entry_price=100.0)
    order_book = OrderBook()
    result = TradingServiceResult()
    pending: list[OrderRequest] = []
    disp.maybe_emit(
        cur_bar=_bar(high=100.0, low=94.0, close=95.0),  # -5% (short profit)
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )
    assert len(pending) == 1
    assert pending[0].side == OrderSide.LONG  # closing a short buys
    assert pending[0].qty == 50.0


def test_scaled_close_requeues_capped_remainder() -> None:
    """A fire-once rung can't re-emit, so its market close must requeue any
    participation-capped remainder (unlike a full-position exit, which re-fires next
    bar). Assert the emitted scale-out carries REQUEUE_NEXT_BAR."""
    disp = _dispatcher(exit_rules=[_ladder()])
    tracker = _tracker(OrderSide.LONG)
    portfolio = _portfolio_with(side=OrderSide.LONG, qty=100.0, entry_price=100.0)
    order_book = OrderBook()
    result = TradingServiceResult()
    pending: list[OrderRequest] = []
    disp.maybe_emit(
        cur_bar=_bar(high=106.0, low=100.0, close=105.0),
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )
    assert pending[0].unfilled_policy == UnfilledPolicy.REQUEUE_NEXT_BAR


def test_scale_out_deferred_while_entry_continuation_resting_then_fires_full_size() -> None:
    """A partial entry (50 of 100 filled) with a REQUEUE_NEXT_BAR continuation still
    resting: pos.original_qty is only 50 so far and the fill simulator will BUMP it
    to 100 once the rest fills. A rung firing now would close 0.5*50=25 and be marked
    fired, stranding the catch-up. The dispatcher must DEFER until the entry settles,
    then close 0.5*100=50."""
    disp = _dispatcher(exit_rules=[_ladder()])
    order_book = OrderBook()
    # Submit the entry order; only 50 of 100 filled, 50 still working as a
    # continuation. Pin the tracked position to whatever order_id the book assigned
    # (no hardcoded id) so ``_entry_continuations`` matches it as the position's own
    # in-flight entry.
    cont = order_book.submit(
        OrderRequest(
            client_order_id="entry-AAA",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=100.0,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
        ),
        submitted_at="2024-01-09",
        submitted_equity=1_000_000.0,
    )
    cont.cumulative_filled_qty = 50.0  # 50 filled, 50 still working
    tracker = _tracker(OrderSide.LONG, entry_order_id=cont.order_id)
    portfolio = _portfolio_with(
        side=OrderSide.LONG, qty=50.0, entry_price=100.0, entry_order_id=cont.order_id
    )
    portfolio.positions["AAA"].original_qty = 50.0  # only the first slice so far
    result = TradingServiceResult()
    bar = _bar(high=106.0, low=100.0, close=105.0)  # +5% target reached

    pending: list[OrderRequest] = []
    disp.maybe_emit(
        cur_bar=bar,
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )
    # Deferred: nothing emitted and the cursor is NOT advanced.
    assert pending == []
    assert tracker["AAA"].scaled_cursor.mapping == {}

    # Entry settles: continuation leaves the book, original_qty now reflects 100.
    order_book.remove(cont.order_id, was_filled=True)
    portfolio.positions["AAA"].original_qty = 100.0
    portfolio.positions["AAA"].qty = 100.0
    pending = []
    disp.maybe_emit(
        cur_bar=bar,
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )
    assert [r.qty for r in pending] == [50.0]  # 0.5 * 100, not 0.5 * 50
    assert tracker["AAA"].scaled_cursor.mapping == {0: 1}


def test_deferred_scale_out_still_lets_a_same_bar_stop_fire() -> None:
    """While the entry continuation is in flight the scaled rung is deferred — but a
    lower-priority full-position stop (listed AFTER the ladder) must still fire this
    bar rather than being suppressed along with the deferred rung."""
    disp = _dispatcher(exit_rules=[_ladder(), StopLossRule(pct=0.03)])
    order_book = OrderBook()
    cont = order_book.submit(
        OrderRequest(
            client_order_id="entry-AAA",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=100.0,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
        ),
        submitted_at="2024-01-09",
        submitted_equity=1_000_000.0,
    )
    cont.cumulative_filled_qty = 50.0  # entry still filling → rung deferred
    tracker = _tracker(OrderSide.LONG, entry_order_id=cont.order_id)
    portfolio = _portfolio_with(
        side=OrderSide.LONG, qty=50.0, entry_price=100.0, entry_order_id=cont.order_id
    )
    portfolio.positions["AAA"].original_qty = 50.0
    result = TradingServiceResult()
    # Bar reaches the +5% rung (high>=105) AND breaches the 3% stop (low<=97).
    bar = _bar(high=106.0, low=96.0, close=98.0)

    pending: list[OrderRequest] = []
    disp.maybe_emit(
        cur_bar=bar,
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )
    # The stop fired (full close) even though the rung was deferred; the ladder
    # cursor did NOT advance (the rung is still pending for a settled-entry bar).
    assert len(pending) == 1
    assert "stop_loss" in pending[0].reason
    assert tracker["AAA"].scaled_cursor.mapping == {}


def _submit_inflight_scaled_partial(order_book: OrderBook) -> PendingOrder:
    """Submit a prior scaled rung's market scale-out (a PARTIAL close, closing-side
    SHORT, carrying the structural ``engine_scaled_partial`` flag) still resting on
    the book — e.g. a participation-capped rung requeued across bars. This is the
    in-flight state the gate must recognise as a partial (defer the next rung)
    rather than a full close (stand the whole bar down)."""
    return order_book.submit(
        OrderRequest(
            client_order_id="e1",
            symbol="AAA",
            side=OrderSide.SHORT,
            qty=50.0,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
            reason=f"{ENGINE_EXIT_REASON_PREFIX}scaled_take_profit",
            unfilled_policy=UnfilledPolicy.REQUEUE_NEXT_BAR,
            engine_scaled_partial=True,
        ),
        submitted_at="2024-01-09",
        submitted_equity=1_000_000.0,
    )


def test_inflight_partial_scale_out_does_not_block_runner_stop() -> None:
    """A prior rung's PARTIAL scale-out still in flight must NOT stand the whole bar
    down: a stop protecting the runner (listed after the ladder) still has to fire
    this bar. Regression — the in-flight-engine-MARKET standdown previously suppressed
    every exit, leaving the runner unprotected until the partial cleared."""
    disp = _dispatcher(exit_rules=[_ladder(), StopLossRule(pct=0.03)])
    tracker = _tracker(OrderSide.LONG)
    tracker["AAA"].scaled_cursor.advance(0, 0)  # rung 0 already fired → cursor at 1
    # The runner: 50 of the original 100 remains after rung 0 scaled out half.
    portfolio = _portfolio_with(side=OrderSide.LONG, qty=50.0, entry_price=100.0)
    portfolio.positions["AAA"].original_qty = 100.0
    order_book = OrderBook()
    _submit_inflight_scaled_partial(order_book)  # rung 0's scale-out still pending
    result = TradingServiceResult()
    # Bar both reaches rung 1 (+10%, high>=110) AND breaches the 3% stop (low<=97).
    bar = _bar(high=111.0, low=96.0, close=98.0)

    pending: list[OrderRequest] = []
    disp.maybe_emit(
        cur_bar=bar,
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )
    # The stop fired a full close on the runner even though a partial was in flight;
    # the NEXT rung stayed deferred (cursor unchanged at 1), preserving the
    # one-rung-at-a-time ordering the standdown used to give.
    assert len(pending) == 1
    assert "stop_loss" in pending[0].reason
    assert pending[0].qty == 50.0  # full close of the 50-share runner
    assert tracker["AAA"].scaled_cursor.mapping == {0: 1}  # rung 1 NOT fired


def test_inflight_partial_scale_out_defers_next_rung_without_a_full_exit() -> None:
    """With only the ladder (no stop), an in-flight partial scale-out simply defers
    the next rung: nothing new emits and the cursor does not advance, so each rung
    still completes before the next fires."""
    disp = _dispatcher(exit_rules=[_ladder()])
    tracker = _tracker(OrderSide.LONG)
    tracker["AAA"].scaled_cursor.advance(0, 0)  # rung 0 fired
    portfolio = _portfolio_with(side=OrderSide.LONG, qty=50.0, entry_price=100.0)
    portfolio.positions["AAA"].original_qty = 100.0
    order_book = OrderBook()
    _submit_inflight_scaled_partial(order_book)
    result = TradingServiceResult()
    bar = _bar(high=111.0, low=100.0, close=110.0)  # rung 1's +10% target reached

    pending: list[OrderRequest] = []
    disp.maybe_emit(
        cur_bar=bar,
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )
    assert pending == []  # rung 1 deferred until the in-flight partial clears
    assert tracker["AAA"].scaled_cursor.mapping == {0: 1}


def test_inflight_full_close_still_stands_the_bar_down() -> None:
    """The fix is scoped to scaled PARTIALs: an in-flight FULL close (a stop's market,
    not a scaled-rung reason) must still stand the whole bar down so the engine does
    not stack a redundant guaranteed close while the rule keeps re-triggering."""
    disp = _dispatcher(exit_rules=[StopLossRule(pct=0.03), _ladder()])
    tracker = _tracker(OrderSide.LONG)
    portfolio = _portfolio_with(side=OrderSide.LONG, qty=100.0, entry_price=100.0)
    order_book = OrderBook()
    order_book.submit(
        OrderRequest(
            client_order_id="e1",
            symbol="AAA",
            side=OrderSide.SHORT,
            qty=100.0,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
            reason=f"{ENGINE_EXIT_REASON_PREFIX}stop_loss",  # a FULL close, in flight
        ),
        submitted_at="2024-01-09",
        submitted_equity=1_000_000.0,
    )
    result = TradingServiceResult()
    bar = _bar(high=111.0, low=96.0, close=98.0)  # would re-trigger both stop and rung

    pending: list[OrderRequest] = []
    disp.maybe_emit(
        cur_bar=bar,
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )
    assert pending == []  # stood down — no redundant second close emitted
    assert tracker["AAA"].scaled_cursor.mapping == {}


def test_scaled_rung_close_carries_structural_partial_flag() -> None:
    """The engine stamps the structural ``engine_scaled_partial`` flag on a scaled
    rung's market scale-out — the in-flight-partial gate reads it instead of parsing
    the order reason. A full-position close leaves the flag False."""
    disp = _dispatcher(exit_rules=[_ladder(), StopLossRule(pct=0.03)])
    order_book = OrderBook()
    result = TradingServiceResult()

    # A scaled rung fires → its close carries engine_scaled_partial=True.
    tracker = _tracker(OrderSide.LONG)
    portfolio = _portfolio_with(side=OrderSide.LONG, qty=100.0, entry_price=100.0)
    pending: list[OrderRequest] = []
    disp.maybe_emit(
        cur_bar=_bar(high=106.0, low=100.0, close=105.0),  # +5% rung only
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )
    assert len(pending) == 1
    assert pending[0].engine_scaled_partial is True

    # A full-position stop close leaves the flag False.
    tracker2 = _tracker(OrderSide.LONG)
    portfolio2 = _portfolio_with(side=OrderSide.LONG, qty=100.0, entry_price=100.0)
    pending2: list[OrderRequest] = []
    disp.maybe_emit(
        cur_bar=_bar(high=100.0, low=96.0, close=97.0),  # 3% stop breached
        position_tracker=tracker2,
        portfolio=portfolio2,
        pending_for_prev=pending2,
        order_book=OrderBook(),
        result=TradingServiceResult(),
    )
    assert len(pending2) == 1
    assert "stop_loss" in pending2[0].reason
    assert pending2[0].engine_scaled_partial is False


def test_engine_exit_kind_strips_prefix_and_index_suffix() -> None:
    """_engine_exit_kind is the single source of engine-reason → rule-kind parsing:
    it drops the engine_exit: prefix and any signal_exit-style [idx] suffix."""
    assert _engine_exit_kind("engine_exit:scaled_take_profit") == "scaled_take_profit"
    assert _engine_exit_kind("engine_exit:stop_loss") == "stop_loss"
    assert _engine_exit_kind("engine_exit:signal_exit[2]") == "signal_exit"
    assert _engine_exit_kind("engine_exit:take_profit") == "take_profit"
    # Precondition is enforced: a non-engine reason fails loudly rather than being
    # silently mis-sliced into a bogus kind.
    for bad in ("", "stop_loss", "strategy_close"):
        with pytest.raises(ValueError, match="engine_exit:"):
            _engine_exit_kind(bad)


def _submit_competing_short(order_book: OrderBook) -> PendingOrder:
    """An unbound, opposite-side (SHORT) strategy LIMIT exit resting on the book for
    AAA — the kind of order the full-close cleanups must bind so it can't survive the
    close and later fire as a reverse entry."""
    return order_book.submit(
        OrderRequest(
            client_order_id="strat-tp",
            symbol="AAA",
            side=OrderSide.SHORT,
            qty=100.0,
            order_type=OrderType.LIMIT,
            limit_price=120.0,
            tif=TimeInForce.GTC,
        ),
        submitted_at="2024-01-09",
        submitted_equity=1_000_000.0,
    )


def test_full_close_rung_retires_competing_resting_orders() -> None:
    """A qty_fraction == 1.0 rung empties the position, so it must run the same
    whole-position cleanups as a full close — here, binding a competing resting exit
    to the position so it retires when the close fills."""
    disp = _dispatcher(
        exit_rules=[ScaledTakeProfitRule(levels=[{"pct": 0.05, "qty_fraction": 1.0}])]
    )
    order_book = OrderBook()
    competing = _submit_competing_short(order_book)
    assert competing.working_against_entry_order_id is None
    tracker = _tracker(OrderSide.LONG)
    portfolio = _portfolio_with(side=OrderSide.LONG, qty=100.0, entry_price=100.0)
    pending: list[OrderRequest] = []
    disp.maybe_emit(
        cur_bar=_bar(high=106.0, low=100.0, close=105.0),  # +5% target reached
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=TradingServiceResult(),
    )
    assert [r.qty for r in pending] == [100.0]  # 1.0 * original_qty
    # The emptying rung bound the competing exit to this position.
    assert competing.working_against_entry_order_id == portfolio.positions["AAA"].entry_order_id


def test_partial_rung_does_not_retire_competing_resting_orders() -> None:
    """Contrast: a true partial (0.5) leaves the position open, so competing exits
    must keep working — the cleanups must NOT run."""
    disp = _dispatcher(
        exit_rules=[ScaledTakeProfitRule(levels=[{"pct": 0.05, "qty_fraction": 0.5}])]
    )
    order_book = OrderBook()
    competing = _submit_competing_short(order_book)
    tracker = _tracker(OrderSide.LONG)
    portfolio = _portfolio_with(side=OrderSide.LONG, qty=100.0, entry_price=100.0)
    pending: list[OrderRequest] = []
    disp.maybe_emit(
        cur_bar=_bar(high=106.0, low=100.0, close=105.0),
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=TradingServiceResult(),
    )
    assert [r.qty for r in pending] == [50.0]  # 0.5 * original_qty, position stays open
    assert competing.working_against_entry_order_id is None  # left working


def test_rung_close_sized_off_original_qty_after_scale_in() -> None:
    """A rung closes ``qty_fraction * original_qty`` even after a scale-in has grown
    the live ``qty`` above ``original_qty``. The engine sizes off the ORIGINAL entry
    quantity (``Portfolio.extend`` never bumps ``original_qty`` for a separate
    same-side order), so a 0.5 rung on a 100-original / 150-current position closes
    50, not 75 — otherwise the ladder would over-harvest the scaled-in shares."""
    disp = _dispatcher(exit_rules=[_ladder()])
    tracker = _tracker(OrderSide.LONG)
    portfolio = _portfolio_with(side=OrderSide.LONG, qty=100.0, entry_price=100.0)
    portfolio.positions["AAA"].qty = 150.0  # scaled in +50; original_qty stays 100
    order_book = OrderBook()
    result = TradingServiceResult()
    pending: list[OrderRequest] = []
    disp.maybe_emit(
        cur_bar=_bar(high=106.0, low=100.0, close=105.0),  # +5% rung reached
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )
    assert [r.qty for r in pending] == [50.0]  # 0.5 * original_qty(100), NOT 0.5 * 150


def test_cursor_resets_when_position_is_swapped() -> None:
    """Fired rungs are tracked per position and reset on a swap. After a rung fires
    (cursor advances), a same-symbol position with a NEW entry_order_id replaces the
    old one; ``_update_position_tracker`` rebuilds the tracker with a fresh cursor, so
    the first rung can fire again for the new position instead of being suppressed."""
    disp = _dispatcher(exit_rules=[_ladder()])
    tracker = _tracker(OrderSide.LONG, entry_order_id="o1")
    portfolio = _portfolio_with(
        side=OrderSide.LONG, qty=100.0, entry_price=100.0, entry_order_id="o1"
    )
    order_book = OrderBook()
    result = TradingServiceResult()
    bar = _bar(high=106.0, low=100.0, close=105.0)  # +5% rung reached

    # Position #1 fires its first rung → cursor advances.
    pending: list[OrderRequest] = []
    disp.maybe_emit(
        cur_bar=bar,
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )
    assert [r.qty for r in pending] == [50.0]
    assert tracker["AAA"].scaled_cursor.mapping == {0: 1}

    # Swap: a brand-new position (different entry_order_id) replaces the old one.
    portfolio.positions["AAA"] = Position(
        symbol="AAA",
        side=OrderSide.LONG,
        qty=100.0,
        entry_price=100.0,
        entry_bid_price=100.0,
        entry_timestamp="2024-01-11",
        entry_order_id="o2",
        entry_client_order_id="c-o2",
        original_qty=100.0,
        entry_order_type="market",
    )
    # Intentional white-box call: tracker reconciliation is a TradingService
    # responsibility the per-bar loop runs BEFORE the dispatcher — maybe_emit does
    # NOT reconcile the tracker itself — so driving the real swap-reset path means
    # calling _update_position_tracker directly (as a @staticmethod) rather than
    # through maybe_emit. This pins the documented "cursor never leaks across trades"
    # invariant against the actual reset code, not a test-local re-implementation.
    TradingService._update_position_tracker(tracker=tracker, cur_bar=bar, portfolio=portfolio)
    assert tracker["AAA"].entry_order_id == "o2"
    assert tracker["AAA"].scaled_cursor.mapping == {}  # cursor reset for the new position

    # Position #2's first rung fires again from a fresh cursor.
    pending = []
    disp.maybe_emit(
        cur_bar=bar,
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )
    assert [r.qty for r in pending] == [50.0]
    assert tracker["AAA"].scaled_cursor.mapping == {0: 1}


def test_independent_ladders_advance_cursors_independently() -> None:
    """Two ScaledTakeProfitRule ladders on one position keep separate cursors keyed
    by rule_index. Each ladder's single rung fires once (in spec/priority order),
    advancing only its own cursor, and diagnostics record a firing per rule_index —
    the ladders never share or clobber each other's progress."""
    ladder_a = ScaledTakeProfitRule(levels=[{"pct": 0.05, "qty_fraction": 0.3}])  # rule 0
    ladder_b = ScaledTakeProfitRule(levels=[{"pct": 0.05, "qty_fraction": 0.3}])  # rule 1
    disp = _dispatcher(exit_rules=[ladder_a, ladder_b])
    tracker = _tracker(OrderSide.LONG)
    portfolio = _portfolio_with(side=OrderSide.LONG, qty=100.0, entry_price=100.0)
    order_book = OrderBook()
    result = TradingServiceResult()
    bar = _bar(high=106.0, low=100.0, close=105.0)  # +5% reached for both ladders

    seen = []
    for _ in range(3):
        pending: list[OrderRequest] = []
        disp.maybe_emit(
            cur_bar=bar,
            position_tracker=tracker,
            portfolio=portfolio,
            pending_for_prev=pending,
            order_book=order_book,
            result=result,
        )
        seen.append([r.qty for r in pending])

    # Rule 0 fires first (priority), then rule 1, then nothing — one tranche per call.
    assert seen == [[30.0], [30.0], []]
    # Each ladder advanced ONLY its own cursor.
    assert tracker["AAA"].scaled_cursor.mapping == {0: 1, 1: 1}
    assert result.execution_diagnostics.scaled_take_profit_level_firings == {"0:0": 1, "1:0": 1}


# ---------------------------------------------------------------------------
# Short-side symmetry: the long-side dispatcher contracts hold for shorts.
# ---------------------------------------------------------------------------


def test_each_rung_fires_at_most_once_and_in_order_short() -> None:
    """Short mirror of the multi-rung firing test: a short's rungs fire on bar.low
    (favorable = price falling), each once in cursor order as partial BUY closes
    (0.5 then 0.3 of original_qty), then nothing; per-rung diagnostics record both."""
    disp = _dispatcher(exit_rules=[_ladder()])
    tracker = _tracker(OrderSide.SHORT)
    portfolio = _portfolio_with(side=OrderSide.SHORT, qty=100.0, entry_price=100.0)
    order_book = OrderBook()
    result = TradingServiceResult()
    bar = _bar(high=100.0, low=89.0, close=90.0)  # crosses -5% AND -10% (short profit)

    seen = []
    for _ in range(3):
        pending: list[OrderRequest] = []
        disp.maybe_emit(
            cur_bar=bar,
            position_tracker=tracker,
            portfolio=portfolio,
            pending_for_prev=pending,
            order_book=order_book,
            result=result,
        )
        seen.append([(r.side, r.qty) for r in pending])

    assert seen == [[(OrderSide.LONG, 50.0)], [(OrderSide.LONG, 30.0)], []]  # buy closes
    assert tracker["AAA"].scaled_cursor.mapping == {0: 2}  # both rungs fired
    assert result.execution_diagnostics.scaled_take_profit_level_firings == {"0:0": 1, "0:1": 1}


def test_stop_loss_listed_first_takes_full_close_over_ladder_short() -> None:
    """Short mirror of stop-loss priority: a stop listed ahead of the ladder fires a
    FULL-position BUY close (not a rung) when the bar both breaches the short's stop
    (entry*(1+pct) on bar.high) and reaches the first profit rung (on bar.low)."""
    disp = _dispatcher(exit_rules=[StopLossRule(pct=0.03), _ladder()])
    tracker = _tracker(OrderSide.SHORT)
    portfolio = _portfolio_with(side=OrderSide.SHORT, qty=100.0, entry_price=100.0)
    order_book = OrderBook()
    result = TradingServiceResult()
    pending: list[OrderRequest] = []
    disp.maybe_emit(
        cur_bar=_bar(high=104.0, low=94.0, close=100.0),  # trips stop (103) AND -5%
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )
    assert len(pending) == 1
    assert pending[0].reason == f"{ENGINE_EXIT_REASON_PREFIX}stop_loss"
    assert pending[0].side == OrderSide.LONG  # closing a short buys
    assert pending[0].qty == 100.0  # full close
    assert tracker["AAA"].scaled_cursor.mapping == {}  # ladder did not fire


def test_scale_out_deferred_while_entry_continuation_resting_then_fires_full_size_short() -> None:
    """Short mirror of the deferred scale-out: while a short entry is still filling
    (50 of 100, continuation resting) the rung defers — sizing off the not-yet-settled
    original_qty would under-close. Once the entry settles to 100, the rung fires a
    full-size 0.5*100=50 BUY close."""
    disp = _dispatcher(exit_rules=[_ladder()])
    order_book = OrderBook()
    cont = order_book.submit(
        OrderRequest(
            client_order_id="entry-AAA",
            symbol="AAA",
            side=OrderSide.SHORT,
            qty=100.0,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
        ),
        submitted_at="2024-01-09",
        submitted_equity=1_000_000.0,
    )
    cont.cumulative_filled_qty = 50.0  # 50 filled, 50 still working
    tracker = _tracker(OrderSide.SHORT, entry_order_id=cont.order_id)
    portfolio = _portfolio_with(
        side=OrderSide.SHORT, qty=50.0, entry_price=100.0, entry_order_id=cont.order_id
    )
    portfolio.positions["AAA"].original_qty = 50.0  # only the first slice so far
    result = TradingServiceResult()
    bar = _bar(high=100.0, low=94.0, close=95.0)  # -5% target reached

    pending: list[OrderRequest] = []
    disp.maybe_emit(
        cur_bar=bar,
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )
    # Deferred: nothing emitted and the cursor is NOT advanced.
    assert pending == []
    assert tracker["AAA"].scaled_cursor.mapping == {}

    # Entry settles: continuation leaves the book, original_qty now reflects 100.
    order_book.remove(cont.order_id, was_filled=True)
    portfolio.positions["AAA"].original_qty = 100.0
    portfolio.positions["AAA"].qty = 100.0
    pending = []
    disp.maybe_emit(
        cur_bar=bar,
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )
    assert [(r.side, r.qty) for r in pending] == [(OrderSide.LONG, 50.0)]  # 0.5 * 100 buy close
    assert tracker["AAA"].scaled_cursor.mapping == {0: 1}


# ---------------------------------------------------------------------------
# Fill simulator: a partial MARKET close reduces qty and keeps the position open.
# ---------------------------------------------------------------------------


def _full_bar(
    ts: str,
    *,
    open_price: float,
    high: float,
    low: float,
    close: float | None = None,
    volume: float = 10_000_000.0,
) -> Bar:
    """A full ``contract.Bar`` for ``AAA`` (close defaults to open; high volume)."""
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


def _make_simulator(model) -> tuple[FillSimulator, OrderBook, Portfolio]:
    """A FillSimulator (+ its OrderBook/Portfolio) with no slippage/cost, for the
    given execution ``model``."""
    portfolio = Portfolio(initial_capital=100_000_000.0)
    order_book = OrderBook()
    sim = FillSimulator(
        portfolio=portfolio,
        order_book=order_book,
        risk_filter=RiskFilter(RiskLimits(max_position_pct=100, max_gross_leverage=10.0)),
        config=FillSimulatorConfig(slippage_bps=0.0, transaction_cost_bps=0.0),
        bar_safety=BarSafetyAssertion(),
        execution_model=model,
    )
    return sim, order_book, portfolio


def _open_long(sim: FillSimulator, order_book: OrderBook, qty: float = 100.0) -> None:
    """Open a fully-filled long ``AAA`` position of ``qty`` via a market entry bar."""
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
        submitted_equity=100_000_000.0,
    )
    sim.process_bar(_full_bar("2024-01-02", open_price=100.0, high=100.0, low=100.0))


def _submit_partial_close(order_book: OrderBook, cid: str, qty: float) -> None:
    """Submit an engine scaled-take-profit market close of ``qty`` (client id ``cid``)."""
    order_book.submit(
        OrderRequest(
            client_order_id=cid,
            symbol="AAA",
            side=OrderSide.SHORT,
            qty=qty,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
            reason=f"{ENGINE_EXIT_REASON_PREFIX}scaled_take_profit",
        ),
        submitted_at="2024-01-02",
        submitted_equity=100_000_000.0,
    )


@pytest.mark.parametrize(
    "model",
    [RealisticExecutionModel(participation_cap=1.0), OptimisticExecutionModel(warn=False)],
)
def test_partial_market_close_reduces_qty_and_keeps_position_open(model) -> None:
    """A partial MARKET scale-out reduces the position qty and keeps it open (no
    TradeRecord until the remainder closes), under both execution models."""
    sim, order_book, portfolio = _make_simulator(model)
    _open_long(sim, order_book, qty=100.0)

    # First tranche: close 50 of 100.
    _submit_partial_close(order_book, "tp-0", 50.0)
    outcome = sim.process_bar(_full_bar("2024-01-03", open_price=105.0, high=106.0, low=104.0))
    pos = portfolio.positions["AAA"]
    assert pos.qty == pytest.approx(50.0)
    assert not pos.is_closed
    assert not outcome.closed_trades  # no TradeRecord until the position fully closes

    # Second tranche: close the remaining 50 → fully closed, one trade recorded.
    _submit_partial_close(order_book, "tp-1", 50.0)
    outcome = sim.process_bar(_full_bar("2024-01-04", open_price=110.0, high=111.0, low=109.0))
    # Fully closed: the engine either drops the position from the book or leaves it
    # flat. Assert it is genuinely closed (not merely absent) — if still present it
    # must be is_closed with zero qty; the recorded trade below confirms the close.
    remaining = portfolio.positions.get("AAA")
    assert remaining is None or (remaining.is_closed and remaining.qty == pytest.approx(0.0))
    assert len(outcome.closed_trades) == 1
    assert outcome.closed_trades[0].exit_reason == f"{ENGINE_EXIT_REASON_PREFIX}scaled_take_profit"


def test_capped_scaled_close_requeues_until_full_fraction_filled() -> None:
    """End-to-end: a scale-out close that the participation cap clips fills only
    partially on a low-liquidity bar; with REQUEUE_NEXT_BAR the remainder keeps
    working and the rung's full fraction is realised once liquidity returns."""
    model = RealisticExecutionModel(participation_cap=0.10)
    sim, order_book, portfolio = _make_simulator(model)
    _open_long(sim, order_book, qty=100.0)  # high-volume entry bar → full fill

    order_book.submit(
        OrderRequest(
            client_order_id="tp-0",
            symbol="AAA",
            side=OrderSide.SHORT,
            qty=50.0,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
            unfilled_policy=UnfilledPolicy.REQUEUE_NEXT_BAR,
            reason=f"{ENGINE_EXIT_REASON_PREFIX}scaled_take_profit",
        ),
        submitted_at="2024-01-02",
        submitted_equity=100_000_000.0,
    )
    # Low-volume bar: cap (0.10 * 300 shares = 30) clips the 50-share close.
    sim.process_bar(_full_bar("2024-01-03", open_price=105.0, high=106.0, low=104.0, volume=300.0))
    pos = portfolio.positions["AAA"]
    assert pos.qty == pytest.approx(70.0)  # 100 - cap(0.10*300=30) filled; remainder requeued
    # The capped order is still pending with exactly the 20-share remainder requeued
    # (50 requested − 30 cap-filled this bar).
    tp0 = next(
        po for po in order_book.pending_for_symbol("AAA") if po.request.client_order_id == "tp-0"
    )
    assert tp0.cumulative_filled_qty == pytest.approx(30.0)
    assert tp0.request.qty - tp0.cumulative_filled_qty == pytest.approx(20.0)

    # Liquidity returns: the requeued remainder fills, completing the 50-share rung.
    sim.process_bar(
        _full_bar("2024-01-04", open_price=105.0, high=106.0, low=104.0, volume=10_000_000.0)
    )
    assert portfolio.positions["AAA"].qty == pytest.approx(50.0)
