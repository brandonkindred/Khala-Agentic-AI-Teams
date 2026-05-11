"""Tests for the coverage-probe orchestrator stage (#451)."""

from __future__ import annotations

import sys
import textwrap
from typing import Any, Dict, List, Optional

import pandas as pd
import pytest

from investment_team.models import (
    BacktestConfig,
    BacktestExecutionDiagnostics,
    CoverageCategory,
    CoverageReport,
    LikelyBlocker,
    StrategySpec,
    SubconditionCoverage,
)
from investment_team.strategy_lab.coverage_probe import (
    LOW_TRADE_THRESHOLD,
    merge_reports,
    run_coverage_stage,
    should_run_probes,
)
from investment_team.strategy_lab.coverage_probe import aggregator as agg_mod
from investment_team.trading_service.modes.sandbox_compat import StrategyRunResult

# ─────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-06-30",
        initial_capital=100_000.0,
        benchmark_symbol="SPY",
        transaction_cost_bps=5.0,
        slippage_bps=2.0,
    )


def _spec(strategy_code: str | None) -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-coverage-stage-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis="hyp",
        signal_definition="sig",
        entry_rules=["enter when RSI < 25"],
        exit_rules=["exit when RSI > 70"],
        sizing_rules=["risk 2% per trade"],
        risk_limits={"max_position_pct": 5},
        speculative=False,
        strategy_code=strategy_code,
    )


def _flat_df(n: int, close: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {
            "open": [close] * n,
            "high": [close + 1.0] * n,
            "low": [close - 1.0] * n,
            "close": [close] * n,
            "volume": [1_000_000] * n,
        },
        index=idx,
    )


def _diag(
    *,
    category: Optional[str] = None,
    closed: int = 0,
    orders_accepted: int = 0,
) -> BacktestExecutionDiagnostics:
    return BacktestExecutionDiagnostics(
        zero_trade_category=category,  # type: ignore[arg-type]
        closed_trades=closed,
        orders_accepted=orders_accepted,
    )


def _exec_result(diagnostics: Optional[BacktestExecutionDiagnostics]) -> StrategyRunResult:
    return StrategyRunResult(
        success=True,
        trades=[],
        execution_diagnostics=diagnostics,
    )


def _never_called_run_strategy_code(*args: Any, **kwargs: Any) -> StrategyRunResult:
    raise AssertionError("run_strategy_code must not be invoked")


# ─────────────────────────────────────────────────────────────────────
# should_run_probes
# ─────────────────────────────────────────────────────────────────────


def test_should_run_probes_returns_false_when_diagnostics_missing() -> None:
    assert should_run_probes(None) is False


def test_should_run_probes_returns_false_for_healthy_run() -> None:
    diag = _diag(closed=10)
    assert should_run_probes(diag) is False


def test_should_run_probes_triggers_on_no_orders_emitted() -> None:
    diag = _diag(category="NO_ORDERS_EMITTED", closed=0)
    assert should_run_probes(diag) is True


def test_should_run_probes_triggers_on_only_warmup_orders() -> None:
    diag = _diag(category="ONLY_WARMUP_ORDERS", closed=0)
    assert should_run_probes(diag) is True


def test_should_run_probes_triggers_on_unknown_zero_trade_path() -> None:
    diag = _diag(category="UNKNOWN_ZERO_TRADE_PATH", closed=0)
    assert should_run_probes(diag) is True


def test_should_run_probes_triggers_on_low_closed_trades() -> None:
    diag = _diag(closed=LOW_TRADE_THRESHOLD - 1)
    assert should_run_probes(diag) is True


def test_should_run_probes_does_not_trigger_for_other_lifecycle_failures() -> None:
    # ORDERS_REJECTED already has a structured envelope (#404); coverage
    # probes can't add anything, so the stage stays off.
    diag = _diag(category="ORDERS_REJECTED", closed=10)
    assert should_run_probes(diag) is False


# ─────────────────────────────────────────────────────────────────────
# merge_reports
# ─────────────────────────────────────────────────────────────────────


