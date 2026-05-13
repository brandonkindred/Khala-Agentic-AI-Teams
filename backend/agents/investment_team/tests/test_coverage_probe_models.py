"""Model tests for Strategy Lab rule-coverage probes (#446)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from investment_team.models import (
    BacktestResult,
    CoverageCategory,
    CoverageReport,
    LikelyBlocker,
    SubconditionCoverage,
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
        # #378: sortino_ratio / calmar_ratio / deflated_sharpe are now
        # required (no Optional defaults, no None coercion).
        "sortino_ratio": 0.0,
        "calmar_ratio": 0.0,
        "deflated_sharpe": 0.0,
    }


def test_backtest_result_construction_stays_backward_compatible() -> None:
    result = BacktestResult(**_backtest_payload())

    assert result.coverage_report is None


def test_backtest_result_model_validate_accepts_legacy_payload() -> None:
    result = BacktestResult.model_validate(_backtest_payload())

    assert result.coverage_report is None


def test_coverage_report_defaults_are_empty() -> None:
    report = CoverageReport()

    assert report.coverage_category is CoverageCategory.UNKNOWN_LOW_COVERAGE
    assert report.summary == ""
    assert report.symbols_checked == 0
    assert report.bars_checked == 0
    assert report.warmup_bars_required == 0
    assert report.entry_orders_emitted == 0
    assert report.subconditions == []
    assert report.likely_blockers == []


def test_coverage_report_validates_populated_payload() -> None:
    report = CoverageReport.model_validate(
        {
            "coverage_category": "ENTRY_CONDITION_NEVER_TRUE",
            "summary": "Combined RSI<25 and close>SMA200 never occurred.",
            "symbols_checked": 5,
            "bars_checked": 1250,
            "warmup_bars_required": 80,
            "entry_orders_emitted": 0,
            "subconditions": [
                {
                    "label": "rsi < 25",
                    "hit_count": 0,
                    "hit_rate": 0.0,
                    "last_true_bar": None,
                },
                {
                    "label": "close > sma200",
                    "hit_count": 612,
                    "hit_rate": 0.49,
                    "last_true_bar": "2025-09-30",
                },
            ],
            "likely_blockers": [
                {
                    "reason": "volume_filter_hit_rate=0.0%",
                    "evidence": "volume > avg_volume * 1.5 never true over 1250 bars",
                    "hit_rate": 0.0,
                },
                {
                    "reason": "target symbol TSLA not present in fetched universe",
                    "evidence": "fetched={AAPL, MSFT, NVDA, GOOG, AMZN}",
                },
            ],
        }
    )

    assert report.coverage_category is CoverageCategory.ENTRY_CONDITION_NEVER_TRUE
    assert report.subconditions[0].label == "rsi < 25"
    assert report.subconditions[1].last_true_bar == "2025-09-30"
    assert report.likely_blockers[1].hit_rate is None


def test_invalid_coverage_category_fails_validation() -> None:
    with pytest.raises(ValidationError):
        CoverageReport(coverage_category="NOT_A_CATEGORY")


def test_negative_counter_values_fail_validation() -> None:
    with pytest.raises(ValidationError):
        CoverageReport(bars_checked=-1)


def test_hit_rate_outside_unit_interval_fails_validation() -> None:
    with pytest.raises(ValidationError):
        SubconditionCoverage(label="rsi < 25", hit_count=0, hit_rate=1.5)

    with pytest.raises(ValidationError):
        SubconditionCoverage(label="rsi < 25", hit_count=0, hit_rate=-0.1)

    with pytest.raises(ValidationError):
        LikelyBlocker(reason="bad rate", hit_rate=1.5)

    with pytest.raises(ValidationError):
        LikelyBlocker(reason="bad rate", hit_rate=-0.1)


def test_extra_fields_are_ignored_inside_coverage_models() -> None:
    report = CoverageReport.model_validate(
        {
            "coverage_category": "WARMUP_EXCEEDS_HISTORY",
            "extra_top_level": "ignored",
            "subconditions": [
                {
                    "label": "rsi < 25",
                    "hit_count": 1,
                    "hit_rate": 0.001,
                    "extra_subcondition": "ignored",
                }
            ],
            "likely_blockers": [
                {
                    "reason": "warmup=200 > history=120",
                    "extra_blocker": "ignored",
                }
            ],
        }
    )

    dumped = report.model_dump()
    assert "extra_top_level" not in dumped
    assert "extra_subcondition" not in report.subconditions[0].model_dump()
    assert "extra_blocker" not in report.likely_blockers[0].model_dump()


def test_backtest_result_with_coverage_dumps_as_json_serializable_dict() -> None:
    result = BacktestResult(
        **_backtest_payload(),
        coverage_report=CoverageReport(
            coverage_category=CoverageCategory.TARGET_SYMBOL_MISSING,
            summary="Target symbol TSLA not in fetched universe.",
            symbols_checked=5,
            bars_checked=1250,
            warmup_bars_required=200,
            entry_orders_emitted=0,
            likely_blockers=[
                LikelyBlocker(
                    reason="target symbol TSLA not present in fetched universe",
                    evidence="fetched={AAPL, MSFT}",
                ),
            ],
        ),
    )

    dumped = result.model_dump(mode="json")
    json.dumps(dumped)

    assert dumped["coverage_report"]["coverage_category"] == "TARGET_SYMBOL_MISSING"
    assert dumped["coverage_report"]["likely_blockers"][0]["reason"].startswith(
        "target symbol TSLA"
    )
