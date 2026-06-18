"""Engine-side exit-attribution reconciliation.

PR #867 made the engine the owner of ``spec.exit_rules`` for custom-code
strategies, but a spec-compliant *manual* close still fills ahead of the engine
and drops the ``engine_exit:<kind>`` attribution the trade-alignment gate relies
on. These tests cover the two halves of the fix:

1. ``_build_exit_reconciler`` — the closure that maps a strategy-initiated close
   complying with a structured exit rule (within bounds) to an
   ``engine_exit:<kind>`` label, and leaves out-of-bounds / non-firing closes
   untouched.
2. ``FillSimulator`` — invokes the injected reconciler at its TradeRecord
   exit-stamping site only for non-``engine_exit`` reasons, and is a strict no-op
   when no reconciler is injected.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from investment_team.execution.bar_safety import BarSafetyAssertion
from investment_team.execution.risk_filter import RiskFilter, RiskLimits
from investment_team.strategy_lab.executor.predicate_evaluator import (
    BarRecord,
    StreamingHistoryView,
)
from investment_team.strategy_lab.spec_dsl import (
    Predicate,
    SignalExitRule,
    StopLossRule,
    TakeProfitRule,
)
from investment_team.trading_service.engine.execution_model import OptimisticExecutionModel
from investment_team.trading_service.engine.fill_simulator import (
    FillSimulator,
    FillSimulatorConfig,
)
from investment_team.trading_service.engine.order_book import OrderBook
from investment_team.trading_service.engine.portfolio import Portfolio
from investment_team.trading_service.service import (
    ENGINE_EXIT_REASON_PREFIX,
    _build_exit_reconciler,
    _TrackedPosition,
)
from investment_team.trading_service.strategy.contract import (
    Bar,
    OrderRequest,
    OrderSide,
    OrderType,
    TimeInForce,
)

# ---------------------------------------------------------------------------
# _build_exit_reconciler — closure unit tests
# ---------------------------------------------------------------------------


def _view(*bars: tuple[float, float, float]) -> StreamingHistoryView:
    """Build a streaming view whose latest bar is the *signal bar*.

    Each ``bar`` is ``(high, low, close)``. The reconciler reads the latest
    bar's OHLC for price (TP/SL) rules and runs signal-exit predicates against
    it — mirroring the run loop, where the fill bar has not yet been appended
    when the reconciler runs, so the view's tail is the bar the strategy acted
    on.
    """
    view = StreamingHistoryView()
    for i, (high, low, close) in enumerate(bars):
        view.append(
            BarRecord(
                timestamp=f"2024-01-{i + 1:02d}",
                open=close,
                high=high,
                low=low,
                close=close,
                volume=1_000_000.0,
            )
        )
    return view


def test_build_returns_none_when_no_exit_rules() -> None:
    """No structured rules → no reconciler (FillSimulator keeps its no-op)."""
    assert _build_exit_reconciler([], {}) is None


def test_take_profit_within_ceiling_is_reconciled() -> None:
    # entry 100, long; signal bar high 106 clears the 105 target → rule fires.
    view = _view((106.0, 99.0, 105.5))
    reconcile = _build_exit_reconciler([TakeProfitRule(pct=0.05)], {"AAA": view})
    assert reconcile is not None
    label = reconcile(
        symbol="AAA",
        side="long",
        entry_price=100.0,
        qty=10.0,
        return_pct=4.8,  # within the 5% ceiling (+0.5pp slack)
    )
    assert label == f"{ENGINE_EXIT_REASON_PREFIX}take_profit"


def test_take_profit_past_cap_is_not_reconciled() -> None:
    """A close that filled *past* the cap keeps its strategy reason — the bound
    check is what prevents masking the original #867 cap-breach bug."""
    view = _view((112.0, 99.0, 111.0))
    reconcile = _build_exit_reconciler([TakeProfitRule(pct=0.05)], {"AAA": view})
    assert reconcile is not None
    label = reconcile(
        symbol="AAA",
        side="long",
        entry_price=100.0,
        qty=10.0,
        return_pct=7.0,  # exceeds the 5% ceiling + slack
    )
    assert label is None


def test_stop_loss_entry_basis_within_floor_is_reconciled() -> None:
    # entry 100, long; signal bar low 94 crosses the 95 floor → rule fires.
    view = _view((101.0, 94.0, 95.0))
    reconcile = _build_exit_reconciler([StopLossRule(pct=0.05, basis="entry_price")], {"AAA": view})
    assert reconcile is not None
    label = reconcile(
        symbol="AAA",
        side="long",
        entry_price=100.0,
        qty=10.0,
        return_pct=-4.8,  # within the -5% floor (-0.5pp slack)
    )
    assert label == f"{ENGINE_EXIT_REASON_PREFIX}stop_loss"


