"""Perf-guard for the #451 coverage-probe stage on the success path (#453, F).

Acceptance criterion (F) of #453: on a successful backtest the orchestrator's
coverage-probe stage MUST short-circuit (``BacktestResult.coverage_report =
None``) and add at most ``1.1×`` to the total runtime vs. the unprobed path.

Structural correctness on the success path is already exercised by
``test_coverage_probe_orchestrator_stage.py`` (``no_ops_when_gate_off``); this
module pins the *quantitative* runtime bound called out in the issue.
"""

from __future__ import annotations

import time
from statistics import median
from typing import Any

import pytest

from investment_team.models import BacktestResult
from investment_team.strategy_lab import orchestrator as orch_mod
from investment_team.strategy_lab.orchestrator import _maybe_attach_coverage_report

from ._coverage_probe_test_helpers import (
    make_config,
    make_diag,
    make_exec_result,
    make_flat_df,
    make_spec,
)

_ITERATIONS = 200
_RUNTIME_RATIO_BOUND = 1.1
_WORKLOAD_OPS = 2000


def _fresh_metrics() -> BacktestResult:
    return BacktestResult(
        total_return_pct=8.0,
        annualized_return_pct=12.0,
        volatility_pct=15.0,
        sharpe_ratio=1.1,
        max_drawdown_pct=4.0,
        win_rate_pct=55.0,
        profit_factor=1.6,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )


def _success_kwargs() -> dict[str, Any]:
    return dict(
        spec=make_spec("X = 1\n"),
        market_data={"AAPL": make_flat_df(50)},
        config=make_config(),
        exec_result=make_exec_result(make_diag(closed=10, orders_accepted=10)),
    )


def _synthetic_backtest_workload() -> float:
    total = 0.0
    for i in range(_WORKLOAD_OPS):
        total += (i * 0.5) - 0.25
    return total


def test_success_path_does_not_invoke_probe_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run with ``closed_trades >= LOW_TRADE_THRESHOLD`` and no
    ``zero_trade_category`` must never reach ``run_coverage_stage`` —
    the gate in ``should_run_probes`` short-circuits and
    ``BacktestResult.coverage_report`` stays ``None``.

    Re-locks #453 (G) at the success-path boundary: the probe stage is
    the only LLM-touching code path here, so "stage never runs" implies
    "no LLM call on a successful backtest".
    """

    def _must_not_run(**_kwargs: Any) -> None:
        raise AssertionError("run_coverage_stage must not run on a successful backtest")

    monkeypatch.setattr(orch_mod, "run_coverage_stage", _must_not_run)

    metrics = _fresh_metrics()
    _maybe_attach_coverage_report(metrics=metrics, **_success_kwargs())

    assert metrics.coverage_report is None


def test_success_path_runtime_within_ten_percent_of_unprobed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Median runtime of (workload + helper-gated-off) must stay within
    ``1.1×`` of (workload alone).

    The helper itself is O(1) on success (one ``should_run_probes`` call
    plus a Python-level branch), so this is effectively a regression
    guard: a future refactor that started doing real work on the success
    path would have to either fail this bound or stash that work behind
    a different name.

    Samples are interleaved so a transient CPU spike biases both
    populations equally — straight back-to-back loops let scheduler
    hiccups cluster on one side and flake CI.
    """

    def _must_not_run(**_kwargs: Any) -> None:
        raise AssertionError("run_coverage_stage must not run on a successful backtest")

    monkeypatch.setattr(orch_mod, "run_coverage_stage", _must_not_run)

    kwargs = _success_kwargs()
    metrics = _fresh_metrics()

    baseline_samples: list[float] = []
    probed_samples: list[float] = []
    for _ in range(_ITERATIONS):
        t0 = time.perf_counter()
        _synthetic_backtest_workload()
        baseline_samples.append(time.perf_counter() - t0)

        t0 = time.perf_counter()
        _synthetic_backtest_workload()
        _maybe_attach_coverage_report(metrics=metrics, **kwargs)
        probed_samples.append(time.perf_counter() - t0)

    baseline_med = median(baseline_samples)
    probed_med = median(probed_samples)
    # Floor the denominator at 1µs to defang the unlikely zero-baseline
    # corner case; real samples on any CI machine sit in the 50–500µs
    # band given _WORKLOAD_OPS=2000.
    baseline_floor = max(baseline_med, 1e-6)

    assert probed_med <= _RUNTIME_RATIO_BOUND * baseline_floor, (
        "coverage-probe gate added > "
        f"{(_RUNTIME_RATIO_BOUND - 1) * 100:.0f}% on successful backtest: "
        f"baseline_med={baseline_med * 1e6:.2f}µs, "
        f"probed_med={probed_med * 1e6:.2f}µs"
    )
    assert metrics.coverage_report is None
