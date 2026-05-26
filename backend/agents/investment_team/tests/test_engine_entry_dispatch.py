"""Unit tests for ``_EngineEntryDispatcher`` in ``trading_service.service``."""

from __future__ import annotations

from unittest.mock import MagicMock

from investment_team.strategy_lab.executor.predicate_evaluator import (
    BarRecord,
    StreamingHistoryView,
)
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    FixedFractionSizing,
    FixedNotionalSizing,
    Predicate,
)
from investment_team.trading_service.service import (
    TradingServiceResult,
    _EngineEntryDispatcher,
)


def _make_bar(
    symbol="AAA", close=100.0, high=101.0, low=99.0, volume=1000.0, timestamp="2024-01-10"
):
    bar = MagicMock()
    bar.symbol = symbol
    bar.close = close
    bar.high = high
    bar.low = low
    bar.open = close
    bar.volume = volume
    bar.timestamp = timestamp
    return bar


def _make_portfolio(capital=100000.0, positions=None):
    port = MagicMock()
    port.positions = positions or {}
    port.mark_to_market.return_value = capital
    return port


def _build_view(closes: list[float]) -> StreamingHistoryView:
    view = StreamingHistoryView()
    for i, c in enumerate(closes):
        view.append(
            BarRecord(
                timestamp=f"2024-01-{i + 1:02d}",
                open=c,
                high=c + 1,
                low=c - 1,
                close=c,
                volume=1000.0,
            )
        )
    return view


def test_entry_fires_when_predicate_satisfied():
    rules = [
        EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=90.0)),
    ]
    dispatcher = _EngineEntryDispatcher(
        entry_rules=rules,
        sizing=FixedFractionSizing(fraction=0.02),
    )
    bar = _make_bar(close=100.0)
    portfolio = _make_portfolio()
    pending: list = []
    views = {"AAA": _build_view([80.0, 90.0, 100.0])}
    result = TradingServiceResult()

    dispatcher.maybe_emit(
        cur_bar=bar,
        portfolio=portfolio,
        pending_for_prev=pending,
        views=views,
        result=result,
    )
    assert len(pending) == 1
    assert pending[0].side.value == "long"
    assert pending[0].reason.startswith("engine_entry:")


def test_entry_skipped_when_position_exists():
    rules = [
        EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=90.0)),
    ]
    dispatcher = _EngineEntryDispatcher(
        entry_rules=rules,
        sizing=FixedFractionSizing(fraction=0.02),
    )
    bar = _make_bar(close=100.0)
    portfolio = _make_portfolio(positions={"AAA": MagicMock()})
    pending: list = []
    views = {"AAA": _build_view([80.0, 90.0, 100.0])}
    result = TradingServiceResult()

    dispatcher.maybe_emit(
        cur_bar=bar,
        portfolio=portfolio,
        pending_for_prev=pending,
        views=views,
        result=result,
    )
    assert len(pending) == 0


def test_entry_skipped_when_predicate_not_satisfied():
    rules = [
        EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=200.0)),
    ]
    dispatcher = _EngineEntryDispatcher(
        entry_rules=rules,
        sizing=FixedFractionSizing(fraction=0.02),
    )
    bar = _make_bar(close=100.0)
    portfolio = _make_portfolio()
    pending: list = []
    views = {"AAA": _build_view([80.0, 90.0, 100.0])}
    result = TradingServiceResult()

    dispatcher.maybe_emit(
        cur_bar=bar,
        portfolio=portfolio,
        pending_for_prev=pending,
        views=views,
        result=result,
    )
    assert len(pending) == 0


def test_entry_disabled_when_no_rules():
    dispatcher = _EngineEntryDispatcher(entry_rules=[], sizing=None)
    bar = _make_bar()
    portfolio = _make_portfolio()
    pending: list = []
    result = TradingServiceResult()
    dispatcher.maybe_emit(
        cur_bar=bar,
        portfolio=portfolio,
        pending_for_prev=pending,
        views={"AAA": _build_view([100.0])},
        result=result,
    )
    assert len(pending) == 0


def test_sizing_fixed_notional():
    rules = [
        EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=50.0)),
    ]
    dispatcher = _EngineEntryDispatcher(
        entry_rules=rules,
        sizing=FixedNotionalSizing(notional_usd=5000.0),
    )
    bar = _make_bar(close=100.0)
    portfolio = _make_portfolio()
    pending: list = []
    views = {"AAA": _build_view([60.0, 70.0, 80.0])}
    result = TradingServiceResult()

    dispatcher.maybe_emit(
        cur_bar=bar,
        portfolio=portfolio,
        pending_for_prev=pending,
        views=views,
        result=result,
    )
    assert len(pending) == 1
    assert pending[0].qty == 50  # 5000 / 100