def test_stop_loss_breaching_floor_is_not_reconciled() -> None:
    view = _view((101.0, 80.0, 85.0))
    reconcile = _build_exit_reconciler([StopLossRule(pct=0.05, basis="entry_price")], {"AAA": view})
    assert reconcile is not None
    label = reconcile(
        symbol="AAA",
        side="long",
        entry_price=100.0,
        qty=10.0,
        return_pct=-9.0,  # breaches the -5% floor
    )
    assert label is None


def test_trailing_stop_is_deferred_not_reconciled() -> None:
    """Trailing-basis stops are path-dependent; the reconciler never stamps them
    (they stay deferred to the alignment gate), even if a touch is detected."""
    view = _view((101.0, 94.0, 95.0))
    reconcile = _build_exit_reconciler(
        [StopLossRule(pct=0.05, basis="trailing_high")], {"AAA": view}
    )
    assert reconcile is not None
    label = reconcile(
        symbol="AAA",
        side="long",
        entry_price=100.0,
        qty=10.0,
        return_pct=-4.8,
    )
    assert label is None


def test_signal_exit_is_reconciled() -> None:
    # latest (signal) bar close 98 < 99 → predicate fires.
    view = _view((102.0, 100.0, 101.0), (100.0, 98.0, 99.0), (99.0, 97.0, 98.0))
    rule = SignalExitRule(when=Predicate(lhs="bar.close", op="<", rhs=99.0))
    reconcile = _build_exit_reconciler([rule], {"AAA": view})
    assert reconcile is not None
    label = reconcile(
        symbol="AAA",
        side="long",
        entry_price=100.0,
        qty=10.0,
        return_pct=-2.0,  # signal exits have no return bound
    )
    # Bracketed rule index matches the engine's emitted form so the
    # rule-firing-rate gate counts the reconciled close.
    assert label == f"{ENGINE_EXIT_REASON_PREFIX}signal_exit[0]"


def test_non_firing_rule_is_not_reconciled() -> None:
    # signal bar high 101 never reaches the 105 target → no rule fires.
    view = _view((101.0, 99.0, 100.5))
    reconcile = _build_exit_reconciler([TakeProfitRule(pct=0.05)], {"AAA": view})
    assert reconcile is not None
    label = reconcile(
        symbol="AAA",
        side="long",
        entry_price=100.0,
        qty=10.0,
        return_pct=0.5,
    )
    assert label is None


def test_zero_qty_is_not_reconciled() -> None:
    view = _view((106.0, 99.0, 105.0))
    reconcile = _build_exit_reconciler([TakeProfitRule(pct=0.05)], {"AAA": view})
    assert reconcile is not None
    assert (
        reconcile(
            symbol="AAA",
            side="long",
            entry_price=100.0,
            qty=0.0,
            return_pct=4.8,
        )
        is None
    )


def test_missing_view_is_not_reconciled() -> None:
    """Without a signal bar in ``views`` the reconciler defers (never guesses)."""
    reconcile = _build_exit_reconciler([TakeProfitRule(pct=0.05)], {})
    assert reconcile is not None
    assert reconcile(symbol="AAA", side="long", entry_price=100.0, qty=10.0, return_pct=4.8) is None


# --- Multiple rules firing on the same bar ---------------------------------


def test_multiple_rules_first_out_of_bounds_falls_through_to_signal() -> None:
    """A take-profit firing *past* its ceiling must not shadow a compliant
    signal-exit firing on the same bar — attribution still gets stamped."""
    # Signal bar clears the 105 TP target (high 112) AND fires the signal
    # predicate (close 98 < 99).
    view = _view((112.0, 97.0, 98.0))
    rules = [
        TakeProfitRule(pct=0.05),
        SignalExitRule(when=Predicate(lhs="bar.close", op="<", rhs=99.0)),
    ]
    reconcile = _build_exit_reconciler(rules, {"AAA": view})
    assert reconcile is not None
    label = reconcile(
        symbol="AAA",
        side="long",
        entry_price=100.0,
        qty=10.0,
        return_pct=7.0,  # TP fires but return is past the cap → falls through
    )
    # Signal rule is at spec index 1 → bracketed index in the reason.
    assert label == f"{ENGINE_EXIT_REASON_PREFIX}signal_exit[1]"