def _report(
    category: CoverageCategory,
    *,
    subconditions: Optional[List[SubconditionCoverage]] = None,
    blockers: Optional[List[LikelyBlocker]] = None,
    warmup: int = 0,
    bars: int = 0,
    symbols: int = 0,
) -> CoverageReport:
    return CoverageReport(
        coverage_category=category,
        subconditions=subconditions or [],
        likely_blockers=blockers or [],
        warmup_bars_required=warmup,
        bars_checked=bars,
        symbols_checked=symbols,
    )


def test_merge_warmup_exceeds_history_beats_indicator_restrictive() -> None:
    static = _report(CoverageCategory.WARMUP_EXCEEDS_HISTORY, warmup=200, bars=120)
    indicator = _report(CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE)
    merged = merge_reports(static, indicator)
    assert merged.coverage_category is CoverageCategory.WARMUP_EXCEEDS_HISTORY


def test_merge_target_symbol_missing_beats_conjunction_never_true() -> None:
    static = _report(CoverageCategory.TARGET_SYMBOL_MISSING)
    indicator = _report(CoverageCategory.CONJUNCTION_NEVER_TRUE)
    merged = merge_reports(static, indicator)
    assert merged.coverage_category is CoverageCategory.TARGET_SYMBOL_MISSING


def test_merge_indicator_restrictive_beats_unknown_static() -> None:
    static = _report(CoverageCategory.UNKNOWN_LOW_COVERAGE)
    indicator = _report(CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE)
    merged = merge_reports(static, indicator)
    assert merged.coverage_category is CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE


def test_merge_both_ok_returns_ok() -> None:
    merged = merge_reports(
        _report(CoverageCategory.COVERAGE_OK),
        _report(CoverageCategory.COVERAGE_OK),
    )
    assert merged.coverage_category is CoverageCategory.COVERAGE_OK


def test_merge_dedups_blockers_and_preserves_order() -> None:
    static = _report(
        CoverageCategory.UNKNOWN_LOW_COVERAGE,
        blockers=[
            LikelyBlocker(reason="first", evidence="static-evidence"),
            LikelyBlocker(reason="dup", evidence="x"),
        ],
    )
    indicator = _report(
        CoverageCategory.UNKNOWN_LOW_COVERAGE,
        blockers=[
            LikelyBlocker(reason="dup", evidence="x"),
            LikelyBlocker(reason="second", evidence="indicator-evidence"),
        ],
    )
    merged = merge_reports(static, indicator)
    reasons = [b.reason for b in merged.likely_blockers]
    assert reasons == ["first", "dup", "second"]


def test_merge_takes_max_of_numeric_fields() -> None:
    static = _report(
        CoverageCategory.UNKNOWN_LOW_COVERAGE,
        warmup=80,
        bars=250,
        symbols=1,
    )
    indicator = _report(
        CoverageCategory.UNKNOWN_LOW_COVERAGE,
        warmup=120,
        bars=200,
        symbols=3,
    )
    merged = merge_reports(static, indicator)
    assert merged.warmup_bars_required == 120
    assert merged.bars_checked == 250
    assert merged.symbols_checked == 3


def test_merge_uses_exec_diag_for_entry_orders_emitted() -> None:
    merged = merge_reports(
        _report(CoverageCategory.COVERAGE_OK),
        _report(CoverageCategory.COVERAGE_OK),
        exec_diag=_diag(orders_accepted=7),
    )
    assert merged.entry_orders_emitted == 7


def test_merge_is_deterministic_across_calls() -> None:
    static = _report(
        CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE,
        blockers=[LikelyBlocker(reason="r1", evidence="e1")],
    )
    indicator = _report(
        CoverageCategory.CONJUNCTION_NEVER_TRUE,
        blockers=[LikelyBlocker(reason="r2", evidence="e2")],
    )
    first = merge_reports(static, indicator)
    second = merge_reports(static, indicator)
    assert first.model_dump() == second.model_dump()


