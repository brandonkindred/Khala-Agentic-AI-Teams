"""Unit tests for ``strategy_lab.temporal.workflows.StrategyLabCycleWorkflow``.

The cycle workflow is a *thin* driver of ``run_cycle``'s outer design-re-entry
loop: it calls ``run_design_attempt_activity`` once per attempt and branches on
that activity's structured ``{"kind": "record" | "reentry", ...}`` outcome. The
whole per-attempt phase pipeline runs verbatim inside that activity (see
``activities.run_design_attempt_activity``), so these tests exercise only the
workflow's own control flow — loop bounds, config/regime resolution, budget /
gate-result / tracker threading across re-entries, and short-circuit assembly —
with every activity mocked.

Pattern mirrors the codebase's other workflow tests
(``agentic_team_provisioning/tests/test_temporal_activity.py``): monkeypatch
``temporalio.workflow.execute_activity`` to dispatch by activity-function name
to canned responses and drive ``run()`` directly with ``asyncio.run`` — no live
Temporal server or sandbox needed. The dedicated real-sandbox regression guard
lives in ``test_strategy_lab_temporal_sandbox.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List
from unittest import mock

from investment_team.strategy_lab.temporal import workflows as wf


def _config_dict(**overrides: Any) -> Dict[str, Any]:
    base = {"start_date": "2023-01-01", "end_date": "2023-12-31"}
    base.update(overrides)
    return base


_WF_CONFIG = {
    "design_review_rounds": 20,
    "design_review_stall_rounds": 3,
    # Exercise the true default so a regression that resets the mechanical
    # repair flag (which hid two crash bugs in the earlier hand-ported port)
    # would surface here rather than being masked.
    "mechanical_repair_enabled": True,
    "code_conformance_retries": 2,
    "design_max_llm_calls": 120,
    "regime_summary_enabled": False,
    "max_design_reentries": 2,
}


def _patch_execute(handlers: Dict[str, Any], *, calls: List[str] | None = None):
    """Context manager patching ``workflow.execute_activity``.

    ``handlers`` maps activity-function *name* to a callable ``(args) -> result``
    or a plain return value. ``calls`` (if given) records every invoked name in
    order, so a test can assert the exact activity call sequence.
    """

    async def _fake_exec(fn, *, args, **_kw):
        name = fn.__name__
        if calls is not None:
            calls.append(name)
        if name not in handlers:
            raise AssertionError(f"unexpected activity call: {name}")
        handler = handlers[name]
        if callable(handler):
            return handler(args)
        return handler

    return mock.patch("temporalio.workflow.execute_activity", _fake_exec)


def _reentry_outcome(**overrides: Any) -> Dict[str, Any]:
    base = {
        "kind": "reentry",
        "evidence": "always fails",
        "last_spec": {"strategy_id": "strat-1"},
        "last_code": "code",
        "failure_phase": "evaluation",
        "design_context": {"rounds": 1, "critiques": [], "stop_reason": "x", "loop_telemetry": {}},
        "convergence_tracker_state": {"trial_count": 0},
        "gate_results": [],
        "budget_calls": 0,
        "drift": {"spec_history": [], "code_history": [], "gate_timeline": []},
    }
    base.update(overrides)
    return base


def _record_outcome(**overrides: Any) -> Dict[str, Any]:
    base = {
        "kind": "record",
        "record": {"lab_record_id": "rec-1"},
        "convergence_tracker_state": {"trial_count": 1},
        "gate_results": [],
        "budget_calls": 3,
        "drift": {"spec_history": [], "code_history": [], "gate_timeline": []},
    }
    base.update(overrides)
    return base


def _skipped_outcome(**overrides: Any) -> Dict[str, Any]:
    base = {
        "kind": "skipped",
        "reason": "no_market_data",
        "convergence_tracker_state": {"trial_count": 1},
        "gate_results": [],
        "budget_calls": 3,
        "drift": {"spec_history": [], "code_history": [], "gate_timeline": []},
    }
    base.update(overrides)
    return base


def _run(cycle_input: Dict[str, Any]) -> Dict[str, Any]:
    return asyncio.run(wf.StrategyLabCycleWorkflow().run(cycle_input))


# ---------------------------------------------------------------------------
# Happy path — first attempt yields a record
# ---------------------------------------------------------------------------


def test_run_returns_record_from_first_successful_attempt():
    calls: List[str] = []
    handlers = {
        "resolve_workflow_config_activity": lambda a: _WF_CONFIG,
        "run_design_attempt_activity": lambda a: _record_outcome(),
    }
    with _patch_execute(handlers, calls=calls):
        result = _run(
            {
                "prior_records": [],
                "config": _config_dict(),
                "signal_brief": None,
                "exclude_asset_classes": None,
                "convergence_tracker_state": {},
            }
        )
    assert result["record"]["lab_record_id"] == "rec-1"
    assert result["convergence_tracker_state"]["trial_count"] == 1
    # Exactly one attempt ran; no short-circuit build, no regime fetch.
    assert calls.count("run_design_attempt_activity") == 1
    assert "build_short_circuit_record_activity" not in calls
    assert "compute_regime_summary_activity" not in calls


def test_run_returns_skipped_outcome_immediately():
    """A ``kind='skipped'`` attempt outcome (no market data) is cycle-terminal
    right away — no further design-attempt retry, no short-circuit build."""
    calls: List[str] = []
    handlers = {
        "resolve_workflow_config_activity": lambda a: _WF_CONFIG,
        "run_design_attempt_activity": lambda a: _skipped_outcome(),
    }
    with _patch_execute(handlers, calls=calls):
        result = _run(
            {
                "prior_records": [],
                "config": _config_dict(),
                "signal_brief": None,
                "exclude_asset_classes": None,
                "convergence_tracker_state": {},
            }
        )
    assert result == {"kind": "skipped", "convergence_tracker_state": {"trial_count": 1}}
    assert "record" not in result
    assert calls.count("run_design_attempt_activity") == 1
    assert "build_short_circuit_record_activity" not in calls


def test_run_skips_config_resolution_when_already_supplied():
    calls: List[str] = []
    handlers = {"run_design_attempt_activity": lambda a: _record_outcome()}
    with _patch_execute(handlers, calls=calls):
        result = _run(
            {
                "prior_records": [],
                "config": _config_dict(),
                "convergence_tracker_state": {},
                "workflow_config": _WF_CONFIG,
            }
        )
    assert result["record"]["lab_record_id"] == "rec-1"
    assert "resolve_workflow_config_activity" not in calls


def test_run_fetches_regime_summary_when_enabled_and_passes_it_down():
    seen: Dict[str, Any] = {}

    def _attempt(args):
        seen["regime_summary"] = args[0]["regime_summary"]
        return _record_outcome()

    handlers = {
        "run_design_attempt_activity": _attempt,
        "compute_regime_summary_activity": lambda a: {"trend": "up"},
    }
    with _patch_execute(handlers):
        _run(
            {
                "prior_records": [],
                "config": _config_dict(),
                "convergence_tracker_state": {},
                "workflow_config": dict(_WF_CONFIG, regime_summary_enabled=True),
            }
        )
    assert seen["regime_summary"] == {"trend": "up"}


# ---------------------------------------------------------------------------
# Re-entry loop — SpecImplementabilityError surfaced as a structured outcome
# ---------------------------------------------------------------------------


def test_run_retries_until_bound_then_short_circuits():
    """Every attempt re-enters; after max_design_reentries+1 attempts the
    workflow builds the ``failed: spec_unimplementable`` short-circuit record."""
    calls: List[str] = []
    handlers = {
        "resolve_workflow_config_activity": lambda a: _WF_CONFIG,
        "run_design_attempt_activity": lambda a: _reentry_outcome(),
        "build_short_circuit_record_activity": lambda a: {
            "record": {
                "lab_record_id": "sc-1",
                "status": a[0]["short_circuit_status"],
                "phase_back_count": a[0]["phase_back_count"],
            },
            "convergence_tracker_state": a[0]["convergence_tracker_state"],
        },
    }
    with _patch_execute(handlers, calls=calls):
        result = _run(
            {
                "prior_records": [],
                "config": _config_dict(),
                "convergence_tracker_state": {},
            }
        )
    # max_design_reentries=2 -> 3 attempts, then one short-circuit build.
    assert calls.count("run_design_attempt_activity") == 3
    assert result["record"]["lab_record_id"] == "sc-1"
    assert result["record"]["status"] == "failed: spec_unimplementable"
    # phase_back_count is incremented once per re-entry, including the last.
    assert result["record"]["phase_back_count"] == 3


def test_run_recovers_on_second_attempt():
    outcomes = [_reentry_outcome(), _record_outcome(record={"lab_record_id": "rec-2"})]
    handlers = {
        "resolve_workflow_config_activity": lambda a: _WF_CONFIG,
        "run_design_attempt_activity": lambda a: outcomes.pop(0),
    }
    calls: List[str] = []
    with _patch_execute(handlers, calls=calls):
        result = _run(
            {
                "prior_records": [],
                "config": _config_dict(),
                "convergence_tracker_state": {},
            }
        )
    assert result["record"]["lab_record_id"] == "rec-2"
    assert calls.count("run_design_attempt_activity") == 2
    assert "build_short_circuit_record_activity" not in calls


def test_run_threads_directives_budget_and_gate_results_across_reentries():
    """Each re-entry must forward the accumulated gate-results list, the running
    LLM-budget count, and a ``PREVIOUS SPEC UNIMPLEMENTABLE`` directive."""
    seen: List[Dict[str, Any]] = []

    def _attempt(args):
        params = args[0]
        seen.append(
            {
                "budget_calls": params["budget_calls"],
                "gate_results": list(params["gate_results"]),
                "directives": list(params["directives"]),
                "phase_back_count": params["phase_back_count"],
            }
        )
        # Return escalating budget + one more gate row each attempt.
        n = len(seen)
        if n <= 2:
            return _reentry_outcome(
                evidence=f"fail-{n}",
                budget_calls=10 * n,
                gate_results=[{"gate_name": f"g{i}"} for i in range(n)],
            )
        return _record_outcome(budget_calls=10 * n, gate_results=[{"gate_name": "final"}])

    handlers = {
        "resolve_workflow_config_activity": lambda a: _WF_CONFIG,
        "run_design_attempt_activity": _attempt,
    }
    with _patch_execute(handlers):
        _run(
            {
                "prior_records": [],
                "config": _config_dict(),
                "convergence_tracker_state": {},
            }
        )
    # First attempt starts clean.
    assert seen[0]["budget_calls"] == 0
    assert seen[0]["gate_results"] == []
    assert seen[0]["directives"] == []
    assert seen[0]["phase_back_count"] == 0
    # Second attempt sees the first attempt's budget spend + gate rows + directive.
    assert seen[1]["budget_calls"] == 10
    assert seen[1]["gate_results"] == [{"gate_name": "g0"}]
    assert seen[1]["directives"] == ["PREVIOUS SPEC UNIMPLEMENTABLE: fail-1"]
    assert seen[1]["phase_back_count"] == 1
    # Third attempt accumulates both prior directives and the wider gate list.
    assert seen[2]["budget_calls"] == 20
    assert seen[2]["directives"] == [
        "PREVIOUS SPEC UNIMPLEMENTABLE: fail-1",
        "PREVIOUS SPEC UNIMPLEMENTABLE: fail-2",
    ]
    assert seen[2]["phase_back_count"] == 2


def test_run_increments_trial_count_on_each_phase_back():
    """Every phase-back advances the DSR trial counter by one (run_cycle:1057)."""

    def _attempt(args):
        # Echo back the tracker state we were handed, unchanged, so the only
        # trial-count movement comes from the workflow's per-phase-back bump.
        return _reentry_outcome(convergence_tracker_state=args[0]["convergence_tracker_state"])

    handlers = {
        "resolve_workflow_config_activity": lambda a: _WF_CONFIG,
        "run_design_attempt_activity": _attempt,
        "build_short_circuit_record_activity": lambda a: {
            "record": {"trial_count": a[0]["convergence_tracker_state"].get("trial_count")},
            "convergence_tracker_state": a[0]["convergence_tracker_state"],
        },
    }
    with _patch_execute(handlers):
        result = _run(
            {
                "prior_records": [],
                "config": _config_dict(),
                "convergence_tracker_state": {"trial_count": 0},
            }
        )
    # 3 attempts -> 3 phase-backs -> trial_count advanced to 3.
    assert result["record"]["trial_count"] == 3


def test_run_merges_child_drift_into_short_circuit_record():
    def _attempt(args):
        return _reentry_outcome(
            drift={
                "spec_history": [{"before_hash": "a", "after_hash": "b"}],
                "code_history": [],
                "gate_timeline": [],
            }
        )

    captured: Dict[str, Any] = {}

    def _short_circuit(args):
        captured["drift"] = args[0]["drift_collector"]
        return {"record": {"lab_record_id": "sc"}, "convergence_tracker_state": {}}

    handlers = {
        "resolve_workflow_config_activity": lambda a: _WF_CONFIG,
        "run_design_attempt_activity": _attempt,
        "build_short_circuit_record_activity": _short_circuit,
    }
    with _patch_execute(handlers):
        _run(
            {
                "prior_records": [],
                "config": _config_dict(),
                "convergence_tracker_state": {},
            }
        )
    # Each of the 3 attempts contributed one spec_history row to the parent.
    assert len(captured["drift"]["spec_history"]) == 3


# ---------------------------------------------------------------------------
# Directive seeding from the batch-level tracker
# ---------------------------------------------------------------------------


def test_run_seeds_failure_directives_from_tracker_state():
    seen: Dict[str, Any] = {}

    def _attempt(args):
        seen["directives"] = list(args[0]["directives"])
        return _record_outcome()

    # A tracker with a gate failing >= 3 times yields a failure directive.
    tracker_state = {
        "window_size": 5,
        "max_history": 50,
        "signatures": [],
        "failure_modes": {"AcceptanceGate": 4},
        "asset_class_history": [],
        "trial_count": 0,
        "trial_count_at_snapshot": 0,
    }
    handlers = {"run_design_attempt_activity": _attempt}
    with _patch_execute(handlers):
        _run(
            {
                "prior_records": [],
                "config": _config_dict(),
                "convergence_tracker_state": tracker_state,
                "workflow_config": _WF_CONFIG,
            }
        )
    assert any("AcceptanceGate" in d for d in seen["directives"])


# ---------------------------------------------------------------------------
# Module-level exports
# ---------------------------------------------------------------------------


def test_module_exports_workflow_and_activities():
    from investment_team.strategy_lab.temporal import activities as act

    assert wf.WORKFLOWS == [wf.StrategyLabCycleWorkflow, wf.StrategyLabBatchWorkflow]
    assert wf.TASK_QUEUE == "strategy-lab-queue"
    assert wf.ACTIVITIES == act.ACTIVITIES


def test_default_fencing_generation_matches_run_state():
    """wf._DEFAULT_FENCING_GENERATION is duplicated (not imported) from
    run_state.DEFAULT_FENCING_GENERATION because this module runs inside the
    temporalio workflow sandbox, which can't tolerate run_state's module-level
    threading.Lock() side effect. Guard against the two silently drifting."""
    from investment_team.strategy_lab import run_state

    assert wf._DEFAULT_FENCING_GENERATION == run_state.DEFAULT_FENCING_GENERATION
