"""Exit-label enrichment: distinguish trailing vs fixed stop fires.

The enrichment is additive metadata. ``ExitIntent.basis`` carries the
``StopLossRule.basis`` so the engine can count trailing vs fixed stop fires
separately (``BacktestExecutionDiagnostics.exit_rule_firings_by_basis``) WITHOUT
perturbing ``rule_kind`` or the ``engine_exit:<rule_kind>`` close ``reason`` that
the conformance + alignment gates match by exact equality. These tests pin both
properties: the new per-basis counter is populated, and the close ``reason`` /
``exit_rule_firings`` stay byte-stable.
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
from investment_team.trading_service.strategy.contract import OrderSide

# ---------------------------------------------------------------------------
# evaluate_exit_rules — ExitIntent.basis population (pure function)
# ---------------------------------------------------------------------------


def _long_position(*, high_since_entry: float = 110.0, low_since_entry: float = 95.0):
    return {
        "AAA": PositionState(
            symbol="AAA",
            side="long",
            qty=10.0,
            entry_price=100.0,
            high_since_entry=high_since_entry,
            low_since_entry=low_since_entry,
        )
    }


def test_intent_basis_trailing_high() -> None:
    positions = _long_position()
    # trailing_high floor = 110 * 0.98 = 107.8; bar.low 107 trips it.
    bars = {"AAA": BarSnapshot(high=108.0, low=107.0, close=107.5)}
    intents = evaluate_exit_rules([StopLossRule(pct=0.02, basis="trailing_high")], positions, bars)
    assert len(intents) == 1
    assert intents[0].rule_kind == "stop_loss"
    assert intents[0].basis == "trailing_high"


def test_intent_basis_entry_price() -> None:
    positions = _long_position()
    # entry_price floor = 100 * 0.95 = 95; bar.low 94 trips it.
    bars = {"AAA": BarSnapshot(high=99.0, low=94.0, close=96.0)}
    intents = evaluate_exit_rules([StopLossRule(pct=0.05)], positions, bars)
    assert len(intents) == 1
    assert intents[0].basis == "entry_price"


def test_intent_basis_none_for_take_profit() -> None:
    positions = _long_position()
    # take_profit at 100 * 1.10 = 110; bar.high 111 trips it.
    bars = {"AAA": BarSnapshot(high=111.0, low=105.0, close=110.5)}
    intents = evaluate_exit_rules([TakeProfitRule(pct=0.10)], positions, bars)
    assert len(intents) == 1
    assert intents[0].rule_kind == "take_profit"
    assert intents[0].basis is None


# ---------------------------------------------------------------------------
# Dispatcher — exit_rule_firings_by_basis + exit_reason stability
# ---------------------------------------------------------------------------


@dataclass
class _MockBar:
    symbol: str
    timestamp: str
    high: float
    low: float
    close: float


def _setup(high_since_entry: float = 110.0):
    tracker: Dict[str, _TrackedPosition] = {
        "AAA": _TrackedPosition(
            side=OrderSide.LONG,
            entry_price=100.0,
            entry_order_id="o1",
            just_opened=False,
            high_since_entry=high_since_entry,
            low_since_entry=95.0,
        )
    }
    portfolio = Portfolio(initial_capital=100_000.0)
    portfolio.positions["AAA"] = Position(
        symbol="AAA",
        side=OrderSide.LONG,
        qty=100.0,
        entry_price=100.0,
        entry_bid_price=100.0,
        entry_timestamp="2024-01-01",
        entry_order_id="o1",
        entry_client_order_id="c-o1",
        original_qty=100.0,
        entry_order_type="market",
    )
    return tracker, portfolio, OrderBook()


def _emit(exit_rules, tracker, portfolio, order_book) -> TradingServiceResult:
    disp = _EngineExitDispatcher(exit_rules=exit_rules, engine_exit_bindings={})
    bar = _MockBar(symbol="AAA", timestamp="2024-01-10T00:00:00", high=105.0, low=95.0, close=100.0)
    result = TradingServiceResult()
    pending: list = []
    disp.maybe_emit(
        cur_bar=bar,
        position_tracker=tracker,
        portfolio=portfolio,
        pending_for_prev=pending,
        order_book=order_book,
        result=result,
    )
    result._pending = pending  # type: ignore[attr-defined]
    return result


def test_by_basis_counter_trailing_high_and_reason_stable() -> None:
    tracker, portfolio, order_book = _setup(high_since_entry=110.0)
    # trailing_high floor = 110 * 0.98 = 107.8; bar.low 95 trips it.
    result = _emit([StopLossRule(pct=0.02, basis="trailing_high")], tracker, portfolio, order_book)
    diag = result.execution_diagnostics
    # rule_kind counter unchanged; new by-basis counter distinguishes trailing.
    assert diag.exit_rule_firings.get("stop_loss") == 1
    assert diag.exit_rule_firings_by_basis.get("stop_loss:trailing_high") == 1
    # The close reason stays EXACTLY "engine_exit:stop_loss" (no basis suffix),
    # so the exact-match conformance + alignment gates keep counting it.
    engine_orders = [
        r for r in result._pending if (r.reason or "").startswith(ENGINE_EXIT_REASON_PREFIX)
    ]
    assert len(engine_orders) == 1
    assert engine_orders[0].reason == f"{ENGINE_EXIT_REASON_PREFIX}stop_loss"


def test_by_basis_counter_entry_price() -> None:
    tracker, portfolio, order_book = _setup(high_since_entry=110.0)
    # entry_price floor = 100 * 0.02 → 98; bar.low 95 trips it.
    result = _emit([StopLossRule(pct=0.02)], tracker, portfolio, order_book)
    diag = result.execution_diagnostics
    assert diag.exit_rule_firings.get("stop_loss") == 1
    assert diag.exit_rule_firings_by_basis.get("stop_loss:entry_price") == 1
