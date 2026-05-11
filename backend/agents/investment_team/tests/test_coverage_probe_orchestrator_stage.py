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


def test_merge_warmup_takes_max_across_reports() -> None:
    static = _report(CoverageCategory.UNKNOWN_LOW_COVERAGE, warmup=80)
    indicator = _report(CoverageCategory.UNKNOWN_LOW_COVERAGE, warmup=120)
    merged = merge_reports(static, indicator)
    # Warmup is a per-symbol bars count; both probes use the same unit
    # so max() is safe.
    assert merged.warmup_bars_required == 120


def test_merge_bars_and_symbols_take_indicator_values() -> None:
    # bars_checked / symbols_checked are reported in different units by
    # the two probes (static = longest single-symbol history; indicator =
    # sum across symbols). merge_reports trusts the indicator probe's
    # values because they reflect what was actually examined for hit-rate
    # computation, which is what downstream consumers (refinement prompt
    # in #452) want.
    static = _report(
        CoverageCategory.UNKNOWN_LOW_COVERAGE,
        bars=250,  # longest single symbol
        symbols=1,
    )
    indicator = _report(
        CoverageCategory.UNKNOWN_LOW_COVERAGE,
        bars=200,  # sum across symbols actually examined
        symbols=3,
    )
    merged = merge_reports(static, indicator)
    assert merged.bars_checked == 200
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
    assert "runtime_error" in failure_blockers[0].evidence
    assert "runtime_events=failed" in report.summary
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


# ─────────────────────────────────────────────────────────────────────
# Module-level exhaustiveness guard
# ─────────────────────────────────────────────────────────────────────


def test_category_priority_covers_every_coverage_category() -> None:
    # The aggregator module asserts this at import time, but the
    # explicit test pins the contract so a future enum addition fails
    # here with a clear name rather than as an opaque ValueError from
    # tuple.index() deep inside the orchestrator.
    assert set(agg_mod._CATEGORY_PRIORITY) == set(CoverageCategory)


# ─────────────────────────────────────────────────────────────────────
# Orchestrator integration — helper wiring
#
# The bug these tests guard against: each of the three orchestrator
# call sites (primary / alignment / repair) must pass the spec that
# carries the same ``strategy_code`` the executor actually ran. The
# alignment site is the failure mode — it has both ``spec`` (stale)
# and ``proposed_spec`` (current) in scope.
# ─────────────────────────────────────────────────────────────────────


def test_maybe_attach_coverage_report_no_ops_when_gate_off() -> None:
    from investment_team.models import BacktestResult
    from investment_team.strategy_lab.orchestrator import _maybe_attach_coverage_report

    metrics = BacktestResult(
        total_return_pct=5.0,
        annualized_return_pct=5.0,
        volatility_pct=10.0,
        sharpe_ratio=1.0,
        max_drawdown_pct=2.0,
        win_rate_pct=55.0,
        profit_factor=1.5,
    )
    # Healthy diagnostics — gate is off.
    _maybe_attach_coverage_report(
        metrics=metrics,
        spec=_spec("X = 1\n"),
        market_data={"AAPL": _flat_df(50)},
        config=_config(),
        exec_result=_exec_result(_diag(closed=10)),
    )
    assert metrics.coverage_report is None


def test_maybe_attach_coverage_report_runs_stage_when_gate_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from investment_team.models import BacktestResult
    from investment_team.strategy_lab import orchestrator as orch_mod
    from investment_team.strategy_lab.orchestrator import _maybe_attach_coverage_report

    captured: List[Dict[str, Any]] = []

    def _spy_run_coverage_stage(**kwargs: Any) -> CoverageReport:
        captured.append(kwargs)
        return _report(CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE)

    monkeypatch.setattr(orch_mod, "run_coverage_stage", _spy_run_coverage_stage)

    metrics = BacktestResult(
        total_return_pct=0.0,
        annualized_return_pct=0.0,
        volatility_pct=0.0,
        sharpe_ratio=0.0,
        max_drawdown_pct=0.0,
        win_rate_pct=0.0,
        profit_factor=0.0,
    )
    spec = _spec("Y = 2\n")
    _maybe_attach_coverage_report(
        metrics=metrics,
        spec=spec,
        market_data={"AAPL": _flat_df(50)},
        config=_config(),
        exec_result=_exec_result(_diag(category="NO_ORDERS_EMITTED", closed=0)),
    )
    assert metrics.coverage_report is not None
    assert metrics.coverage_report.coverage_category is (
        CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE
    )
    assert len(captured) == 1
    assert captured[0]["spec"] is spec


