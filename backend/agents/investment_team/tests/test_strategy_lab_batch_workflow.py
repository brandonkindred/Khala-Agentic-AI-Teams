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
    function name → ``(args) -> result``. ``patched_result`` stands in for
    ``workflow.patched("strategy-lab-sse-run-events")`` — real workflow code, so
    it needs its own mock the same way ``execute_activity``/
    ``start_child_workflow`` do; defaults to ``True`` (the "fresh execution, not
    replaying a pre-SSE-events history" case) so existing tests exercise the new
    publish call sites by default. A test asserting the False (in-flight-at-deploy
    replay) branch skips them instead passes ``patched_result=False``.
    """

    def __init__(
        self,
        child_results: Dict[str, Any],
        activity_handlers: Dict[str, Any],
        *,
        patched_result: bool = True,
    ) -> None:
        self.child_results = child_results
        self.activity_handlers = activity_handlers
        self.child_starts: List[str] = []
        self.child_start_args: Dict[str, Any] = {}
        # For each awaited child, how many children had been *started* by then —
        # lets a test prove all children in a wave start before any is awaited.
        self.start_count_at_await: List[int] = []
        self.activity_calls: List[str] = []
        self.patched_result = patched_result
        self.patched_calls: List[str] = []

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

    def patched(self, patch_id: str) -> bool:
        self.patched_calls.append(patch_id)
        return self.patched_result

    def patch(self):
        return (
            mock.patch("temporalio.workflow.start_child_workflow", self.start_child_workflow),
            mock.patch("temporalio.workflow.execute_activity", self.execute_activity),
            mock.patch("temporalio.workflow.patched", self.patched),
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
        "publish_run_event_activity": lambda a: None,
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
    p1, p2, p3 = harness.patch()
    with p1, p2, p3:
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


def test_each_cycle_gets_the_batch_scoped_cache_key():
    """Every cycle in a batch carries the same deterministic ``batch_cache_key``
    (``f"{run_id}-b{batch_idx}"``), so the worker resolves one shared
    BatchIndicatorCache per batch from it. Two batches → two distinct keys."""
    child_results = {
        "run-1-c0": _child_record("0"),
        "run-1-c1": _child_record("1"),
        "run-1-c2": _child_record("2"),
        "run-1-c3": _child_record("3"),
    }
    harness = _Harness(
        child_results=child_results,
        activity_handlers=_default_activity_handlers(),
    )
    _run(_batch_input(batch_size=2, batch_count=2), harness)

    # Batch 0's two cycles share key "run-1-b0"; batch 1's share "run-1-b1".
    assert harness.child_start_args["run-1-c0"]["batch_cache_key"] == "run-1-b0"
    assert harness.child_start_args["run-1-c1"]["batch_cache_key"] == "run-1-b0"
    assert harness.child_start_args["run-1-c2"]["batch_cache_key"] == "run-1-b1"
    assert harness.child_start_args["run-1-c3"]["batch_cache_key"] == "run-1-b1"


def test_each_cycle_input_carries_its_own_cycle_index():
    """Every child's ``cycle_input`` carries the same 0-based ``cycle_index``
    baked into its deterministic child-workflow id (``f"{run_id}-c{cycle_index}"``)
    -- this is what lets ``run_design_attempt_activity``'s progress-publish
    checkpoint attach a ``StrategyLabProgressEvent.cycle_index``."""
    harness = _Harness(
        child_results={"run-1-c0": _child_record("0"), "run-1-c1": _child_record("1")},
        activity_handlers=_default_activity_handlers(),
    )
    _run(_batch_input(), harness)

    assert harness.child_start_args["run-1-c0"]["cycle_index"] == 0
    assert harness.child_start_args["run-1-c1"]["cycle_index"] == 1


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


def test_seeded_completed_record_ids_are_extended_not_overwritten():
    """A resumed run's completed_record_ids seed is extended with newly
    finalized ids, not overwritten — required because persist_run_state's
    job-service write replaces the field's value wholesale rather than
    appending, so the seed is the only way pre-resume ids survive the first
    mid-run persist after resume."""
    harness = _Harness(
        child_results={"run-1-c0": _child_record("0"), "run-1-c1": _child_record("1")},
        activity_handlers=_default_activity_handlers(),
    )
    result = _run(_batch_input(completed_record_ids=["prior-1", "prior-2"]), harness)
    assert result["completed_record_ids"] == ["prior-1", "prior-2", "rec-0", "rec-1"]


def test_completed_record_ids_seed_persisted_mid_run():
    persisted: List[Dict[str, Any]] = []
    harness = _Harness(
        child_results={"run-1-c0": _child_record("0"), "run-1-c1": _child_record("1")},
        activity_handlers=_default_activity_handlers(
            persist_run_state_activity=lambda a: persisted.append(a[1]),
        ),
    )
    _run(_batch_input(completed_record_ids=["prior-1"]), harness)
    assert any(p.get("completed_record_ids") == ["prior-1", "rec-0", "rec-1"] for p in persisted)


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


def test_errored_details_captures_structured_entry_for_failed_cycle():
    harness = _Harness(
        child_results={
            "run-1-c0": _child_record("0"),
            "run-1-c1": RuntimeError("cycle blew up"),
        },
        activity_handlers=_default_activity_handlers(),
    )
    result = _run(_batch_input(), harness)
    assert result["errored_details"] == [
        {
            "cycle_index": 2,
            "batch_index": 1,
            "error": "cycle blew up",
            "exception_type": "RuntimeError",
        }
    ]


def test_errored_details_walks_full_cause_chain_to_terminal_failure():
    """A real child-workflow failure surfaces as a multi-hop chain —
    ChildWorkflowError -> ActivityError -> ApplicationError (the domain
    error) — so ``exception_type``/``error`` must reflect the terminal cause,
    not just one ``__cause__`` hop (which would only reach the generic
    RPC-boundary wrapper, e.g. ActivityError, not the real failure)."""

    class _OuterChildWorkflowError(Exception):
        pass

    class _MiddleActivityError(Exception):
        pass

    class _DomainValueError(Exception):
        pass

    terminal = _DomainValueError("root cause")
    middle = _MiddleActivityError("activity task failed")
    middle.__cause__ = terminal
    outer = _OuterChildWorkflowError("child workflow execution failed")
    outer.__cause__ = middle

    harness = _Harness(
        child_results={"run-1-c0": outer, "run-1-c1": _child_record("1")},
        activity_handlers=_default_activity_handlers(),
    )
    result = _run(_batch_input(), harness)
    assert result["errored_details"] == [
        {
            "cycle_index": 1,
            "batch_index": 1,
            "error": "root cause",
            "exception_type": "_DomainValueError",
        }
    ]


def test_errored_details_falls_back_to_context_when_no_cause():
    """When a failure has no explicit ``__cause__`` chain but does have an
    implicit ``__context__`` chain (raised while handling another exception,
    without ``from``), the walk must still reach the terminal cause instead
    of stopping at the outer wrapper — mirroring Python's own traceback
    resolution rule of falling back to ``__context__`` once ``__cause__`` is
    exhausted."""

    class _OuterChildWorkflowError(Exception):
        pass

    class _DomainValueError(Exception):
        pass

    terminal = _DomainValueError("root cause")
    outer = _OuterChildWorkflowError("child workflow execution failed")
    outer.__context__ = terminal
    outer.__suppress_context__ = False

    harness = _Harness(
        child_results={"run-1-c0": outer, "run-1-c1": _child_record("1")},
        activity_handlers=_default_activity_handlers(),
    )
    result = _run(_batch_input(), harness)
    assert result["errored_details"] == [
        {
            "cycle_index": 1,
            "batch_index": 1,
            "error": "root cause",
            "exception_type": "_DomainValueError",
        }
    ]


def test_errored_details_does_not_follow_suppressed_context():
    """``raise ... from None`` sets ``__suppress_context__`` to signal the
    implicit ``__context__`` chain is deliberately not the real cause — the
    walk must stop at the outer exception rather than reporting the
    suppressed context as the terminal failure."""

    class _OuterChildWorkflowError(Exception):
        pass

    class _UnrelatedContext(Exception):
        pass

    context = _UnrelatedContext("unrelated exception being handled")
    outer = _OuterChildWorkflowError("child workflow execution failed")
    outer.__context__ = context
    outer.__suppress_context__ = True

    harness = _Harness(
        child_results={"run-1-c0": outer, "run-1-c1": _child_record("1")},
        activity_handlers=_default_activity_handlers(),
    )
    result = _run(_batch_input(), harness)
    assert result["errored_details"] == [
        {
            "cycle_index": 1,
            "batch_index": 1,
            "error": "child workflow execution failed",
            "exception_type": "_OuterChildWorkflowError",
        }
    ]


def test_errored_details_uses_application_error_type_not_class_name():
    """The real terminal cause is usually the ApplicationError
    ``_map_exception_to_application_error`` produced, whose actionable
    classification lives in ``.type`` (e.g. "ValueError" or an LLM outcome) —
    the Python class name is just "ApplicationError" for every such failure,
    which would defeat the whole point of walking the chain."""
    from temporalio.exceptions import ApplicationError

    terminal = ApplicationError("bad json", type="ValueError", non_retryable=True)
    middle = Exception("activity task failed")
    middle.__cause__ = terminal
    outer = Exception("child workflow execution failed")
    outer.__cause__ = middle

    harness = _Harness(
        child_results={"run-1-c0": outer, "run-1-c1": _child_record("1")},
        activity_handlers=_default_activity_handlers(),
    )
    result = _run(_batch_input(), harness)
    assert result["errored_details"][0]["exception_type"] == "ValueError"


def test_errored_details_cap_enforced():
    n = 55
    child_results = {f"run-1-c{i}": RuntimeError(f"boom-{i}") for i in range(n)}
    harness = _Harness(
        child_results=child_results,
        activity_handlers=_default_activity_handlers(),
    )
    result = _run(_batch_input(batch_size=n, max_parallel=n), harness)
    assert result["errored_cycles"] == n
    assert len(result["errored_details"]) == wf._ERRORED_DETAILS_MAX


def test_seeded_errored_details_are_extended_not_overwritten():
    seed = [{"cycle_index": 99, "batch_index": 9, "error": "old", "exception_type": "OldError"}]
    harness = _Harness(
        child_results={
            "run-1-c0": _child_record("0"),
            "run-1-c1": RuntimeError("new failure"),
        },
        activity_handlers=_default_activity_handlers(),
    )
    result = _run(_batch_input(errored_details=seed), harness)
    assert result["errored_details"][0] == seed[0]
    assert len(result["errored_details"]) == 2
    assert result["errored_details"][1]["error"] == "new failure"


def test_errored_details_persisted_mid_run():
    persisted: List[Dict[str, Any]] = []
    harness = _Harness(
        child_results={
            "run-1-c0": _child_record("0"),
            "run-1-c1": RuntimeError("cycle blew up"),
        },
        activity_handlers=_default_activity_handlers(
            persist_run_state_activity=lambda a: persisted.append(a[1]),
        ),
    )
    _run(_batch_input(), harness)
    assert any("errored_details" in p and p["errored_details"] for p in persisted)


def test_merge_error_is_folded_without_failing_the_batch():
    """A tracker-merge failure reported by ``merge_wave_results_activity``
    (isolated per-record, per #4016/#4017) degrades to a soft counter bump —
    it never propagates as an exception through ``run()``."""
    harness = _Harness(
        child_results={"run-1-c0": _child_record("0"), "run-1-c1": _child_record("1")},
        activity_handlers=_default_activity_handlers(
            merge_wave_results_activity=lambda a: {
                "primary_tracker_state": {"merged": True},
                "merge_errors": [
                    {
                        "cycle_index": 1,
                        "error": "merge boom",
                        "exception_type": "ValueError",
                        "reason": "tracker_merge_failed",
                    }
                ],
            },
        ),
    )
    result = _run(_batch_input(), harness)
    assert result["status"] == "completed_with_errors"
    assert result["errored_cycles"] == 1
    assert result["tracker_merge_error_count"] == 1
    assert result["errored_details"] == [
        {
            "cycle_index": 1,
            "batch_index": 1,
            "error": "merge boom",
            "exception_type": "ValueError",
            "reason": "tracker_merge_failed",
        }
    ]
    # Both cycles were still finalized — the merge failure didn't drop a record.
    assert sorted(result["completed_record_ids"]) == ["rec-0", "rec-1"]


def test_seeded_tracker_merge_error_count_is_added_not_overwritten():
    harness = _Harness(
        child_results={"run-1-c0": _child_record("0"), "run-1-c1": _child_record("1")},
        activity_handlers=_default_activity_handlers(
            merge_wave_results_activity=lambda a: {
                "primary_tracker_state": {"merged": True},
                "merge_errors": [
                    {
                        "cycle_index": 1,
                        "error": "merge boom",
                        "exception_type": "ValueError",
                        "reason": "tracker_merge_failed",
                    }
                ],
            },
        ),
    )
    result = _run(_batch_input(tracker_merge_error_count=4), harness)
    assert result["tracker_merge_error_count"] == 5


def test_tracker_merge_error_count_persisted_mid_run():
    persisted: List[Dict[str, Any]] = []
    harness = _Harness(
        child_results={"run-1-c0": _child_record("0"), "run-1-c1": _child_record("1")},
        activity_handlers=_default_activity_handlers(
            merge_wave_results_activity=lambda a: {
                "primary_tracker_state": {"merged": True},
                "merge_errors": [
                    {
                        "cycle_index": 1,
                        "error": "merge boom",
                        "exception_type": "ValueError",
                        "reason": "tracker_merge_failed",
                    }
                ],
            },
            persist_run_state_activity=lambda a: persisted.append(a[1]),
        ),
    )
    _run(_batch_input(), harness)
    assert any(p.get("tracker_merge_error_count") == 1 for p in persisted)


def test_signal_brief_refreshed_once_per_batch():
    harness = _Harness(
        child_results={f"run-1-c{i}": _child_record(str(i)) for i in range(4)},
        activity_handlers=_default_activity_handlers(),
    )
    # 2 batches × batch_size 2.
    _run(_batch_input(batch_size=2, batch_count=2), harness)
    assert harness.activity_calls.count("compute_signal_brief_activity") == 2


# ---------------------------------------------------------------------------
# Resume carry-forward regression — ported from test_strategy_lab_resume.py
# (which exercised thread-mode's _strategy_lab_worker directly). A skip
# encountered after resume must ADD to the pre-resume skipped count, not
# overwrite it, and every persisted snapshot's progress counters must be
# monotonically non-decreasing from the pre-resume floor.
# ---------------------------------------------------------------------------


def _resume_batch_input(**overrides: Any) -> Dict[str, Any]:
    """A 10-cycle run (2 batches x 5) resumed after 3 contiguous completions
    and 2 prior skips — mirrors the pre-resume snapshot
    ``resume_strategy_lab_run`` would have carried into ``batch_input`` via
    ``rehydrate_active_run_offset``/``get_resume_seed_counters``.
    ``max_parallel=1`` keeps waves single-cycle so persisted snapshots are
    deterministic and individually inspectable."""
    return _batch_input(
        batch_size=5,
        batch_count=2,
        max_parallel=1,
        start_cycle_offset=3,
        skipped_cycles=2,
        completed_record_ids=["r1", "r2", "r3"],
        **overrides,
    )


def test_resume_carries_forward_skipped_cycles():
    """A skip encountered after resume must ADD to the pre-resume skipped
    count. Before #4014/#4015 existed, the Temporal path had no seed at all,
    so any post-resume skip would have been the run's only recorded skip."""
    harness = _Harness(
        child_results={
            "run-1-c3": _child_skipped(),  # first resumed cycle: no market data
            **{f"run-1-c{i}": _child_record(str(i)) for i in range(4, 10)},
        },
        activity_handlers=_default_activity_handlers(),
    )
    result = _run(_resume_batch_input(), harness)

    # 2 prior skips + 1 new skip = 3.
    assert result["skipped_cycles"] == 3
    # 3 prior completions + 6 new successes = 9 (cycle index 3 skipped, 4..9 succeed).
    assert result["completed_record_ids"] == [
        "r1",
        "r2",
        "r3",
        "rec-4",
        "rec-5",
        "rec-6",
        "rec-7",
        "rec-8",
        "rec-9",
    ]
    assert result["status"] == "completed"


def test_resume_progress_never_moves_backward():
    """Across every persisted snapshot, completed_cycles and skipped_cycles
    must be monotonically non-decreasing from the pre-resume floor."""
    persisted: List[Dict[str, Any]] = []
    harness = _Harness(
        child_results={f"run-1-c{i}": _child_record(str(i)) for i in range(3, 10)},
        activity_handlers=_default_activity_handlers(
            persist_run_state_activity=lambda a: persisted.append(a[1]),
        ),
    )
    _run(_resume_batch_input(), harness)

    last_completed = 3  # pre-resume floor
    last_skipped = 2  # pre-resume floor
    saw_completed_cycles = False
    for snap in persisted:
        if "completed_cycles" not in snap:
            continue
        saw_completed_cycles = True
        completed = snap["completed_cycles"]
        skipped = snap.get("skipped_cycles", last_skipped)
        assert completed >= last_completed, (
            f"completed_cycles moved backward: {last_completed} -> {completed}"
        )
        assert skipped >= last_skipped, (
            f"skipped_cycles moved backward: {last_skipped} -> {skipped}"
        )
        last_completed = completed
        last_skipped = skipped

    assert saw_completed_cycles
    assert last_completed == 3 + 7  # 3 prior + 7 new successful cycles (3..9)
    assert last_skipped == 2  # no post-resume skips in this scenario


# ---------------------------------------------------------------------------
# Generation fencing threading (#4029)
# ---------------------------------------------------------------------------


def test_generation_threaded_into_finalize_and_persist_activities():
    """batch_input's generation reaches every finalize_cycle_record_activity params
    dict and every persist_run_state_activity call's args (all 4 _persist_state
    call sites, exercised by one realistic run: current_batch, wave-progress,
    completed_batches, and final status)."""
    captured_finalize_params: List[Dict[str, Any]] = []
    captured_persist_args: List[Any] = []
    harness = _Harness(
        child_results={"run-1-c0": _child_record("0"), "run-1-c1": _child_record("1")},
        activity_handlers=_default_activity_handlers(
            finalize_cycle_record_activity=lambda a: (
                captured_finalize_params.append(a[0]),
                {"record": {"lab_record_id": f"rec-{a[0]['record']['lab_record_id']}"}},
            )[1],
            persist_run_state_activity=lambda a: captured_persist_args.append(a),
        ),
    )
    _run(_batch_input(generation=5), harness)

    assert captured_finalize_params, "finalize_cycle_record_activity was never called"
    for params in captured_finalize_params:
        assert params["run_id"] == "run-1"
        assert params["generation"] == 5

    # 4 _persist_state call sites: current_batch, wave-progress, completed_batches, status.
    assert len(captured_persist_args) == 4
    for args in captured_persist_args:
        run_id, _state, create, generation = args
        assert run_id == "run-1"
        assert create is False
        assert generation == 5


def test_generation_defaults_to_one_when_absent_from_batch_input():
    """Backward compat: a batch_input predating this field (e.g. a workflow-history
    replay) defaults to generation 1 rather than raising."""
    captured_persist_args: List[Any] = []
    harness = _Harness(
        child_results={"run-1-c0": _child_record("0"), "run-1-c1": _child_record("1")},
        activity_handlers=_default_activity_handlers(
            persist_run_state_activity=lambda a: captured_persist_args.append(a),
        ),
    )
    bi = _batch_input()
    assert "generation" not in bi
    _run(bi, harness)

    assert captured_persist_args
    for args in captured_persist_args:
        assert args[3] == 1


# ---------------------------------------------------------------------------
# SSE run-event publishing — cycle_skipped / cycle_complete / terminal
# complete/error/cancelled, via publish_run_event_activity.
# ---------------------------------------------------------------------------


def test_batch_publishes_cycle_complete_after_each_finalized_cycle():
    published: List[Dict[str, Any]] = []
    harness = _Harness(
        child_results={"run-1-c0": _child_record("0"), "run-1-c1": _child_record("1")},
        activity_handlers=_default_activity_handlers(
            publish_run_event_activity=lambda a: published.append(a[0]),
        ),
    )
    _run(_batch_input(), harness)

    cycle_complete = [
        p["event"]
        for p in published
        if p["run_id"] == "run-1" and p["event"]["type"] == "cycle_complete"
    ]
    assert [e["cycle_index"] for e in cycle_complete] == [0, 1]
    assert [e["record_id"] for e in cycle_complete] == ["rec-0", "rec-1"]
    assert [e["completed_cycles"] for e in cycle_complete] == [1, 2]
    assert all(e["batch_index"] == 1 for e in cycle_complete)


def test_batch_publishes_cycle_skipped_for_a_no_market_data_cycle():
    published: List[Dict[str, Any]] = []
    harness = _Harness(
        child_results={"run-1-c0": _child_record("0"), "run-1-c1": _child_skipped()},
        activity_handlers=_default_activity_handlers(
            publish_run_event_activity=lambda a: published.append(a[0]),
        ),
    )
    _run(_batch_input(), harness)

    skipped = [p["event"] for p in published if p["event"]["type"] == "cycle_skipped"]
    assert len(skipped) == 1
    assert skipped[0] == {
        "type": "cycle_skipped",
        "cycle_index": 1,
        "reason": "no_market_data",
        "batch_index": 1,
    }
    # A skipped cycle is never finalized, so it never gets a cycle_complete too.
    assert [p["event"]["type"] for p in published if p["event"]["type"] == "cycle_complete"] == [
        "cycle_complete"
    ]


def test_batch_publishes_terminal_complete_event_on_clean_finish():
    published: List[Dict[str, Any]] = []
    harness = _Harness(
        child_results={"run-1-c0": _child_record("0"), "run-1-c1": _child_record("1")},
        activity_handlers=_default_activity_handlers(
            publish_run_event_activity=lambda a: published.append(a[0]),
        ),
    )
    _run(_batch_input(), harness)

    terminal = [
        p["event"] for p in published if p["event"]["type"] in ("complete", "error", "cancelled")
    ]
    assert len(terminal) == 1
    assert terminal[0]["type"] == "complete"
    assert terminal[0]["status"] == "completed"
    assert terminal[0]["completed_count"] == 2
    assert terminal[0]["skipped_count"] == 0
    assert terminal[0]["errored_count"] == 0
    assert terminal[0]["completed_batches"] == 1
    assert terminal[0]["total_batches"] == 1


def test_batch_publishes_terminal_error_event_for_external_failure():
    published: List[Dict[str, Any]] = []
    harness = _Harness(
        child_results={"run-1-c0": _child_record("0"), "run-1-c1": _child_record("1")},
        activity_handlers=_default_activity_handlers(
            external_terminal_status_activity=lambda a: "failed",
            publish_run_event_activity=lambda a: published.append(a[0]),
        ),
    )
    result = _run(_batch_input(), harness)

    assert result["status"] == "failed"
    terminal = [
        p["event"] for p in published if p["event"]["type"] in ("complete", "error", "cancelled")
    ]
    assert len(terminal) == 1
    assert terminal[0] == {"type": "error", "detail": "Run failed.", "terminal_status": "failed"}


def test_batch_publishes_terminal_cancelled_event():
    published: List[Dict[str, Any]] = []
    harness = _Harness(
        child_results={"run-1-c0": _child_record("0"), "run-1-c1": _child_record("1")},
        activity_handlers=_default_activity_handlers(
            external_terminal_status_activity=lambda a: "cancelled",
            publish_run_event_activity=lambda a: published.append(a[0]),
        ),
    )
    result = _run(_batch_input(), harness)

    assert result["status"] == "cancelled"
    terminal = [
        p["event"] for p in published if p["event"]["type"] in ("complete", "error", "cancelled")
    ]
    assert terminal == [{"type": "cancelled", "detail": "Run cancelled."}]


def test_batch_terminal_complete_event_counts_errored_cycles():
    published: List[Dict[str, Any]] = []
    harness = _Harness(
        child_results={"run-1-c0": _child_record("0"), "run-1-c1": RuntimeError("boom")},
        activity_handlers=_default_activity_handlers(
            publish_run_event_activity=lambda a: published.append(a[0]),
        ),
    )
    result = _run(_batch_input(), harness)

    assert result["status"] == "completed_with_errors"
    terminal = [p["event"] for p in published if p["event"]["type"] == "complete"]
    assert len(terminal) == 1
    assert terminal[0]["status"] == "completed_with_errors"
    assert terminal[0]["errored_count"] == 1
    assert terminal[0]["completed_count"] == 1


def test_no_sse_events_published_when_not_patched():
    """A run already in flight when SSE publishing shipped replays with
    ``workflow.patched(...)`` False and simply never publishes for the rest of
    its lifetime — the pre-existing, already-safe degraded behavior, not an
    error. The run's own return value/persisted state is unaffected."""
    published: List[Dict[str, Any]] = []
    harness = _Harness(
        child_results={"run-1-c0": _child_record("0"), "run-1-c1": _child_record("1")},
        activity_handlers=_default_activity_handlers(
            publish_run_event_activity=lambda a: published.append(a[0]),
        ),
        patched_result=False,
    )
    result = _run(_batch_input(), harness)

    assert published == []
    assert harness.patched_calls == ["strategy-lab-sse-run-events"]
    assert result["status"] == "completed"
    assert sorted(result["completed_record_ids"]) == ["rec-0", "rec-1"]
    # publish_run_event_activity itself is simply never called at all.
    assert "publish_run_event_activity" not in harness.activity_calls


# ---------------------------------------------------------------------------
# Module exports
# ---------------------------------------------------------------------------


def test_batch_workflow_registered():
    assert wf.StrategyLabBatchWorkflow in wf.WORKFLOWS