def test_multiple_rules_first_within_bounds_wins() -> None:
    """When the first firing rule complies, it takes priority (spec order)."""
    view = _view((106.0, 97.0, 98.0))  # TP target and signal predicate both fire
    rules = [
        TakeProfitRule(pct=0.05),
        SignalExitRule(when=Predicate(lhs="bar.close", op="<", rhs=99.0)),
    ]
    reconcile = _build_exit_reconciler(rules, {"AAA": view})
    assert reconcile is not None
    label = reconcile(
        symbol="AAA",
        side="long",
        entry_price=100.0,
        qty=10.0,
        return_pct=4.8,  # within the 5% ceiling
    )
    assert label == f"{ENGINE_EXIT_REASON_PREFIX}take_profit"


def test_tightest_take_profit_ceiling_blocks_drift() -> None:
    """With multiple TP rules, the tightest ceiling governs — a close that
    drifted past it is not reconciled (mirrors the alignment gate)."""
    # Both targets (103, 105) are cleared on the signal bar (high 106).
    view = _view((106.0, 99.0, 105.5))
    reconcile = _build_exit_reconciler(
        [TakeProfitRule(pct=0.03), TakeProfitRule(pct=0.05)], {"AAA": view}
    )
    assert reconcile is not None
    label = reconcile(
        symbol="AAA",
        side="long",
        entry_price=100.0,
        qty=10.0,
        return_pct=4.8,  # past the tightest 3% ceiling
    )
    assert label is None


# --- Entry-bar skip (mirrors the dispatcher's just_opened guard) ----------


def _tracked(*, just_opened: bool) -> _TrackedPosition:
    return _TrackedPosition(
        side=OrderSide.LONG,
        entry_price=100.0,
        entry_order_id="e1",
        just_opened=just_opened,
        high_since_entry=100.0,
        low_since_entry=100.0,
    )


def test_just_opened_entry_bar_is_not_reconciled() -> None:
    """A close whose signal bar is a non-market entry bar (just_opened) is not
    reconciled — the engine deliberately skips exit eval there."""
    view = _view((106.0, 99.0, 105.5))  # TP target cleared on the (entry) bar
    tracker = {"AAA": _tracked(just_opened=True)}
    reconcile = _build_exit_reconciler([TakeProfitRule(pct=0.05)], {"AAA": view}, tracker)
    assert reconcile is not None
    label = reconcile(
        symbol="AAA",
        side="long",
        entry_price=100.0,
        qty=10.0,
        return_pct=4.8,
    )
    assert label is None


def test_non_entry_bar_is_reconciled_with_tracker() -> None:
    """With the tracker present but the position past its entry bar
    (just_opened False), reconciliation proceeds normally."""
    view = _view((106.0, 99.0, 105.5))
    tracker = {"AAA": _tracked(just_opened=False)}
    reconcile = _build_exit_reconciler([TakeProfitRule(pct=0.05)], {"AAA": view}, tracker)
    assert reconcile is not None
    label = reconcile(
        symbol="AAA",
        side="long",
        entry_price=100.0,
        qty=10.0,
        return_pct=4.8,
    )
    assert label == f"{ENGINE_EXIT_REASON_PREFIX}take_profit"


# --- Short positions ------------------------------------------------------


def test_short_take_profit_within_ceiling_is_reconciled() -> None:
    # entry 100, short; signal bar low 94 clears the 95 target → rule fires; a
    # short profits as price falls, so the realized return is positive.
    view = _view((101.0, 94.0, 95.0))
    reconcile = _build_exit_reconciler([TakeProfitRule(pct=0.05)], {"AAA": view})
    assert reconcile is not None
    label = reconcile(
        symbol="AAA",
        side="short",
        entry_price=100.0,
        qty=10.0,
        return_pct=4.8,  # within the 5% ceiling
    )
    assert label == f"{ENGINE_EXIT_REASON_PREFIX}take_profit"


def test_short_stop_loss_within_floor_is_reconciled() -> None:
    # entry 100, short; signal bar high 106 crosses the 105 ceiling → rule
    # fires; a short loses as price rises, so the realized return is negative.
    view = _view((106.0, 99.0, 105.0))
    reconcile = _build_exit_reconciler([StopLossRule(pct=0.05, basis="entry_price")], {"AAA": view})
    assert reconcile is not None
    label = reconcile(
        symbol="AAA",
        side="short",
        entry_price=100.0,
        qty=10.0,
        return_pct=-4.8,  # within the -5% floor
    )
    assert label == f"{ENGINE_EXIT_REASON_PREFIX}stop_loss"


# ---------------------------------------------------------------------------
# FillSimulator — invokes the injected reconciler at the stamping site
# ---------------------------------------------------------------------------


