"""Orchestrator-wiring tests for the coverage-probe stage (#451).

Tests the orchestrator's ``_maybe_attach_coverage_report`` helper and
the source-level invariant that each ``run_strategy_code`` call site
passes a matching spec. Pure aggregator unit tests live in
``test_coverage_probe_aggregator.py``; pipeline integration in
``test_coverage_probe_stage.py``.
"""

from __future__ import annotations

import ast
import inspect
import json
from typing import Any

import pytest

from investment_team.models import (
    BacktestResult,
    CoverageCategory,
    CoverageReport,
    LikelyBlocker,
    SubconditionCoverage,
)
from investment_team.strategy_lab import orchestrator as orch_mod
from investment_team.strategy_lab.orchestrator import (
    _COVERAGE_LIKELY_BLOCKERS_CAP,
    _COVERAGE_SUBCONDITIONS_CAP,
    _format_coverage_report,
    _maybe_attach_coverage_report,
)

from ._coverage_probe_test_helpers import (
    make_config,
    make_diag,
    make_exec_result,
    make_flat_df,
    make_report,
    make_spec,
)

# ─────────────────────────────────────────────────────────────────────
# _maybe_attach_coverage_report — gate behaviour
# ─────────────────────────────────────────────────────────────────────


def _zeroed_metrics() -> BacktestResult:
    return BacktestResult(
        total_return_pct=0.0,
        annualized_return_pct=0.0,
        volatility_pct=0.0,
        sharpe_ratio=0.0,
        max_drawdown_pct=0.0,
        win_rate_pct=0.0,
        profit_factor=0.0,
    )


def test_maybe_attach_coverage_report_no_ops_when_gate_off() -> None:
    metrics = BacktestResult(
        total_return_pct=5.0,
        annualized_return_pct=5.0,
        volatility_pct=10.0,
        sharpe_ratio=1.0,
        max_drawdown_pct=2.0,
        win_rate_pct=55.0,
        profit_factor=1.5,
    )
    _maybe_attach_coverage_report(
        metrics=metrics,
        spec=make_spec("X = 1\n"),
        market_data={"AAPL": make_flat_df(50)},
        config=make_config(),
        exec_result=make_exec_result(make_diag(closed=10)),
    )
    assert metrics.coverage_report is None


def test_maybe_attach_coverage_report_runs_stage_when_gate_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []

    def _spy_run_coverage_stage(**kwargs: Any) -> CoverageReport:
        captured.append(kwargs)
        return make_report(CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE)

    monkeypatch.setattr(orch_mod, "run_coverage_stage", _spy_run_coverage_stage)

    metrics = _zeroed_metrics()
    spec = make_spec("Y = 2\n")
    _maybe_attach_coverage_report(
        metrics=metrics,
        spec=spec,
        market_data={"AAPL": make_flat_df(50)},
        config=make_config(),
        exec_result=make_exec_result(make_diag(category="NO_ORDERS_EMITTED", closed=0)),
    )
    assert metrics.coverage_report is not None
    assert (
        metrics.coverage_report.coverage_category
        is CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE
    )
    assert len(captured) == 1
    assert captured[0]["spec"] is spec


# ─────────────────────────────────────────────────────────────────────
# Source-level invariant — every call site pairs the right spec
# ─────────────────────────────────────────────────────────────────────


def test_orchestrator_call_sites_use_consistent_spec_and_exec_result() -> None:
    """Every ``_maybe_attach_coverage_report`` call site must pair an
    ``exec_result`` with the spec whose ``strategy_code`` matches the
    code that produced it. The alignment-site bug (PR #475 review)
    passed the loop-level ``spec`` alongside ``align_exec`` when
    ``proposed_spec`` is the source of truth. Lock the AST shape so a
    future refactor that re-introduces the drift fails loudly here.
    """
    source = inspect.getsource(orch_mod)
    tree = ast.parse(source)

    expected_spec_for_exec = {
        "exec_result": "spec",
        "align_exec": "proposed_spec",
        "repair_exec": "proposed_spec",
    }

    sites: list[dict[str, Any]] = []
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
# _format_coverage_report — refinement-prompt rendering (#452)
# ─────────────────────────────────────────────────────────────────────


def test_format_coverage_report_returns_empty_when_no_report() -> None:
    """No probe attached → no prompt bloat. Mirrors the
    ``_format_execution_diagnostics`` empty-on-None contract so callers can
    treat the line as additive.
    """
    assert _format_coverage_report(None) == ""


def test_format_coverage_report_emits_single_compact_json_line() -> None:
    """Rendered block is exactly one line, ``Coverage Report: {<json>}``,
    with stable-sorted keys and compact separators — matches the existing
    ``Execution Diagnostics:`` line shape so the refinement prompt's
    parser-friendliness is unchanged.
    """
    report = CoverageReport(
        coverage_category=CoverageCategory.ENTRY_CONDITION_NEVER_TRUE,
        summary="RSI<25 never satisfied",
        bars_checked=250,
        symbols_checked=1,
        warmup_bars_required=14,
        entry_orders_emitted=0,
    )
    line = _format_coverage_report(report)
    assert "\n" not in line
    assert line.startswith("Coverage Report: {")
    payload = json.loads(line[len("Coverage Report: ") :])
    assert payload["coverage_category"] == "ENTRY_CONDITION_NEVER_TRUE"
    assert payload["bars_checked"] == 250
    assert payload["warmup_bars_required"] == 14
    # Stable key ordering for diff-friendliness — sort_keys=True must hold.
    keys = list(payload.keys())
    assert keys == sorted(keys)
    # Compact separators (no whitespace after `:` or `,`).
    assert ", " not in line
    assert ": " not in line[len("Coverage Report: ") :]


def test_format_coverage_report_caps_likely_blockers_and_subconditions() -> None:
    """A pathological probe with many blockers/subconditions must not blow
    up ``failure_details``. The rendered JSON truncates to the module-level
    caps so the LLM context stays bounded.
    """
    blockers = [
        LikelyBlocker(reason=f"blocker_{i}", evidence=f"evidence {i}")
        for i in range(_COVERAGE_LIKELY_BLOCKERS_CAP + 4)
    ]
    subconditions = [
        SubconditionCoverage(label=f"sub_{i}", hit_count=0, hit_rate=0.0)
        for i in range(_COVERAGE_SUBCONDITIONS_CAP + 5)
    ]
    report = CoverageReport(
        coverage_category=CoverageCategory.CONJUNCTION_NEVER_TRUE,
        summary="too many conjuncts",
        likely_blockers=blockers,
        subconditions=subconditions,
    )
    line = _format_coverage_report(report)
    payload = json.loads(line[len("Coverage Report: ") :])
    assert len(payload["likely_blockers"]) == _COVERAGE_LIKELY_BLOCKERS_CAP
    assert len(payload["subconditions"]) == _COVERAGE_SUBCONDITIONS_CAP
    # First-N retained (head-trim, not tail-trim) so the prompt sees the
    # earliest-detected blockers — these are the most likely to be the
    # root cause from a deterministic probe.
    assert payload["likely_blockers"][0]["reason"] == "blocker_0"
    assert payload["subconditions"][0]["label"] == "sub_0"
