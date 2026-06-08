"""End-to-end tests for ``run_coverage_stage`` (#451).

These tests drive the full pipeline (static → indicator → optional
runtime) by injecting a fake ``run_strategy_code`` and (where useful)
stubbing the indicator probe. Aggregator-level unit tests live in
``test_coverage_probe_aggregator.py``; orchestrator wiring lives in
``test_coverage_probe_orchestrator_stage.py``.
"""

from __future__ import annotations

import logging
import sys
import textwrap
from typing import Any

import pandas as pd
import pytest

from investment_team.models import CoverageCategory, CoverageReport, RuleIndex
from investment_team.strategy_lab.coverage_probe import aggregator as agg_mod
from investment_team.strategy_lab.coverage_probe import run_coverage_stage
from investment_team.trading_service.modes.sandbox_compat import StrategyRunResult

from ._coverage_probe_test_helpers import (
    make_config,
    make_diag,
    make_exec_result,
    make_flat_df,
    make_ohlcv_series,
    make_report,
    make_spec,
    never_called_run_strategy_code,
    runtime_capable_code,
    unknown_indicator,
)

# ─────────────────────────────────────────────────────────────────────
# Static probe short-circuit
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
    indicator_called: list[bool] = []

    def _spy_indicator(*args: Any, **kwargs: Any) -> CoverageReport:
        indicator_called.append(True)
        return make_report(CoverageCategory.COVERAGE_OK)

    monkeypatch.setattr(agg_mod, "run_indicator_probe", _spy_indicator)

    report = run_coverage_stage(
        spec=make_spec(code),
        market_data={"AAPL": make_flat_df(50)},
        config=make_config(),
        exec_result=make_exec_result(make_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=never_called_run_strategy_code,
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
    indicator_called: list[bool] = []

    def _spy_indicator(*args: Any, **kwargs: Any) -> CoverageReport:
        indicator_called.append(True)
        return make_report(CoverageCategory.COVERAGE_OK)

    monkeypatch.setattr(agg_mod, "run_indicator_probe", _spy_indicator)

    report = run_coverage_stage(
        spec=make_spec(code),
        market_data={"AAPL": make_flat_df(120)},
        config=make_config(),
        exec_result=make_exec_result(make_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=never_called_run_strategy_code,
    )

    assert report.coverage_category is CoverageCategory.TARGET_SYMBOL_MISSING
    assert indicator_called == []


def test_static_short_circuit_summary_says_indicator_skipped() -> None:
    """Q4: when the static probe short-circuits, the indicator probe is
    not run. The summary must reflect that with "indicator=SKIPPED"
    rather than lying with a default UNKNOWN_LOW_COVERAGE."""
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
        spec=make_spec(code),
        market_data={"AAPL": make_flat_df(50)},
        config=make_config(),
        exec_result=make_exec_result(make_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=never_called_run_strategy_code,
    )
    assert "indicator=SKIPPED" in report.summary
    assert "UNKNOWN" not in report.summary.split("indicator=")[1].split(";")[0]


# ─────────────────────────────────────────────────────────────────────
# Runtime re-execution gating
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

    def _stub_indicator(*args: Any, **kwargs: Any) -> CoverageReport:
        return make_report(CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE)

    monkeypatch.setattr(agg_mod, "run_indicator_probe", _stub_indicator)

    report = run_coverage_stage(
        spec=make_spec(code),
        market_data={"AAPL": make_flat_df(120)},
        config=make_config(),
        exec_result=make_exec_result(make_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=never_called_run_strategy_code,
    )
    assert report.coverage_category is CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE


def test_runtime_reexecution_runs_when_static_and_indicator_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agg_mod, "run_indicator_probe", unknown_indicator)
    runtime_calls: list[dict[str, Any]] = []

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
        spec=make_spec(runtime_capable_code()),
        market_data={"AAPL": make_flat_df(120)},
        config=make_config(),
        exec_result=make_exec_result(make_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=_fake_run_strategy_code,
    )

    assert len(runtime_calls) == 1
    assert runtime_calls[0]["kwargs"].get("coverage_probe_mode") is True
    runtime_blockers = [b for b in report.likely_blockers if b.reason.startswith("runtime:")]
    assert runtime_blockers
    assert "runtime_events=1" in report.summary


# ─────────────────────────────────────────────────────────────────────
# Runtime probe outcomes (B1, B2, failed, skipped)
# ─────────────────────────────────────────────────────────────────────


def test_runtime_reexecution_failure_records_failure_blocker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agg_mod, "run_indicator_probe", unknown_indicator)

    def _failing_run(*args: Any, **kwargs: Any) -> StrategyRunResult:
        return StrategyRunResult(
            success=False,
            trades=[],
            error_type="runtime_error",
            stderr="boom",
        )

    report = run_coverage_stage(
        spec=make_spec(runtime_capable_code()),
        market_data={"AAPL": make_flat_df(120)},
        config=make_config(),
        exec_result=make_exec_result(make_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=_failing_run,
    )

    failure_blockers = [b for b in report.likely_blockers if b.reason == "runtime_probe_failed"]
    assert failure_blockers
    assert "runtime_error" in failure_blockers[0].evidence
    assert "runtime_events=failed" in report.summary
    assert report.coverage_category is CoverageCategory.UNKNOWN_LOW_COVERAGE


def test_runtime_reexecution_skipped_when_no_instrumentable_predicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No on_bar at all → instrument_strategy_code returns an empty
    # RuleIndex. The stage must record that as a skip-blocker rather
    # than spinning up the harness.
    monkeypatch.setattr(agg_mod, "run_indicator_probe", unknown_indicator)

    report = run_coverage_stage(
        spec=make_spec("X = 1\n"),
        market_data={"AAPL": make_flat_df(120)},
        config=make_config(),
        exec_result=make_exec_result(make_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=never_called_run_strategy_code,
    )

    skipped = [b for b in report.likely_blockers if b.reason == "runtime_probe_skipped"]
    assert skipped


def test_runtime_zero_events_records_no_hits_blocker_and_zero_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # B1: a clean run with an empty events list is a strong "predicates
    # never fired" signal — must be surfaced as a distinct blocker, and
    # the summary must say "runtime_events=0" (not "n/a").
    monkeypatch.setattr(agg_mod, "run_indicator_probe", unknown_indicator)

    def _fake_run(*args: Any, **kwargs: Any) -> StrategyRunResult:
        return StrategyRunResult(
            success=True,
            trades=[],
            probe_events={"events": [], "truncated": False},
        )

    report = run_coverage_stage(
        spec=make_spec(runtime_capable_code()),
        market_data={"AAPL": make_flat_df(120)},
        config=make_config(),
        exec_result=make_exec_result(make_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=_fake_run,
    )

    no_hits = [b for b in report.likely_blockers if b.reason == "runtime_probe_no_hits"]
    assert no_hits
    assert "hits=0" in no_hits[0].evidence
    assert "runtime_events=0" in report.summary


def test_runtime_success_with_no_probe_events_field_records_no_frame_blocker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # B2: success=True but probe_events=None must be distinguishable
    # from a hard failure. Surface as a separate blocker so #452 can
    # decide whether to retry the run.
    monkeypatch.setattr(agg_mod, "run_indicator_probe", unknown_indicator)

    def _no_frame_run(*args: Any, **kwargs: Any) -> StrategyRunResult:
        return StrategyRunResult(success=True, trades=[], probe_events=None)

    report = run_coverage_stage(
        spec=make_spec(runtime_capable_code()),
        market_data={"AAPL": make_flat_df(120)},
        config=make_config(),
        exec_result=make_exec_result(make_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=_no_frame_run,
    )

    no_frame = [b for b in report.likely_blockers if b.reason == "runtime_probe_no_frame"]
    assert no_frame
    assert "runtime_events=no_frame" in report.summary
    assert not [b for b in report.likely_blockers if b.reason == "runtime_probe_failed"], (
        "no_frame must not be conflated with failure"
    )


def test_runtime_skipped_summary_token_for_empty_strategy_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agg_mod, "run_indicator_probe", unknown_indicator)

    report = run_coverage_stage(
        spec=make_spec(""),
        market_data={"AAPL": make_flat_df(120)},
        config=make_config(),
        exec_result=make_exec_result(make_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=never_called_run_strategy_code,
    )
    skipped = [b for b in report.likely_blockers if b.reason == "runtime_probe_skipped"]
    assert skipped
    assert "spec.strategy_code is empty" in skipped[0].evidence
    assert "runtime_events=skipped" in report.summary


def test_runtime_uncaught_exception_is_logged_and_recorded(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # N4: the runtime exception path uses logger.warning — visible in
    # prod (a spike in sandbox crashes is observable) but not
    # ERROR-severity. exc_info=True preserves the traceback for triage.
    monkeypatch.setattr(agg_mod, "run_indicator_probe", unknown_indicator)

    def _raising_run(*args: Any, **kwargs: Any) -> StrategyRunResult:
        raise RuntimeError("sandbox blew up")

    with caplog.at_level(
        logging.WARNING, logger="investment_team.strategy_lab.coverage_probe.aggregator"
    ):
        report = run_coverage_stage(
            spec=make_spec(runtime_capable_code()),
            market_data={"AAPL": make_flat_df(120)},
            config=make_config(),
            exec_result=make_exec_result(make_diag(category="NO_ORDERS_EMITTED", closed=0)),
            run_strategy_code_fn=_raising_run,
        )

    failed = [b for b in report.likely_blockers if b.reason == "runtime_probe_failed"]
    assert failed
    assert "RuntimeError" in failed[0].evidence
    assert "runtime_events=failed" in report.summary
    warning_records = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "runtime re-execution" in r.message
    ]
    assert warning_records, "expected a WARNING-level log record"
    assert warning_records[0].exc_info is not None, "expected exc_info on the warning record"


def test_runtime_failure_evidence_not_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    """A long runtime-failure message reaches the blocker evidence whole —
    the error string is no longer cut off at 160 chars before it lands in
    the refinement / zero-trade-repair prompt."""
    monkeypatch.setattr(agg_mod, "run_indicator_probe", unknown_indicator)
    long_detail = "boom-" * 100  # 500 chars, well past the old 160 cap

    def _raising_run(*args: Any, **kwargs: Any) -> StrategyRunResult:
        raise RuntimeError(long_detail)

    report = run_coverage_stage(
        spec=make_spec(runtime_capable_code()),
        market_data={"AAPL": make_flat_df(120)},
        config=make_config(),
        exec_result=make_exec_result(make_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=_raising_run,
    )

    failed = [b for b in report.likely_blockers if b.reason == "runtime_probe_failed"]
    assert failed
    assert long_detail in failed[0].evidence


# ─────────────────────────────────────────────────────────────────────
# Numeric-aware rule_id sort, empty-rule_id filter (C5, R1)
# ─────────────────────────────────────────────────────────────────────


def test_runtime_blockers_sorted_numerically_by_rule_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agg_mod, "run_indicator_probe", unknown_indicator)
    rule_index = RuleIndex(rules={"r0": "p0", "r1": "p1", "r2": "p2", "r10": "p10"})
    monkeypatch.setattr(
        agg_mod,
        "instrument_strategy_code",
        lambda code: (code, rule_index),
    )

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
        spec=make_spec(runtime_capable_code()),
        market_data={"AAPL": make_flat_df(120)},
        config=make_config(),
        exec_result=make_exec_result(make_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=_fake_run,
    )
    runtime_reasons = [b.reason for b in report.likely_blockers if b.reason.startswith("runtime:")]
    assert runtime_reasons == [
        "runtime: p0",
        "runtime: p1",
        "runtime: p2",
        "runtime: p10",
    ]


def test_runtime_events_with_empty_rule_id_are_filtered_and_count_stays_truthful(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1: the harness contract is one event per rule_id; if it ever
    emits events with empty / None rule_ids those get filtered out by
    the renderer, and the summary's ``runtime_events=N`` reflects the
    produced blocker count, not the raw event count.
    """
    monkeypatch.setattr(agg_mod, "run_indicator_probe", unknown_indicator)
    rule_index = RuleIndex(rules={"r0": "p0", "r1": "p1"})
    monkeypatch.setattr(
        agg_mod,
        "instrument_strategy_code",
        lambda code: (code, rule_index),
    )

    def _fake_run(*args: Any, **kwargs: Any) -> StrategyRunResult:
        return StrategyRunResult(
            success=True,
            trades=[],
            probe_events={
                "events": [
                    {"rule_id": "r0", "hit_count": 5},
                    {"rule_id": "", "hit_count": 3},  # filtered: empty id
                    {"rule_id": "r1", "hit_count": 2},
                    {"rule_id": None, "hit_count": 1},  # filtered: missing id
                ],
                "truncated": False,
            },
        )

    report = run_coverage_stage(
        spec=make_spec(runtime_capable_code()),
        market_data={"AAPL": make_flat_df(120)},
        config=make_config(),
        exec_result=make_exec_result(make_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=_fake_run,
    )
    runtime_blockers = [b for b in report.likely_blockers if b.reason.startswith("runtime:")]
    assert len(runtime_blockers) == 2
    assert "runtime_events=2" in report.summary


# ─────────────────────────────────────────────────────────────────────
# Production market_data shape (OHLCVBar) flows through correctly
# ─────────────────────────────────────────────────────────────────────


def test_market_data_ohlcv_bar_lists_reach_indicator_probe_as_dataframes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The aggregator converts ``list[OHLCVBar]`` → ``pd.DataFrame``
    before invoking the indicator probe, otherwise the probe sees zero
    data in production and the whole stage becomes a no-op.
    """
    captured: list[dict[str, Any]] = []

    def _spy_indicator(**kwargs: Any) -> CoverageReport:
        captured.append(kwargs)
        return make_report(CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE, bars=120, symbols=1)

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
    report = run_coverage_stage(
        spec=make_spec(code),
        market_data={"AAPL": make_ohlcv_series(120)},
        config=make_config(),
        exec_result=make_exec_result(make_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=never_called_run_strategy_code,
    )

    assert len(captured) == 1
    converted = captured[0]["market_data"]
    assert isinstance(converted["AAPL"], pd.DataFrame)
    assert len(converted["AAPL"]) == 120
    assert {"open", "high", "low", "close", "volume"}.issubset(converted["AAPL"].columns)
    assert report.coverage_category is CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE


def test_market_data_dataframe_inputs_pass_through_without_reconstruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DataFrame inputs reach the indicator probe as the SAME object the
    caller passed — pre-built DataFrames must not be cloned or rebuilt.
    """
    captured: list[dict[str, Any]] = []

    def _spy_indicator(**kwargs: Any) -> CoverageReport:
        captured.append(kwargs)
        return make_report(CoverageCategory.COVERAGE_OK)

    monkeypatch.setattr(agg_mod, "run_indicator_probe", _spy_indicator)

    df = make_flat_df(120)
    run_coverage_stage(
        spec=make_spec(
            textwrap.dedent(
                """
                from contract import Strategy
                class S(Strategy):
                    def on_bar(self, ctx, bar):
                        if bar.close > 0: pass
                """
            )
        ),
        market_data={"AAPL": df},
        config=make_config(),
        exec_result=make_exec_result(make_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=never_called_run_strategy_code,
    )
    assert len(captured) == 1
    assert captured[0]["market_data"]["AAPL"] is df


def test_runtime_stage_forwards_original_market_data_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runtime stage forwards the ORIGINAL market_data shape
    (OHLCVBar lists in prod), not the pandas-converted version — the
    harness expects what ``run_strategy_code`` always sees.
    """
    monkeypatch.setattr(agg_mod, "run_indicator_probe", unknown_indicator)
    captured: list[Any] = []

    def _spy_run(
        strategy_code: str, market_data: Any, config: Any, **kwargs: Any
    ) -> StrategyRunResult:
        captured.append(market_data)
        return StrategyRunResult(
            success=True,
            trades=[],
            probe_events={"events": [], "truncated": False},
        )

    bars = make_ohlcv_series(120)
    run_coverage_stage(
        spec=make_spec(runtime_capable_code()),
        market_data={"AAPL": bars},
        config=make_config(),
        exec_result=make_exec_result(make_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=_spy_run,
    )
    assert len(captured) == 1
    # Identity check: original list, not a DataFrame conversion.
    assert captured[0]["AAPL"] is bars


# ─────────────────────────────────────────────────────────────────────
# Determinism + LLM-free
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
    pre = {n for n in sys.modules if n.startswith(("llm_service", "anthropic", "openai"))}
    run_coverage_stage(
        spec=make_spec(code),
        market_data={"AAPL": make_flat_df(120)},
        config=make_config(),
        exec_result=make_exec_result(make_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=never_called_run_strategy_code,
    )
    post = {n for n in sys.modules if n.startswith(("llm_service", "anthropic", "openai"))}
    assert post == pre


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
    market_data = {"AAPL": make_flat_df(120)}
    args = dict(
        spec=make_spec(code),
        market_data=market_data,
        config=make_config(),
        exec_result=make_exec_result(make_diag(category="NO_ORDERS_EMITTED", closed=0)),
        run_strategy_code_fn=never_called_run_strategy_code,
    )
    assert run_coverage_stage(**args).model_dump() == run_coverage_stage(**args).model_dump()