class _RecordingReconciler:
    """Stub reconciler that records its calls and returns a fixed label."""

    def __init__(self, label: Optional[str]) -> None:
        self.label = label
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Optional[str]:
        self.calls.append(kwargs)
        return self.label


def _bar(ts: str, *, price: float) -> Bar:
    return Bar(
        symbol="AAA",
        timestamp=ts,
        timeframe="1d",
        open=price,
        high=price + 1,
        low=price - 1,
        close=price,
        volume=1_000_000.0,
    )


def _make_sim(reconciler: Any | None) -> tuple[FillSimulator, OrderBook]:
    portfolio = Portfolio(initial_capital=10_000_000.0)
    order_book = OrderBook()
    sim = FillSimulator(
        portfolio=portfolio,
        order_book=order_book,
        risk_filter=RiskFilter(RiskLimits(max_position_pct=100, max_gross_leverage=10.0)),
        config=FillSimulatorConfig(slippage_bps=0.0, transaction_cost_bps=0.0),
        bar_safety=BarSafetyAssertion(),
        execution_model=OptimisticExecutionModel(warn=False),
        exit_reconciler=reconciler,
    )
    return sim, order_book


def _open_long(sim: FillSimulator, order_book: OrderBook) -> None:
    order_book.submit(
        OrderRequest(
            client_order_id="entry-1",
            symbol="AAA",
            side=OrderSide.LONG,
            qty=10,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
        ),
        submitted_at="2024-01-01",
        submitted_equity=10_000_000.0,
    )
    sim.process_bar(_bar("2024-01-02", price=100.0))


def _submit_close(order_book: OrderBook, *, reason: str) -> None:
    order_book.submit(
        OrderRequest(
            client_order_id="exit-1",
            symbol="AAA",
            side=OrderSide.SHORT,
            qty=10,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
            reason=reason,
        ),
        submitted_at="2024-01-02",
        submitted_equity=10_000_000.0,
    )


def test_reconciler_stamps_strategy_close() -> None:
    """A strategy-emitted close is rewritten with the reconciler's label, and
    the reconciler receives the closing position's facts."""
    stub = _RecordingReconciler(f"{ENGINE_EXIT_REASON_PREFIX}take_profit")
    sim, order_book = _make_sim(stub)
    _open_long(sim, order_book)
    _submit_close(order_book, reason="sma_cross_down")

    outcome = sim.process_bar(_bar("2024-01-03", price=105.0))

    assert len(outcome.closed_trades) == 1
    assert outcome.closed_trades[0].exit_reason == f"{ENGINE_EXIT_REASON_PREFIX}take_profit"
    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["symbol"] == "AAA"
    assert call["side"] == "long"
    assert call["qty"] == 10
    assert call["entry_price"] == pytest.approx(100.0)
    assert call["return_pct"] == pytest.approx(5.0)
    assert "bar" not in call  # the reconciler reads the signal bar from views


def test_reconciler_skipped_for_engine_owned_reason() -> None:
    """A close already labelled ``engine_exit:*`` bypasses the reconciler."""
    stub = _RecordingReconciler(f"{ENGINE_EXIT_REASON_PREFIX}take_profit")
    sim, order_book = _make_sim(stub)
    _open_long(sim, order_book)
    _submit_close(order_book, reason=f"{ENGINE_EXIT_REASON_PREFIX}stop_loss")

    outcome = sim.process_bar(_bar("2024-01-03", price=105.0))

    assert outcome.closed_trades[0].exit_reason == f"{ENGINE_EXIT_REASON_PREFIX}stop_loss"
    assert stub.calls == []


def test_reconciler_returning_none_keeps_strategy_reason() -> None:
    """An out-of-bounds close (reconciler → None) keeps its strategy reason."""
    stub = _RecordingReconciler(None)
    sim, order_book = _make_sim(stub)
    _open_long(sim, order_book)
    _submit_close(order_book, reason="discretionary_close")

    outcome = sim.process_bar(_bar("2024-01-03", price=105.0))

    assert outcome.closed_trades[0].exit_reason == "discretionary_close"
    assert len(stub.calls) == 1


def test_no_reconciler_is_a_strict_no_op() -> None:
    """Default (no reconciler) leaves the strategy close reason untouched."""
    sim, order_book = _make_sim(None)
    _open_long(sim, order_book)
    _submit_close(order_book, reason="discretionary_close")

    outcome = sim.process_bar(_bar("2024-01-03", price=105.0))

    assert outcome.closed_trades[0].exit_reason == "discretionary_close"
