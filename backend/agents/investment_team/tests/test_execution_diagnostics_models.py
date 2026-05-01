"""Model tests for Strategy Lab execution diagnostics (#407)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from investment_team.models import (
    BacktestExecutionDiagnostics,
    BacktestResult,
)


def _backtest_payload() -> dict[str, float]:
    return {
        "total_return_pct": 0.0,
        "annualized_return_pct": 0.0,
        "volatility_pct": 0.0,
        "sharpe_ratio": 0.0,
        "max_drawdown_pct": 0.0,
        "win_rate_pct": 0.0,
        "profit_factor": 0.0,
    }


def test_backtest_result_construction_stays_backward_compatible() -> None:
    result = BacktestResult(**_backtest_payload())

    assert result.execution_diagnostics is None


def test_backtest_result_model_validate_accepts_legacy_payload() -> None:
    result = BacktestResult.model_validate(_backtest_payload())

    assert result.execution_diagnostics is None


def test_execution_diagnostics_defaults_are_empty() -> None:
    diagnostics = BacktestExecutionDiagnostics()

    assert diagnostics.zero_trade_category is None
    assert diagnostics.summary == ""
    assert diagnostics.bars_processed == 0
    assert diagnostics.orders_emitted == 0
    assert diagnostics.orders_accepted == 0
    assert diagnostics.orders_rejected == 0
    assert diagnostics.orders_rejection_reasons == {}
    assert diagnostics.orders_unfilled == 0
    assert diagnostics.warmup_orders_dropped == 0
    assert diagnostics.entries_filled == 0
    assert diagnostics.exits_emitted == 0
    assert diagnostics.closed_trades == 0
    assert diagnostics.open_positions_at_end == []
    assert diagnostics.last_order_events == []


def test_execution_diagnostics_validates_populated_payload() -> None:
    diagnostics = BacktestExecutionDiagnostics.model_validate(
        {
            "zero_trade_category": "ENTRY_WITH_NO_EXIT",
            "summary": "Entry filled but no exit was emitted.",
            "bars_processed": 42,
            "orders_emitted": 1,
            "orders_accepted": 1,
            "orders_rejected": 0,
            "orders_rejection_reasons": {},
            "orders_unfilled": 0,
            "warmup_orders_dropped": 0,
            "entries_filled": 1,
            "exits_emitted": 0,
            "closed_trades": 0,
            "open_positions_at_end": [
                {
                    "symbol": "AAPL",
                    "side": "long",
                    "qty": 10.0,
                    "entry_price": 125.5,
                    "entry_timestamp": "2024-01-03",
                }
            ],
            "last_order_events": [
                {
                    "event_type": "entry_filled",
                    "timestamp": "2024-01-03",
                    "symbol": "AAPL",
                    "side": "long",
                    "order_type": "market",
                    "reason": "test-entry",
                    "detail": "filled next bar",
                }
            ],
        }
    )

    assert diagnostics.zero_trade_category == "ENTRY_WITH_NO_EXIT"
    assert diagnostics.open_positions_at_end[0].symbol == "AAPL"
    assert diagnostics.last_order_events[0].event_type == "entry_filled"


def test_invalid_zero_trade_category_fails_validation() -> None:
    with pytest.raises(ValidationError):
        BacktestExecutionDiagnostics(zero_trade_category="NOT_A_CATEGORY")


def test_negative_counter_values_fail_validation() -> None:
    with pytest.raises(ValidationError):
        BacktestExecutionDiagnostics(orders_emitted=-1)


def test_extra_fields_are_ignored_inside_diagnostics_models() -> None:
    diagnostics = BacktestExecutionDiagnostics.model_validate(
        {
            "extra_top_level": "ignored",
            "open_positions_at_end": [
                {
                    "symbol": "MSFT",
                    "side": "short",
                    "qty": 5.0,
                    "entry_price": 300.0,
                    "entry_timestamp": "2024-01-04",
                    "extra_position": "ignored",
                }
            ],
            "last_order_events": [
                {
                    "event_type": "emitted",
                    "symbol": "MSFT",
                    "extra_event": "ignored",
                }
            ],
        }
    )

    dumped = diagnostics.model_dump()
    assert "extra_top_level" not in dumped
    assert "extra_position" not in diagnostics.open_positions_at_end[0].model_dump()
    assert "extra_event" not in diagnostics.last_order_events[0].model_dump()


def test_backtest_result_with_diagnostics_dumps_as_json_serializable_dict() -> None:
    result = BacktestResult(
        **_backtest_payload(),
        execution_diagnostics=BacktestExecutionDiagnostics(
            zero_trade_category="NO_ORDERS_EMITTED",
            summary="No orders were emitted.",
            bars_processed=10,
            last_order_events=[
                {
                    "event_type": "unfilled",
                    "timestamp": "2024-01-05",
                    "symbol": "AAPL",
                    "detail": "end_of_stream_pending",
                }
            ],
        ),
    )

    dumped = result.model_dump(mode="json")
    json.dumps(dumped)

    assert dumped["execution_diagnostics"]["zero_trade_category"] == "NO_ORDERS_EMITTED"
    assert dumped["execution_diagnostics"]["last_order_events"][0]["event_type"] == "unfilled"
