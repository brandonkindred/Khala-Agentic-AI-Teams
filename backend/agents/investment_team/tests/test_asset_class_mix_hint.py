"""Regression tests for ``asset_class_mix_hint`` (#535).

Verifies that ``options`` — which the validator now rejects as a
critical gate failure — is no longer surfaced as an underrepresented
asset class the LLM should favor.
"""

from __future__ import annotations

import uuid

from investment_team.api import main as lab_main
from investment_team.models import (
    BacktestConfig,
    BacktestRecord,
    BacktestResult,
    StrategyLabRecord,
    StrategySpec,
)
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    Predicate,
    StopLossRule,
)
from investment_team.strategy_lab_context import asset_class_mix_hint


def _stub_backtest_result() -> BacktestResult:
    return BacktestResult(
        total_return_pct=10.0,
        annualized_return_pct=5.0,
        volatility_pct=12.0,
        sharpe_ratio=0.5,
        max_drawdown_pct=-3.0,
        win_rate_pct=55.0,
        profit_factor=1.2,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )


def _record(asset_class: str) -> StrategyLabRecord:
    suffix = uuid.uuid4().hex[:6]
    strategy = StrategySpec(
        strategy_id=f"s-{suffix}",
        authored_by="test",
        asset_class=asset_class,
        hypothesis="h",
        signal_definition="sig",
        timeframe="1d",
        entry_rules=[EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=0))],
        exit_rules=[StopLossRule(pct=0.03)],
        risk_limits={},
        speculative=False,
    )
    now = lab_main._now()
    backtest = BacktestRecord(
        backtest_id=f"bt-{suffix}",
        strategy_id=strategy.strategy_id,
        strategy=strategy,
        config=BacktestConfig(start_date="2024-01-01", end_date="2024-12-31"),
        submitted_by="test",
        submitted_at=now,
        completed_at=now,
        result=_stub_backtest_result(),
        notes=[],
        trades=[],
    )
    return StrategyLabRecord(
        lab_record_id=f"lab-{suffix}",
        strategy=strategy,
        backtest=backtest,
        is_winning=False,
        strategy_rationale="r",
        analysis_narrative="ok",
        created_at=now,
        quality_gate_results=[],
    )


def test_hint_no_records_omits_options() -> None:
    """With no prior strategies the menu of choices must not include options."""
    out = asset_class_mix_hint([])
    assert "options" not in out.lower(), out


def test_hint_with_history_never_lists_options_as_underrepresented() -> None:
    """A history of stocks-heavy runs would have left options=0 in the count
    dict before the fix, sending the LLM to options as 'underrepresented'."""
    records = [_record("stocks") for _ in range(5)] + [_record("crypto")]
    out = asset_class_mix_hint(records)

    # The recent-counts breakdown must not enumerate options at all.
    assert "options=" not in out, out
    # The underrepresented-line steering must not name options.
    assert "options" not in out.lower(), out


def test_hint_count_includes_supported_classes() -> None:
    """The supported classes should still appear in the count breakdown so
    the LLM gets useful diversification steering."""
    records = [_record("stocks") for _ in range(3)]
    out = asset_class_mix_hint(records)
    for ac in ("stocks", "crypto", "forex", "futures", "commodities"):
        assert f"{ac}=" in out, f"expected '{ac}=' in counts, got: {out}"
