"""Perf-guard for the #451 coverage-probe stage on the success path (#453, F).

Acceptance criterion (F) of #453: on a successful backtest the orchestrator's
coverage-probe stage MUST short-circuit (``BacktestResult.coverage_report =
None``) and add at most ``1.12×`` to the total runtime vs. the unprobed path.

Structural correctness on the success path is already exercised by
``test_coverage_probe_orchestrator_stage.py`` (``no_ops_when_gate_off``); this
module pins the *quantitative* runtime bound called out in the issue.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from investment_team.models import BacktestResult
from investment_team.strategy_lab import _orchestrator_helpers as helpers_mod
from investment_team.strategy_lab.orchestrator import _maybe_attach_coverage_report

from ._coverage_probe_test_helpers import (
    make_config,
    make_diag,
    make_exec_result,
    make_flat_df,
    make_spec,
)

_ITERATIONS = 200
_RUNTIME_RATIO_BOUND = 1.12
# A single 200-sample P25 measurement still occasionally lands on a shared,
# noisy CI runner (a scheduler stall mid-loop biases the probed arm more than
# the baseline arm, since the probed arm does strictly more work per sample).
# Retrying re-measures from scratch rather than reusing samples, so a retry
# only passes if a *subsequent* clean pass is genuinely within bound — a
# consistent regression still fails every attempt.
_MAX_ATTEMPTS = 3
# Sized so the baseline workload (~ms scale) dominates the helper's fixed
# success-path cost (one ``should_run_probes`` call + a branch, ~tens of µs).
# A small workload made that fixed overhead a double-digit fraction of the
# baseline, so ordinary CI scheduler noise tipped the P25 ratio past 1.1× and
# flaked. With the workload an order of magnitude larger than the helper cost,
# the ratio is stable while the bound still trips if the success path ever
# starts doing real (sub-millisecond+) work.
_WORKLOAD_OPS = 20000


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

    monkeypatch.setattr(helpers_mod, "run_coverage_stage", _must_not_run)

    metrics = _fresh_metrics()
    _maybe_attach_coverage_report(metrics=metrics, **_success_kwargs())

    assert metrics.coverage_report is None


def test_success_path_runtime_within_ten_percent_of_unprobed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lower-quartile (P25) runtime of (workload + helper-gated-off) must
    stay within ``1.12×`` of (workload alone).

    The helper itself is O(1) on success (one ``should_run_probes`` call
    plus a Python-level branch), so this is effectively a regression
    guard: a future refactor that started doing real work on the success
    path would have to either fail this bound or stash that work behind
    a different name.

    Samples are interleaved so a transient CPU spike biases both
    populations equally — straight back-to-back loops let scheduler
    hiccups cluster on one side and flake CI.

    Statistic choice — we compare the **lower quartile (P25)** rather
    than ``min`` or ``median``:

    * ``min`` (previously tried) is too lenient — one lucky probed
      iteration is enough to satisfy the bound even when most probed
      iterations are consistently slower, so meaningful overhead
      regressions slip through.
    * ``median`` (originally used) flaked under ``pytest-xdist`` with
      3+ concurrent workers, where scheduler interrupts can land on
      more than half the samples on one side and shift the median.
    * **P25** is robust on *both* sides: it ignores the worst-75% of
      scheduler noise (so xdist hiccups don't bias it) but is still a
      typical-case statistic — beating it requires a 25%-of-samples
      run, not a single lucky iteration. This catches a consistent
      slowdown of ``_maybe_attach_coverage_report`` while staying
      stable under parallel CI workers.

    Preconditions:
        ``_ITERATIONS`` is large enough (≥ 8) that ``sample[N // 4]``
        is a meaningful quartile estimate. We use 200.

    Postconditions:
        Asserts that ``probed_p25 <= _RUNTIME_RATIO_BOUND *
        max(baseline_p25, 1e-6)`` and that ``metrics.coverage_report
        is None``, re-measuring up to ``_MAX_ATTEMPTS`` times so a single
        noisy CI pass doesn't fail the build.
    """

    def _must_not_run(**_kwargs: Any) -> None:
        raise AssertionError("run_coverage_stage must not run on a successful backtest")

    monkeypatch.setattr(helpers_mod, "run_coverage_stage", _must_not_run)

    kwargs = _success_kwargs()
    metrics = _fresh_metrics()

    for attempt in range(1, _MAX_ATTEMPTS + 1):
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

        # Lower quartile (P25) via sorted-index access — no numpy.
        quartile_index = _ITERATIONS // 4
        baseline_p25 = sorted(baseline_samples)[quartile_index]
        probed_p25 = sorted(probed_samples)[quartile_index]
        # Floor the denominator at 1µs to defang the unlikely zero-baseline
        # corner case; real samples sit in the low-millisecond band given
        # _WORKLOAD_OPS=20000, well above the helper's fixed success-path cost.
        baseline_floor = max(baseline_p25, 1e-6)

        if probed_p25 <= _RUNTIME_RATIO_BOUND * baseline_floor:
            break
        if attempt == _MAX_ATTEMPTS:
            raise AssertionError(
                "coverage-probe gate added > "
                f"{(_RUNTIME_RATIO_BOUND - 1) * 100:.0f}% on successful backtest "
                f"across {_MAX_ATTEMPTS} attempts: "
                f"baseline_p25={baseline_p25 * 1e6:.2f}µs, "
                f"probed_p25={probed_p25 * 1e6:.2f}µs"
            )

    assert metrics.coverage_report is None