def test_orchestrator_call_sites_use_consistent_spec_and_exec_result() -> None:
    # Source-level regression: every ``_maybe_attach_coverage_report``
    # call site must pair an ``exec_result`` with the spec whose
    # ``strategy_code`` matches the code that produced it. The
    # alignment-site bug was passing the loop-level ``spec`` alongside
    # ``align_exec`` (where ``proposed_spec`` is the source of truth).
    # Lock the AST shape so a future refactor that re-introduces the
    # drift fails loudly here.
    import ast
    import inspect

    from investment_team.strategy_lab import orchestrator as orch_mod

    source = inspect.getsource(orch_mod)
    tree = ast.parse(source)

    # The contract: exec_result kwarg names must each have a
    # matching spec kwarg name following a known-good pairing rule.
    expected_spec_for_exec = {
        "exec_result": "spec",
        "align_exec": "proposed_spec",
        "repair_exec": "proposed_spec",
    }

    sites: List[Dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name != "_maybe_attach_coverage_report":
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        spec_arg = kwargs.get("spec")
        exec_arg = kwargs.get("exec_result")
        lineno = node.lineno
        assert isinstance(spec_arg, ast.Name), (
            f"orchestrator.py:{lineno} — spec kwarg must be a bare Name reference"
        )
        assert isinstance(exec_arg, ast.Name), (
            f"orchestrator.py:{lineno} — exec_result kwarg must be a bare Name reference"
        )
        sites.append({"spec": spec_arg.id, "exec_result": exec_arg.id, "lineno": lineno})

    assert len(sites) == 3, f"expected 3 _maybe_attach_coverage_report sites, found {sites}"
    for site in sites:
        expected_spec = expected_spec_for_exec.get(site["exec_result"])
        assert expected_spec is not None, (
            f"orchestrator.py:{site['lineno']} — unknown exec_result variable "
            f"{site['exec_result']!r}; if a new orchestrator call site is "
            f"intentional, update expected_spec_for_exec in this test"
        )
        assert site["spec"] == expected_spec, (
            f"orchestrator.py:{site['lineno']} — _maybe_attach_coverage_report "
            f"site mismatched spec/exec_result: got spec={site['spec']!r} with "
            f"exec_result={site['exec_result']!r}; expected spec={expected_spec!r}"
        )


# ─────────────────────────────────────────────────────────────────────
# Runtime probe outcome distinctions (B1, B2)
#
# These pin down the four distinct outcomes of the runtime stage so the
# refinement prompt (#452) can render them honestly:
#   - "n/a"     → didn't run (static short-circuited, or merged != UNKNOWN)
#   - "<N>"     → ran cleanly, N events emitted (0 is informative!)
#   - "failed"  → subprocess crashed
#   - "no_frame"→ subprocess ran cleanly but emitted no probe_events
#   - "skipped" → preconditions unmet (empty code, no on_bar)
# ─────────────────────────────────────────────────────────────────────


def _unknown_indicator(*args: Any, **kwargs: Any) -> CoverageReport:
    """Force ``run_coverage_stage`` into the runtime-reexecution branch."""
    return _report(CoverageCategory.UNKNOWN_LOW_COVERAGE)


def _runtime_capable_code() -> str:
    return textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            def on_bar(self, ctx, bar):
                if self.custom_helper(bar):
                    ctx.submit_order(symbol=bar.symbol, side="long", qty=1)
        """
    )


def test_runtime_zero_events_records_no_hits_blocker_and_zero_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # B1: a clean run with an empty events list is a strong "predicates
    # never fired" signal — must be surfaced as a distinct blocker, and
    # the summary must say "runtime_events=0" (not "n/a").
    monkeypatch.setattr(agg_mod, "run_indicator_probe", _unknown_indicator)

    def _fake_run_strategy_code(*args: Any, **kwargs: Any) -> StrategyRunResult:
        return StrategyRunResult(
            success=True,
            trades=[],
            probe_events={"events": [], "truncated": False},
        )

    report = run_coverage_stage(
        spec=_spec(_runtime_capable_code()),
        market_data={"AAPL": _flat_df(120)},
        config=_config(),
        exec_result=_exec_result(_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=_fake_run_strategy_code,
    )

    no_hits = [b for b in report.likely_blockers if b.reason == "runtime_probe_no_hits"]
    assert no_hits, "expected a runtime_probe_no_hits blocker"
    assert "hits=0" in no_hits[0].evidence
    assert "runtime_events=0" in report.summary


def test_runtime_success_with_no_probe_events_field_records_no_frame_blocker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # B2: success=True but probe_events=None must be distinguishable
    # from a hard failure. Surface as a separate blocker so #452 can
    # decide whether to retry the run.
    monkeypatch.setattr(agg_mod, "run_indicator_probe", _unknown_indicator)

    def _no_frame_run(*args: Any, **kwargs: Any) -> StrategyRunResult:
        return StrategyRunResult(success=True, trades=[], probe_events=None)

    report = run_coverage_stage(
        spec=_spec(_runtime_capable_code()),
        market_data={"AAPL": _flat_df(120)},
        config=_config(),
        exec_result=_exec_result(_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=_no_frame_run,
    )

    no_frame = [b for b in report.likely_blockers if b.reason == "runtime_probe_no_frame"]
    assert no_frame, "expected a runtime_probe_no_frame blocker"
    assert "runtime_events=no_frame" in report.summary
    failure = [b for b in report.likely_blockers if b.reason == "runtime_probe_failed"]
    assert not failure, "no_frame must not be conflated with failure"


def test_runtime_skipped_summary_token_for_empty_strategy_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Runtime stage's "skipped" outcome (empty strategy_code) must show
    # up as ``runtime_events=skipped`` in the summary.
    monkeypatch.setattr(agg_mod, "run_indicator_probe", _unknown_indicator)

    report = run_coverage_stage(
        spec=_spec(""),
        market_data={"AAPL": _flat_df(120)},
        config=_config(),
        exec_result=_exec_result(_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=_never_called_run_strategy_code,
    )
    skipped = [b for b in report.likely_blockers if b.reason == "runtime_probe_skipped"]
    assert skipped
    assert "spec.strategy_code is empty" in skipped[0].evidence
    assert "runtime_events=skipped" in report.summary


def test_runtime_uncaught_exception_is_logged_and_recorded(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Q3: the runtime exception path must use ``logger.exception`` so
    # the traceback is preserved under DEBUG logging, not silently
    # discarded.
    import logging

    monkeypatch.setattr(agg_mod, "run_indicator_probe", _unknown_indicator)

    def _raising_run(*args: Any, **kwargs: Any) -> StrategyRunResult:
        raise RuntimeError("sandbox blew up")

    with caplog.at_level(
        logging.DEBUG, logger="investment_team.strategy_lab.coverage_probe.aggregator"
    ):
        report = run_coverage_stage(
            spec=_spec(_runtime_capable_code()),
            market_data={"AAPL": _flat_df(120)},
            config=_config(),
            exec_result=_exec_result(_diag(category="NO_ORDERS_EMITTED", closed=0)),
            run_strategy_code_fn=_raising_run,
        )

    failed = [b for b in report.likely_blockers if b.reason == "runtime_probe_failed"]
    assert failed
    assert "RuntimeError" in failed[0].evidence
    assert "runtime_events=failed" in report.summary
    # logger.exception records ERROR level by default but here we log at
    # the module's level (DEBUG via logger.exception → ERROR). Confirm a
    # traceback is captured.
    assert any(
        "runtime re-execution" in r.message and r.exc_info is not None for r in caplog.records
    ), "expected logger.exception to capture exc_info"


# ─────────────────────────────────────────────────────────────────────
# Summary line — "indicator=SKIPPED" honesty (Q4)
# ─────────────────────────────────────────────────────────────────────


def test_static_short_circuit_summary_says_indicator_skipped() -> None:
    # Q4: when the static probe short-circuits, the indicator probe is
    # not run. The summary must reflect that with "indicator=SKIPPED"
    # rather than lying with a default UNKNOWN_LOW_COVERAGE.
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
    report = run_coverage_stage(
        spec=_spec(code),
        market_data={"AAPL": _flat_df(50)},
        config=_config(),
        exec_result=_exec_result(_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=_never_called_run_strategy_code,
    )
    assert "indicator=SKIPPED" in report.summary
    assert "UNKNOWN" not in report.summary.split("indicator=")[1].split(";")[0]


# ─────────────────────────────────────────────────────────────────────
# Dedup respects hit_rate (B3)
# ─────────────────────────────────────────────────────────────────────


def test_dedup_keeps_blockers_with_distinct_hit_rates() -> None:
    # B3: two blockers with identical (reason, evidence) but distinct
    # hit_rates carry different information. The dedup key includes
    # hit_rate so neither is dropped.
    static = _report(
        CoverageCategory.UNKNOWN_LOW_COVERAGE,
        blockers=[LikelyBlocker(reason="r", evidence="e", hit_rate=0.0)],
    )
    indicator = _report(
        CoverageCategory.UNKNOWN_LOW_COVERAGE,
        blockers=[
            LikelyBlocker(reason="r", evidence="e", hit_rate=0.0),  # exact dup → drop
            LikelyBlocker(reason="r", evidence="e", hit_rate=0.25),  # distinct → keep
        ],
    )
    merged = merge_reports(static, indicator)
    rates = [b.hit_rate for b in merged.likely_blockers if b.reason == "r"]
    assert rates == [0.0, 0.25]


# ─────────────────────────────────────────────────────────────────────
# Numeric-aware sort on rule_ids (C5 / cosmetic)
# ─────────────────────────────────────────────────────────────────────


def test_runtime_blockers_sorted_numerically_by_rule_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from investment_team.models import RuleIndex

    monkeypatch.setattr(agg_mod, "run_indicator_probe", _unknown_indicator)

    # Force a known rule_index so the test exercises only the sort key,
    # not the instrumenter's labeling logic.
    rule_index = RuleIndex(rules={"r0": "p0", "r1": "p1", "r2": "p2", "r10": "p10"})
    monkeypatch.setattr(
        agg_mod,
        "instrument_strategy_code",
        lambda code: (code, rule_index),
    )

    # Mirror a real runtime emission: r0, r1, r2, r10 should sort
    # numerically rather than lexicographically (which would interleave
    # r10 between r1 and r2).
    def _fake_run(*args: Any, **kwargs: Any) -> StrategyRunResult:
        return StrategyRunResult(
            success=True,
            trades=[],
            probe_events={
                "events": [
                    {"rule_id": "r10", "hit_count": 10},
                    {"rule_id": "r2", "hit_count": 2},
                    {"rule_id": "r0", "hit_count": 0},
                    {"rule_id": "r1", "hit_count": 1},
                ],
                "truncated": False,
            },
        )

    report = run_coverage_stage(
        spec=_spec(_runtime_capable_code()),
        market_data={"AAPL": _flat_df(120)},
        config=_config(),
        exec_result=_exec_result(_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=_fake_run,
    )
    runtime_reasons = [b.reason for b in report.likely_blockers if b.reason.startswith("runtime:")]
    assert runtime_reasons == [
        "runtime: p0",
        "runtime: p1",
        "runtime: p2",
        "runtime: p10",
    ]


# ─────────────────────────────────────────────────────────────────────
# Production market_data shape — OHLCVBar lists must reach the
# indicator probe as DataFrames.
#
# The orchestrator hands ``Dict[str, List[OHLCVBar]]`` to the aggregator
# (production contract from ``run_backtest``). The indicator probe is
# written against ``Dict[str, pd.DataFrame]`` and filters on
# ``isinstance(df, pd.DataFrame)`` — without internal conversion the
# probe would silently see zero data in prod and always return
# UNKNOWN_LOW_COVERAGE.
# ─────────────────────────────────────────────────────────────────────


def _ohlcv_bar(date: str, close: float) -> "OHLCVBar":  # noqa: F821
    from investment_team.market_data_service import OHLCVBar

    return OHLCVBar(date=date, open=close, high=close + 1, low=close - 1, close=close, volume=1e6)


def _ohlcv_series(n: int = 120, close: float = 100.0) -> list:
    dates = pd.date_range("2024-01-01", periods=n, freq="D").strftime("%Y-%m-%d")
    return [_ohlcv_bar(d, close) for d in dates]


def test_market_data_ohlcv_bar_lists_reach_indicator_probe_as_dataframes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The aggregator must convert ``list[OHLCVBar]`` → ``pd.DataFrame``
    before invoking the indicator probe, otherwise the probe sees zero
    data in production and the whole stage becomes a no-op.
    """
    captured: List[Dict[str, Any]] = []

    def _spy_indicator(**kwargs: Any) -> CoverageReport:
        captured.append(kwargs)
        return _report(CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE, bars=120, symbols=1)

    monkeypatch.setattr(agg_mod, "run_indicator_probe", _spy_indicator)

    code = textwrap.dedent(
        """
        from contract import Strategy
        class S(Strategy):
            def on_bar(self, ctx, bar):
                if bar.close > 0:
                    ctx.submit_order(symbol=bar.symbol, side="long", qty=1)
        """
    )
    market_data = {"AAPL": _ohlcv_series(120)}

    report = run_coverage_stage(
        spec=_spec(code),
        market_data=market_data,
        config=_config(),
        exec_result=_exec_result(_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=_never_called_run_strategy_code,
    )

    # Indicator probe was called.
    assert len(captured) == 1
    # And it received a DataFrame, not the OHLCVBar list.
    converted = captured[0]["market_data"]
    assert isinstance(converted["AAPL"], pd.DataFrame)
    assert len(converted["AAPL"]) == 120
    assert {"open", "high", "low", "close", "volume"}.issubset(converted["AAPL"].columns)
    # Static probe's universe / bar-count helpers also saw the data,
    # so the merged report reflects it.
    assert report.coverage_category is CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE


def test_market_data_dataframe_inputs_still_work() -> None:
    """Existing tests + ad-hoc callers that hand in DataFrames directly
    must keep working. The conversion path is opportunistic, not
    coercive."""
    report = run_coverage_stage(
        spec=_spec(
            textwrap.dedent(
                """
                from contract import Strategy
                class S(Strategy):
                    def on_bar(self, ctx, bar):
                        if bar.close > 0: pass
                """
            )
        ),
        market_data={"AAPL": _flat_df(120)},
        config=_config(),
        exec_result=_exec_result(_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=_never_called_run_strategy_code,
    )
    # Report builds successfully — no errors from passing a DataFrame
    # through the universe / longest-bars / conversion helpers.
    assert report is not None


def test_runtime_stage_forwards_original_market_data_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runtime stage must forward the ORIGINAL market_data shape
    (OHLCVBar lists in prod), not the pandas-converted version — the
    harness expects what ``run_strategy_code`` always sees.
    """
    monkeypatch.setattr(agg_mod, "run_indicator_probe", _unknown_indicator)

    captured: List[Any] = []

    def _spy_run(
        strategy_code: str, market_data: Any, config: Any, **kwargs: Any
    ) -> StrategyRunResult:
        captured.append(market_data)
        return StrategyRunResult(
            success=True,
            trades=[],
            probe_events={"events": [], "truncated": False},
        )

    bars = _ohlcv_series(120)
    code = textwrap.dedent(
        """
        from contract import Strategy
        class S(Strategy):
            def on_bar(self, ctx, bar):
                if self.helper(bar):
                    ctx.submit_order(symbol=bar.symbol, side="long", qty=1)
        """
    )
    run_coverage_stage(
        spec=_spec(code),
        market_data={"AAPL": bars},
        config=_config(),
        exec_result=_exec_result(_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=_spy_run,
    )
    assert len(captured) == 1
    # Identity check: the runtime stage forwarded the OHLCVBar list
    # untouched, not a DataFrame conversion.
    assert captured[0]["AAPL"] is bars