# ─────────────────────────────────────────────────────────────────────
# run_coverage_stage — static short-circuit
# ─────────────────────────────────────────────────────────────────────


def test_static_warmup_short_circuits_indicator_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    # WINDOW=9999 against only 50 bars triggers WARMUP_EXCEEDS_HISTORY.
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            WINDOW = 9999

            def on_bar(self, ctx, bar):
                bars = ctx.history(bar.symbol, self.WINDOW)
                if len(bars) < self.WINDOW:
                    return
        """
    )
    indicator_called: List[bool] = []

    def _spy_indicator_probe(*args: Any, **kwargs: Any) -> CoverageReport:
        indicator_called.append(True)
        return _report(CoverageCategory.COVERAGE_OK)

    monkeypatch.setattr(agg_mod, "run_indicator_probe", _spy_indicator_probe)

    report = run_coverage_stage(
        spec=_spec(code),
        market_data={"AAPL": _flat_df(50)},
        config=_config(),
        exec_result=_exec_result(_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=_never_called_run_strategy_code,
    )

    assert report.coverage_category is CoverageCategory.WARMUP_EXCEEDS_HISTORY
    assert indicator_called == []


def test_static_missing_symbol_short_circuits_indicator_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                ctx.submit_order(symbol="TSLA", side="long", qty=1)
        """
    )
    indicator_called: List[bool] = []

    def _spy_indicator_probe(*args: Any, **kwargs: Any) -> CoverageReport:
        indicator_called.append(True)
        return _report(CoverageCategory.COVERAGE_OK)

    monkeypatch.setattr(agg_mod, "run_indicator_probe", _spy_indicator_probe)

    report = run_coverage_stage(
        spec=_spec(code),
        market_data={"AAPL": _flat_df(120)},
        config=_config(),
        exec_result=_exec_result(_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=_never_called_run_strategy_code,
    )

    assert report.coverage_category is CoverageCategory.TARGET_SYMBOL_MISSING
    assert indicator_called == []


# ─────────────────────────────────────────────────────────────────────
# run_coverage_stage — runtime re-execution gating
# ─────────────────────────────────────────────────────────────────────


def test_runtime_reexecution_skipped_when_indicator_is_conclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                if bar.close > 999999.0:
                    ctx.submit_order(symbol=bar.symbol, side="long", qty=1)
        """
    )

    def _stub_indicator_probe(*args: Any, **kwargs: Any) -> CoverageReport:
        return _report(CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE)

    monkeypatch.setattr(agg_mod, "run_indicator_probe", _stub_indicator_probe)

    report = run_coverage_stage(
        spec=_spec(code),
        market_data={"AAPL": _flat_df(120)},
        config=_config(),
        exec_result=_exec_result(_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=_never_called_run_strategy_code,
    )
    assert report.coverage_category is CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE


def test_runtime_reexecution_runs_when_static_and_indicator_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                if self.custom_helper(bar):
                    ctx.submit_order(symbol=bar.symbol, side="long", qty=1)
        """
    )

    def _stub_indicator_probe(*args: Any, **kwargs: Any) -> CoverageReport:
        return _report(CoverageCategory.UNKNOWN_LOW_COVERAGE)

    monkeypatch.setattr(agg_mod, "run_indicator_probe", _stub_indicator_probe)

    runtime_calls: List[Dict[str, Any]] = []

    def _fake_run_strategy_code(*args: Any, **kwargs: Any) -> StrategyRunResult:
        runtime_calls.append({"args": args, "kwargs": kwargs})
        return StrategyRunResult(
            success=True,
            trades=[],
            probe_events={
                "events": [
                    {
                        "rule_id": "r0",
                        "hit_count": 5,
                        "first_true_bar": 12,
                        "last_true_bar": 87,
                    }
                ],
                "truncated": False,
            },
        )

    report = run_coverage_stage(
        spec=_spec(code),
        market_data={"AAPL": _flat_df(120)},
        config=_config(),
        exec_result=_exec_result(_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=_fake_run_strategy_code,
    )

    assert len(runtime_calls) == 1
    assert runtime_calls[0]["kwargs"].get("coverage_probe_mode") is True
    runtime_blockers = [b for b in report.likely_blockers if b.reason.startswith("runtime:")]
    assert runtime_blockers, "expected at least one runtime: blocker"
    assert "runtime_events=1" in report.summary


def test_runtime_reexecution_failure_records_failure_blocker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                if self.custom_helper(bar):
                    ctx.submit_order(symbol=bar.symbol, side="long", qty=1)
        """
    )

    def _stub_indicator_probe(*args: Any, **kwargs: Any) -> CoverageReport:
        return _report(CoverageCategory.UNKNOWN_LOW_COVERAGE)

    monkeypatch.setattr(agg_mod, "run_indicator_probe", _stub_indicator_probe)

    def _failing_run_strategy_code(*args: Any, **kwargs: Any) -> StrategyRunResult:
        return StrategyRunResult(
            success=False,
            trades=[],
            error_type="runtime_error",
            stderr="boom",
        )

    report = run_coverage_stage(
        spec=_spec(code),
        market_data={"AAPL": _flat_df(120)},
        config=_config(),
        exec_result=_exec_result(_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=_failing_run_strategy_code,
    )

    failure_blockers = [b for b in report.likely_blockers if b.reason == "runtime_probe_failed"]
    assert failure_blockers, "expected runtime_probe_failed blocker"
    assert report.coverage_category is CoverageCategory.UNKNOWN_LOW_COVERAGE


def test_runtime_reexecution_skipped_when_no_instrumentable_predicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No on_bar at all → instrument_strategy_code returns an empty
    # RuleIndex. The stage must record that as a skip-blocker rather
    # than spinning up the harness.
    code = "X = 1\n"

    def _stub_indicator_probe(*args: Any, **kwargs: Any) -> CoverageReport:
        return _report(CoverageCategory.UNKNOWN_LOW_COVERAGE)

    monkeypatch.setattr(agg_mod, "run_indicator_probe", _stub_indicator_probe)

    report = run_coverage_stage(
        spec=_spec(code),
        market_data={"AAPL": _flat_df(120)},
        config=_config(),
        exec_result=_exec_result(_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=_never_called_run_strategy_code,
    )

    skipped = [b for b in report.likely_blockers if b.reason == "runtime_probe_skipped"]
    assert skipped


# ─────────────────────────────────────────────────────────────────────
# run_coverage_stage — determinism & LLM-free guarantees
# ─────────────────────────────────────────────────────────────────────


def test_no_llm_calls_made(monkeypatch: pytest.MonkeyPatch) -> None:
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                if bar.close > 999999.0:
                    ctx.submit_order(symbol=bar.symbol, side="long", qty=1)
        """
    )
    pre_modules = {
        name for name in sys.modules if name.startswith(("llm_service", "anthropic", "openai"))
    }

    run_coverage_stage(
        spec=_spec(code),
        market_data={"AAPL": _flat_df(120)},
        config=_config(),
        exec_result=_exec_result(_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=_never_called_run_strategy_code,
    )

    post_modules = {
        name for name in sys.modules if name.startswith(("llm_service", "anthropic", "openai"))
    }
    assert post_modules == pre_modules


def test_coverage_stage_is_deterministic() -> None:
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                if bar.close > 999999.0:
                    ctx.submit_order(symbol=bar.symbol, side="long", qty=1)
        """
    )
    market_data = {"AAPL": _flat_df(120)}

    first = run_coverage_stage(
        spec=_spec(code),
        market_data=market_data,
        config=_config(),
        exec_result=_exec_result(_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=_never_called_run_strategy_code,
    )
    second = run_coverage_stage(
        spec=_spec(code),
        market_data=market_data,
        config=_config(),
        exec_result=_exec_result(_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=_never_called_run_strategy_code,
    )
    assert first.model_dump() == second.model_dump()
