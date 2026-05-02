"""Model tests for the zero-trade execution diagnostics envelope (issue #407).

Pure Pydantic model coverage. The producers and the anomaly-detector
consumer are wired up in subsequent issues (#408–#414); this test file
exercises only the typed shapes and the backwards-compat guarantees on
``BacktestResult``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from investment_team.models import (
    MAX_RECENT_ORDER_EVENTS,
    BacktestExecutionDiagnostics,
    BacktestResult,
    OpenPositionDiagnostic,
    OrderLifecycleEvent,
    OrderLifecycleEventType,
    ZeroTradeCategory,
)


def _required_backtest_kwargs() -> dict:
    return dict(
        total_return_pct=12.0,
        annualized_return_pct=10.0,
        volatility_pct=8.0,
        sharpe_ratio=1.2,
        max_drawdown_pct=4.0,
        win_rate_pct=55.0,
        profit_factor=1.6,
    )


def test_order_lifecycle_event_round_trip():
    event = OrderLifecycleEvent(
        event_type=OrderLifecycleEventType.REJECTED,
        timestamp="2026-04-01T13:30:00Z",
        symbol="AAPL",
        order_id="o-1",
        side="buy",
        order_type="market",
        quantity=10.0,
        price=170.5,
        reason="risk_filter:position_size",
    )
    payload = event.model_dump()
    restored = OrderLifecycleEvent.model_validate(payload)
    assert restored == event
    assert restored.event_type is OrderLifecycleEventType.REJECTED


def test_open_position_diagnostic_round_trip():
    pos = OpenPositionDiagnostic(
        symbol="MSFT",
        quantity=5.0,
        entry_price=420.10,
        entry_date="2026-03-15",
        bars_held=12,
        unrealized_pnl=-3.25,
    )
    restored = OpenPositionDiagnostic.model_validate(pos.model_dump())
    assert restored == pos


def test_backtest_execution_diagnostics_defaults_are_zero_and_empty():
    diag = BacktestExecutionDiagnostics()
    assert diag.bars_processed == 0
    assert diag.orders_emitted == 0
    assert diag.orders_accepted == 0
    assert diag.orders_rejected == 0
    assert diag.orders_unfilled == 0
    assert diag.warmup_orders_dropped == 0
    assert diag.entries_filled == 0
    assert diag.exits_emitted == 0
    assert diag.closed_trades == 0
    assert diag.orders_rejection_reasons == {}
    assert diag.last_order_events == []
    assert diag.open_positions_at_end == []
    assert diag.zero_trade_category is None
    assert diag.summary is None


def test_last_order_events_caps_to_max_and_keeps_tail():
    overflow = MAX_RECENT_ORDER_EVENTS + 7
    events = [
        OrderLifecycleEvent(
            event_type=OrderLifecycleEventType.EMITTED,
            timestamp=f"2026-04-01T00:00:{i % 60:02d}Z",
            order_id=f"o-{i}",
        )
        for i in range(overflow)
    ]
    diag = BacktestExecutionDiagnostics(last_order_events=events)
    assert len(diag.last_order_events) == MAX_RECENT_ORDER_EVENTS
    # Tail is preserved — the very last input event must survive.
    assert diag.last_order_events[-1].order_id == f"o-{overflow - 1}"
    # And the truncation drops the head.
    assert diag.last_order_events[0].order_id == f"o-{overflow - MAX_RECENT_ORDER_EVENTS}"


def test_zero_trade_category_parses_known_values_and_rejects_unknown():
    assert ZeroTradeCategory("orders_rejected") is ZeroTradeCategory.ORDERS_REJECTED
    assert ZeroTradeCategory("entry_with_no_exit") is ZeroTradeCategory.ENTRY_WITH_NO_EXIT
    with pytest.raises(ValueError):
        ZeroTradeCategory("not_a_real_category")


def test_backtest_result_constructs_without_execution_diagnostics():
    result = BacktestResult(**_required_backtest_kwargs())
    assert result.execution_diagnostics is None


def test_backtest_result_deserializes_legacy_row_without_field():
    # Legacy persisted rows predate issue #407 and have no
    # ``execution_diagnostics`` key. ``model_validate`` must accept them and
    # default the field to ``None``. Acceptance criterion of #407.
    legacy_row = _required_backtest_kwargs()
    assert "execution_diagnostics" not in legacy_row
    result = BacktestResult.model_validate(legacy_row)
    assert result.execution_diagnostics is None


def test_backtest_result_with_diagnostics_round_trips_through_json():
    diag = BacktestExecutionDiagnostics(
        bars_processed=500,
        orders_emitted=3,
        orders_rejected=3,
        orders_rejection_reasons={"risk_filter:position_size": 3},
        last_order_events=[
            OrderLifecycleEvent(
                event_type=OrderLifecycleEventType.REJECTED,
                timestamp="2026-04-01T13:30:00Z",
                symbol="AAPL",
                reason="risk_filter:position_size",
            ),
        ],
        open_positions_at_end=[],
        zero_trade_category=ZeroTradeCategory.ORDERS_REJECTED,
        summary="3 orders emitted; all rejected by position-size filter.",
    )
    original = BacktestResult(**_required_backtest_kwargs(), execution_diagnostics=diag)
    restored = BacktestResult.model_validate_json(original.model_dump_json())
    assert restored == original
    assert restored.execution_diagnostics is not None
    assert restored.execution_diagnostics.zero_trade_category is ZeroTradeCategory.ORDERS_REJECTED


def test_order_lifecycle_event_rejects_invalid_event_type():
    with pytest.raises(ValidationError):
        OrderLifecycleEvent(event_type="not_a_real_type", timestamp="2026-04-01T00:00:00Z")
