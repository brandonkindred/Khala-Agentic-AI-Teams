"""Unit tests for ``strategy_lab.temporal.workflows.StrategyLabBatchWorkflow``.

``StrategyLabBatchWorkflow`` ports ``_strategy_lab_worker``'s batch/wave loop: per
batch it refreshes the signal brief, then per wave it starts one
``StrategyLabCycleWorkflow`` **child workflow** per cycle (concurrently), finalizes
each settled record, merges the wave into the batch-level convergence tracker in
cycle-index order, and checks cancellation between waves.

These tests drive ``run()`` with ``asyncio.run`` and mock the two workflow
primitives — ``workflow.start_child_workflow`` and ``workflow.execute_activity`` —
so no live Temporal server is needed. They assert the workflow's own control flow:
per-wave concurrency (all children started before any awaited), sorted-cycle-index
tracker merge, finalize-then-persist, cancellation short-circuit, and errored-cycle
accounting.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List
from unittest import mock

import pytest

from investment_team.strategy_lab.temporal import workflows as wf


def _config_dict() -> Dict[str, Any]:
    return {"start_date": "2023-01-01", "end_date": "2023-12-31"}


_WF_CONFIG = {
    "design_review_rounds": 20,
    "design_review_stall_rounds": 3,
    "mechanical_repair_enabled": True,
    "code_conformance_retries": 2,
    "design_max_llm_calls": 120,
    "regime_summary_enabled": False,
    "max_design_reentries": 2,
}


def _batch_input(**overrides: Any) -> Dict[str, Any]:
    base = {
        "run_id": "run-1",
        "config": _config_dict(),
        "batch_size": 2,
        "batch_count": 1,
        "max_parallel": 2,
        "benchmark_symbol": "SPY",
        "exclude_asset_classes": None,
        "paper_trading_enabled": True,
        "paper_trading_lookback_days": 365,
        "workflow_config": _WF_CONFIG,
        "convergence_tracker_state": {},
    }
    base.update(overrides)
    return base


class _Harness:
    """Records activity + child-workflow calls and serves canned results.

    ``child_results`` maps a child workflow id → the dict the child returns, or an
    ``Exception`` to simulate a failed cycle. ``activity_handlers`` maps activity
    function name → ``(args) -> result``.
    """

    def __init__(
        self,
        child_results: Dict[str, Any],
        activity_handlers: Dict[str, Any],
    ) -> None:
        self.child_results = child_results
        self.activity_handlers = activity_handlers
        self.child_starts: List[str] = []
        self.child_start_args: Dict[str, Any] = {}
        # For each awaited child, how many children had been *started* by then —
        # lets a test prove all children in a wave start before any is awaited.
        self.start_count_at_await: List[int] = []
        self.activity_calls: List[str] = []

    async def start_child_workflow(self, _wf_run, arg, *, id, **_kw):  # noqa: A002
        self.child_starts.append(id)
        self.child_start_args[id] = arg
        result = self.child_results[id]

        async def _handle():
            self.start_count_at_await.append(len(self.child_starts))
            if isinstance(result, BaseException):
                raise result
            return result

        return _handle()

    async def execute_activity(self, fn, *, args, **_kw):
        name = fn.__name__
        self.activity_calls.append(name)
        handler = self.activity_handlers.get(name)
        if handler is None:
            raise AssertionError(f"unexpected activity call: {name}")
        return handler(args)

    def patch(self):
        return (
            mock.patch("temporalio.workflow.start_child_workflow", self.start_child_workflow),
            mock.patch("temporalio.workflow.execute_activity", self.execute_activity),
        )


def _default_activity_handlers(**overrides: Any) -> Dict[str, Any]:
    handlers = {
        "compute_signal_brief_activity": lambda a: {
            "signal_brief": {"brief_version": "v1"},
            "signal_brief_storage": {"stored": True},
        },
        "snapshot_prior_records_activity": lambda a: [],
        "persist_run_state_activity": lambda a: None,
        "finalize_cycle_record_activity": lambda a: {
            "record": {"lab_record_id": f"rec-{a[0]['record']['lab_record_id']}"}
        },
        "merge_wave_results_activity": lambda a: {
            "primary_tracker_state": {"merged": [w["cycle_index"] for w in a[0]["wave_results"]]}
        },
        "is_run_cancelled_activity": lambda a: False,
        "external_terminal_status_activity": lambda a: None,
        "resolve_workflow_config_activity": lambda a: _WF_CONFIG,
    }
    handlers.update(overrides)
    return handlers


def _child_record(lab_record_id: str) -> Dict[str, Any]:
    return {
        "kind": "record",
        "record": {"lab_record_id": lab_record_id},
        "convergence_tracker_state": {"trial_count": 1, "src": lab_record_id},
    }


def _child_skipped() -> Dict[str, Any]:
    return {"kind": "skipped", "convergence_tracker_state": {"trial_count": 1}}


def _run(batch_input: Dict[str, Any], harness: _Harness) -> Dict[str, Any]:
    p1, p2 = harness.patch()
    with p1, p2:
        return asyncio.run(wf.StrategyLabBatchWorkflow().run(batch_input))


# ---------------------------------------------------------------------------
# Happy path — one batch, one wave of 2 cycles
# ---------------------------------------------------------------------------


def test_batch_runs_one_wave_and_completes():
    harness = _Harness(
        child_results={"run-1-c0": _child_record("0"), "run-1-c1": _child_record("1")},
        activity_handlers=_default_activity_handlers(),
    )
    result = _run(_batch_input(), harness)

    assert result["status"] == "completed"
    assert result["errored_cycles"] == 0
    # Both cycles finalized → their finalized record ids collected.
    assert sorted(result["completed_record_ids"]) == ["rec-0", "rec-1"]
    # Two child workflows started, ids derived from run_id + cycle index.
    assert harness.child_starts == ["run-1-c0", "run-1-c1"]
    # Signal brief refreshed once for the single batch.
    assert harness.activity_calls.count("compute_signal_brief_activity") == 1
    # Prior records snapshotted once for the single wave.
    assert harness.activity_calls.count("snapshot_prior_records_activity") == 1


def test_all_children_in_wave_start_before_any_is_awaited():
    """Per-wave concurrency: the whole wave is started before gather awaits any
    child (reproducing ThreadPoolExecutor(max_workers=len(wave))'s fan-out)."""
    harness = _Harness(
        child_results={"run-1-c0": _child_record("0"), "run-1-c1": _child_record("1")},
        activity_handlers=_default_activity_handlers(),
    )
    _run(_batch_input(), harness)
    # Every child was awaited only after all 2 children in the wave had started.
    assert harness.start_count_at_await == [2, 2]


def test_config_resolved_via_activity_when_absent():
    harness = _Harness(
        child_results={"run-1-c0": _child_record("0"), "run-1-c1": _child_record("1")},
        activity_handlers=_default_activity_handlers(),
    )
    bi = _batch_input()
    del bi["workflow_config"]
    _run(bi, harness)
    assert harness.activity_calls.count("resolve_workflow_config_activity") == 1


def test_each_cycle_gets_its_own_tracker_snapshot_and_shared_prior_records():
    prior = [{"lab_record_id": "prior-1"}]
    harness = _Harness(
        child_results={"run-1-c0": _child_record("0"), "run-1-c1": _child_record("1")},
        activity_handlers=_default_activity_handlers(
            snapshot_prior_records_activity=lambda a: prior,
        ),
    )
    _run(_batch_input(), harness)
    c0 = harness.child_start_args["run-1-c0"]
    c1 = harness.child_start_args["run-1-c1"]
    # Same per-wave prior-records snapshot handed to both cycles.
    assert c0["prior_records"] == prior
    assert c1["prior_records"] == prior
    # Each cycle carries its own tracker snapshot + the threaded workflow config.
    assert "convergence_tracker_state" in c0
    assert c0["workflow_config"] == _WF_CONFIG
    assert c0["signal_brief"] == {"brief_version": "v1"}


# ---------------------------------------------------------------------------
# Wave merge — sorted by cycle index regardless of completion order
# ---------------------------------------------------------------------------


def test_wave_merged_into_tracker_in_cycle_index_order():
    captured: Dict[str, Any] = {}

    def _merge(args):
        captured["wave_results"] = args[0]["wave_results"]
        return {"primary_tracker_state": {"ok": True}}

    harness = _Harness(
        child_results={"run-1-c0": _child_record("0"), "run-1-c1": _child_record("1")},
        activity_handlers=_default_activity_handlers(merge_wave_results_activity=_merge),
    )
    result = _run(_batch_input(), harness)
    indices = [w["cycle_index"] for w in captured["wave_results"]]
    assert indices == sorted(indices) == [0, 1]
    # Each merged entry carries the child's returned tracker state + finalized record.
    by_index = {w["cycle_index"]: w for w in captured["wave_results"]}
    assert by_index[0]["cycle_tracker_state"]["src"] == "0"
    assert by_index[0]["record"]["lab_record_id"] == "rec-0"
    assert result["convergence_tracker_state"] == {"ok": True}


def test_multiple_waves_when_wave_exceeds_max_parallel():
    # 3 cycles, max_parallel=2 → wave [0,1] then wave [2].
    harness = _Harness(
        child_results={
            "run-1-c0": _child_record("0"),
            "run-1-c1": _child_record("1"),
            "run-1-c2": _child_record("2"),
        },
        activity_handlers=_default_activity_handlers(),
    )
    result = _run(_batch_input(batch_size=3, max_parallel=2), harness)
    assert result["status"] == "completed"
    # Two waves → two prior-record snapshots + two merges.
    assert harness.activity_calls.count("snapshot_prior_records_activity") == 2
    assert harness.activity_calls.count("merge_wave_results_activity") == 2
    assert harness.child_starts == ["run-1-c0", "run-1-c1", "run-1-c2"]


# ---------------------------------------------------------------------------
# Skipped cycles (no market data)
# ---------------------------------------------------------------------------


def test_skipped_cycle_is_counted_and_not_finalized():
    captured: Dict[str, Any] = {}

    def _merge(args):
        captured["wave_results"] = args[0]["wave_results"]
        return {"primary_tracker_state": {"ok": True}}

    harness = _Harness(
        child_results={"run-1-c0": _child_record("0"), "run-1-c1": _child_skipped()},
        activity_handlers=_default_activity_handlers(merge_wave_results_activity=_merge),
    )
    result = _run(_batch_input(), harness)

    assert result["status"] == "completed"
    assert result["skipped_cycles"] == 1
    assert result["errored_cycles"] == 0
    # Only the surviving cycle was finalized/collected/merged into the tracker.
    assert result["completed_record_ids"] == ["rec-0"]
    assert harness.activity_calls.count("finalize_cycle_record_activity") == 1
    assert [w["cycle_index"] for w in captured["wave_results"]] == [0]


def test_seeded_skipped_cycles_are_additive_not_overwritten():
    """A resumed run's skipped_cycles seed (#4014) is added to, not
    overwritten by, new skips in this dispatch."""
    harness = _Harness(
        child_results={"run-1-c0": _child_skipped(), "run-1-c1": _child_skipped()},
        activity_handlers=_default_activity_handlers(),
    )
    result = _run(_batch_input(skipped_cycles=3), harness)
    assert result["skipped_cycles"] == 5  # 3 seeded + 2 new


def test_skipped_cycles_persisted_mid_run():
    persisted: List[Dict[str, Any]] = []
    harness = _Harness(
        child_results={"run-1-c0": _child_record("0"), "run-1-c1": _child_skipped()},
        activity_handlers=_default_activity_handlers(
            persist_run_state_activity=lambda a: persisted.append(a[1]),
        ),
    )
    _run(_batch_input(), harness)
    assert any(s.get("skipped_cycles") == 1 for s in persisted)


# ---------------------------------------------------------------------------
# Cancellation + errored cycles
# ---------------------------------------------------------------------------


def test_cancellation_between_waves_stops_and_marks_cancelled():
    persisted: List[Dict[str, Any]] = []
    harness = _Harness(
        child_results={
            "run-1-c0": _child_record("0"),
            "run-1-c1": _child_record("1"),
            "run-1-c2": _child_record("2"),
            "run-1-c3": _child_record("3"),
        },
        activity_handlers=_default_activity_handlers(
            # Cancelled after the first wave — the status-returning activity
            # yields the true external status verbatim.
            external_terminal_status_activity=lambda a: "cancelled",
            persist_run_state_activity=lambda a: persisted.append(a[1]),
        ),
    )
    # 4 cycles, max_parallel=2 → wave [0,1] runs, cancel check trips, wave [2,3] never starts.
    result = _run(_batch_input(batch_size=4, max_parallel=2), harness)
    assert result["status"] == "cancelled"
    # Wave 2's children never started after the cancel check returned non-None.
    assert harness.child_starts == ["run-1-c0", "run-1-c1"]
    # The external-stop check runs *between* waves: after wave 1's merge +
    # run-state persist, not before them.
    calls = harness.activity_calls
    first_check = calls.index("external_terminal_status_activity")
    assert "merge_wave_results_activity" in calls[:first_check]
    assert "persist_run_state_activity" in calls[:first_check]
    # No further wave was launched, so only one external-stop check happened.
    assert calls.count("external_terminal_status_activity") == 1
    # Terminal status persisted.
    assert any(s.get("status") == "cancelled" for s in persisted)


@pytest.mark.parametrize("external_status", ["interrupted", "failed"])
def test_external_interrupt_or_failure_not_mislabeled_cancelled(external_status: str):
    """Regression: an external stop marked 'interrupted'/'failed' (e.g. a
    service-wide reconciliation) must be persisted as that TRUE status in
    Temporal mode, never forced to 'cancelled' — matching thread mode."""
    persisted: List[Dict[str, Any]] = []
    harness = _Harness(
        child_results={
            "run-1-c0": _child_record("0"),
            "run-1-c1": _child_record("1"),
            "run-1-c2": _child_record("2"),
            "run-1-c3": _child_record("3"),
        },
        activity_handlers=_default_activity_handlers(
            external_terminal_status_activity=lambda a: external_status,
            persist_run_state_activity=lambda a: persisted.append(a[1]),
        ),
    )
    result = _run(_batch_input(batch_size=4, max_parallel=2), harness)
    # The true external status survives — not overwritten to 'cancelled'.
    assert result["status"] == external_status
    assert harness.child_starts == ["run-1-c0", "run-1-c1"]
    assert any(s.get("status") == external_status for s in persisted)
    assert not any(s.get("status") == "cancelled" for s in persisted)


def test_contiguous_prefix():
    from investment_team.strategy_lab.temporal.workflows import _contiguous_prefix

    assert _contiguous_prefix(set()) == 0
    assert _contiguous_prefix({0}) == 1
    assert _contiguous_prefix({1, 2}) == 0  # index 0 missing → no contiguous prefix
    assert _contiguous_prefix({0, 1, 2}) == 3  # fully contiguous
    assert _contiguous_prefix({0, 1, 3}) == 2  # gap at 2 stops the prefix
    assert _contiguous_prefix({0, 2, 3, 5}) == 1


def test_errored_cycle_is_counted_and_yields_completed_with_errors():
    harness = _Harness(
        child_results={
            "run-1-c0": _child_record("0"),
            "run-1-c1": RuntimeError("cycle blew up"),
        },
        activity_handlers=_default_activity_handlers(),
    )
    result = _run(_batch_input(), harness)
    assert result["status"] == "completed_with_errors"
    assert result["errored_cycles"] == 1
    # Only the surviving cycle was finalized + collected.
    assert result["completed_record_ids"] == ["rec-0"]
    assert harness.activity_calls.count("finalize_cycle_record_activity") == 1


def test_signal_brief_refreshed_once_per_batch():
    harness = _Harness(
        child_results={f"run-1-c{i}": _child_record(str(i)) for i in range(4)},
        activity_handlers=_default_activity_handlers(),
    )
    # 2 batches × batch_size 2.
    _run(_batch_input(batch_size=2, batch_count=2), harness)
    assert harness.activity_calls.count("compute_signal_brief_activity") == 2


# ---------------------------------------------------------------------------
# Module exports
# ---------------------------------------------------------------------------


def test_batch_workflow_registered():
    assert wf.StrategyLabBatchWorkflow in wf.WORKFLOWS
