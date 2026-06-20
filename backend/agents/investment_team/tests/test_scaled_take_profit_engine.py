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

from dataclasses import dataclass
from typing import Dict

import pytest

from investment_team.execution.bar_safety import BarSafetyAssertion
from investment_team.execution.risk_filter import RiskFilter, RiskLimits
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
from investment_team.trading_service.engine.order_book import OrderBook
from investment_team.trading_service.engine.portfolio import Portfolio, Position
from investment_team.trading_service.service import (
    TradingServiceResult,
    _EngineExitDispatcher,
    _TrackedPosition,
)
from investment_team.trading_service.strategy.contract import (
    Bar,
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


def _ladder() -> ScaledTakeProfitRule:
    return ScaledTakeProfitRule(
        levels=[{"pct": 0.05, "qty_fraction": 0.5}, {"pct": 0.10, "qty_fraction": 0.3}]
    )


def _dispatcher(*, exit_rules) -> _EngineExitDispatcher:
    return _EngineExitDispatcher(exit_rules=exit_rules, engine_exit_bindings={})


def _portfolio_with(
    *, side: OrderSide, qty: float, entry_price: float, entry_order_id: str = "o1"
) -> Portfolio:
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


def _tracker(side: OrderSide, entry_price: float = 100.0) -> Dict[str, _TrackedPosition]:
    return {
        "AAA": _TrackedPosition(
            side=side,
            entry_price=entry_price,
            entry_order_id="o1",
            just_opened=False,
            high_since_entry=entry_price,
            low_since_entry=entry_price,
        )
    }


def _bar(**kw) -> _MockBar:
    defaults = {
        "symbol": "AAA",
        "timestamp": "2024-01-10T00:00:00",
        "high": 100.0,
        "low": 100.0,
        "close": 100.0,
    }
    defaults.update(kw)
    return _MockBar(**defaults)


# ---------------------------------------------------------------------------
# Dispatcher: partial-close sizing + at-most-once firing + diagnostics.
# ---------------------------------------------------------------------------


def test_first_rung_emits_partial_close_sized_to_original_qty() -> None:
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
    assert tracker["AAA"].fired_tp_levels == {(0, 0)}
    diag = result.execution_diagnostics
    assert diag.exit_rule_firings.get("scaled_take_profit") == 1
    assert diag.scaled_take_profit_level_firings == {"0:0": 1}


def test_each_rung_fires_at_most_once_and_in_order() -> None:
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
    assert tracker["AAA"].fired_tp_levels == {(0, 0), (0, 1)}
    assert result.execution_diagnostics.scaled_take_profit_level_firings == {"0:0": 1, "0:1": 1}


def test_partial_scale_out_does_not_cancel_competing_resting_order() -> None:
    # A resting opposite-side protective order must survive a partial scale-out —
    # the remainder of the position still needs it. (A full close would retire it.)
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


def test_stop_loss_listed_first_takes_full_close_over_ladder() -> None:
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
    assert tracker["AAA"].fired_tp_levels == set()  # ladder did not fire


def test_short_first_rung_emits_partial_buy_close() -> None:
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


def test_scale_out_deferred_while_entry_continuation_resting_then_fires_full_size() -> None:
    # A partial entry (50 of 100 filled) with a REQUEUE_NEXT_BAR continuation still
    # resting: pos.original_qty is only 50 so far and the fill simulator will BUMP
    # it to 100 once the rest fills. A rung firing now would close 0.5*50=25 and be
    # marked fired, stranding the catch-up. The dispatcher must DEFER until the
    # entry settles, then close 0.5*100=50.
    disp = _dispatcher(exit_rules=[_ladder()])
    tracker = _tracker(OrderSide.LONG)
    portfolio = _portfolio_with(side=OrderSide.LONG, qty=50.0, entry_price=100.0)
    portfolio.positions["AAA"].original_qty = 50.0  # only the first slice so far
    order_book = OrderBook()
    cont = order_book.submit(
        OrderRequest(
            client_order_id="c-o1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=100.0,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
        ),
        submitted_at="2024-01-09",
        submitted_equity=1_000_000.0,
    )
    assert cont.order_id == "o1"  # matches pos.entry_order_id, so it's a continuation
    cont.cumulative_filled_qty = 50.0  # 50 filled, 50 still working
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
    # Deferred: nothing emitted and the rung is NOT marked fired.
    assert pending == []
    assert tracker["AAA"].fired_tp_levels == set()

    # Entry settles: continuation leaves the book, original_qty now reflects 100.
    order_book.remove("o1", was_filled=True)
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
    assert tracker["AAA"].fired_tp_levels == {(0, 0)}


# ---------------------------------------------------------------------------
# Fill simulator: a partial MARKET close reduces qty and keeps the position open.
# ---------------------------------------------------------------------------


def _full_bar(
    ts: str, *, open_price: float, high: float, low: float, close: float | None = None
) -> Bar:
    return Bar(
        symbol="AAA",
        timestamp=ts,
        timeframe="1d",
        open=open_price,
        high=high,
        low=low,
        close=close if close is not None else open_price,
        volume=10_000_000.0,
    )


def _make_simulator(model) -> tuple[FillSimulator, OrderBook, Portfolio]:
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
    assert "AAA" not in portfolio.positions or portfolio.positions["AAA"].qty == pytest.approx(0.0)
    assert len(outcome.closed_trades) == 1
    assert outcome.closed_trades[0].exit_reason == f"{ENGINE_EXIT_REASON_PREFIX}scaled_take_profit"
