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
(``agent_team_studio/agentic_team_provisioning/tests/test_temporal_activity.py``): monkeypatch
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
from investment_team.tests.strategy_lab_temporal_fixtures import (
    WF_CONFIG as _WF_CONFIG,
)
from investment_team.tests.strategy_lab_temporal_fixtures import (
    config_dict as _config_dict,
)
from investment_team.tests.strategy_lab_temporal_fixtures import (
    record_outcome as _record_outcome,
)
from investment_team.tests.strategy_lab_temporal_fixtures import (
    reentry_outcome as _reentry_outcome,
)


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


def test_run_forwards_batch_cache_key_to_the_attempt_activity():
    """The cycle workflow threads the parent's ``batch_cache_key`` into every
    ``run_design_attempt_activity`` call so the worker can resolve the batch's
    shared cache."""
    seen: Dict[str, Any] = {}

    def _attempt(args):
        seen["batch_cache_key"] = args[0].get("batch_cache_key")
        return _record_outcome()

    handlers = {"run_design_attempt_activity": _attempt}
    with _patch_execute(handlers):
        _run(
            {
                "prior_records": [],
                "config": _config_dict(),
                "convergence_tracker_state": {},
                "workflow_config": _WF_CONFIG,
                "batch_cache_key": "run-1-b3",
            }
        )
    assert seen["batch_cache_key"] == "run-1-b3"


def test_run_tolerates_missing_batch_cache_key():
    """Old-shaped/resumed cycle inputs predating the key still run; the forwarded
    value is simply ``None``."""
    seen: Dict[str, Any] = {}

    def _attempt(args):
        seen["batch_cache_key"] = args[0].get("batch_cache_key", "MISSING")
        return _record_outcome()

    handlers = {"run_design_attempt_activity": _attempt}
    with _patch_execute(handlers):
        _run(
            {
                "prior_records": [],
                "config": _config_dict(),
                "convergence_tracker_state": {},
                "workflow_config": _WF_CONFIG,
            }
        )
    assert seen["batch_cache_key"] is None


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


def test_run_threads_run_id_and_generation_into_design_attempt_params():
    """``cycle_input``'s ``run_id``/``generation`` (ADR-012) must reach
    ``run_design_attempt_activity``'s params verbatim -- this is what lets
    that activity look up and write a design-attempt checkpoint at all."""
    seen: Dict[str, Any] = {}

    def _attempt(args):
        seen["run_id"] = args[0]["run_id"]
        seen["generation"] = args[0]["generation"]
        return _record_outcome()

    handlers = {
        "resolve_workflow_config_activity": lambda a: _WF_CONFIG,
        "run_design_attempt_activity": _attempt,
    }
    with _patch_execute(handlers):
        _run(
            {
                "run_id": "run-1",
                "generation": 3,
                "prior_records": [],
                "config": _config_dict(),
                "convergence_tracker_state": {},
            }
        )
    assert seen["run_id"] == "run-1"
    assert seen["generation"] == 3


def test_run_defaults_run_id_and_generation_when_cycle_input_predates_them():
    """A ``cycle_input`` from a workflow-history replay predating these
    fields (pre-ADR-012) must still run -- run_id defaults to None
    (disabling checkpointing) and generation defaults to
    ``_DEFAULT_FENCING_GENERATION``."""
    seen: Dict[str, Any] = {}

    def _attempt(args):
        seen["run_id"] = args[0]["run_id"]
        seen["generation"] = args[0]["generation"]
        return _record_outcome()

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
    assert seen["run_id"] is None
    assert seen["generation"] == wf._DEFAULT_FENCING_GENERATION


def test_run_threads_cycle_index_into_design_attempt_params():
    """``cycle_input``'s ``cycle_index`` must reach every
    ``run_design_attempt_activity`` call's params verbatim -- this is what lets
    that activity's progress-publish checkpoint attach the right
    ``StrategyLabProgressEvent.cycle_index`` (a required field on the
    frontend)."""
    seen: List[Any] = []

    def _attempt(args):
        seen.append(args[0]["cycle_index"])
        return _record_outcome()

    handlers = {
        "resolve_workflow_config_activity": lambda a: _WF_CONFIG,
        "run_design_attempt_activity": _attempt,
    }
    with _patch_execute(handlers):
        _run(
            {
                "run_id": "run-1",
                "cycle_index": 4,
                "prior_records": [],
                "config": _config_dict(),
                "convergence_tracker_state": {},
            }
        )
    assert seen == [4]


def test_run_defaults_cycle_index_to_none_when_cycle_input_predates_it():
    """A ``cycle_input`` from a workflow-history replay predating this field
    must still run -- ``cycle_index`` defaults to ``None`` (disabling live
    progress publishing for the attempt, never a crash)."""
    seen: List[Any] = []

    def _attempt(args):
        seen.append(args[0]["cycle_index"])
        return _record_outcome()

    handlers = {
        "resolve_workflow_config_activity": lambda a: _WF_CONFIG,
        "run_design_attempt_activity": _attempt,
    }
    with _patch_execute(handlers):
        _run(
            {
                "run_id": "run-1",
                "prior_records": [],
                "config": _config_dict(),
                "convergence_tracker_state": {},
            }
        )
    assert seen == [None]


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
    """Every phase-back advances the DSR trial counter by one, per run_cycle's re-entry handling."""

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
    """Drift collected across the reentry attempts (spec/code history, gate
    timeline) is merged into the drift_collector passed to the
    short-circuit record builder, not dropped when the run short-circuits."""

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
    """A convergence tracker whose failure_modes shows a gate failing at or
    above the seeding threshold (here AcceptanceGate x4) yields a failure
    directive naming that gate in the first design attempt's directives."""
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
    """The temporal workflows module exports the expected workflow classes,
    task queue name, and activity list -- the Pattern-A contract every
    other team's temporal package registration depends on."""
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


def test_signal_brief_activity_timeout_scales_with_configured_llm_timeout():
    """The signal-brief activity's start_to_close deadline must grow with an
    operator's LLM_TIMEOUT override, not stay pinned to a fixed constant --
    otherwise a large override could make five serial per-category calls
    exceed a fixed ceiling even though each stays within its own deadline,
    forcing Temporal to retry the whole activity and repeat paid LLM calls."""
    from datetime import timedelta

    default_timeout = wf._signal_brief_activity_timeout(3600.0, None, 10, 0.0)
    doubled_timeout = wf._signal_brief_activity_timeout(7200.0, None, 10, 0.0)
    assert doubled_timeout == default_timeout * 2

    # An excluded category shrinks the allowed count, so the deadline shrinks too.
    stocks_only = wf._signal_brief_activity_timeout(
        3600.0, ["crypto", "forex", "futures", "commodities"], 10, 0.0
    )
    assert stocks_only == timedelta(seconds=1 * 3600.0 * 11)
    assert stocks_only < default_timeout


def test_signal_brief_activity_timeout_scales_with_configured_retry_ceiling():
    """The deadline must also grow with an operator's LLM_MAX_RETRIES
    override -- a legitimately-retrying (not stalled) call, which the
    heartbeat mechanism does not protect against, must not outlast a safety
    margin sized independently of the client's own retry budget."""
    from datetime import timedelta

    default_retries = wf._signal_brief_activity_timeout(3600.0, None, 10, 0.0)
    more_retries = wf._signal_brief_activity_timeout(3600.0, None, 20, 0.0)
    assert more_retries == timedelta(seconds=5 * 3600.0 * 21)
    assert more_retries > default_retries

    zero_retries = wf._signal_brief_activity_timeout(3600.0, None, 0, 0.0)
    assert zero_retries == timedelta(seconds=5 * 3600.0 * 1)


def test_signal_brief_activity_timeout_includes_retry_backoff_sleep_time():
    """A start_to_close deadline sized only for attempts' own durations can
    still expire mid-backoff during a valid retry sequence -- the client
    sleeps up to llm_backoff_cap_s after each failed attempt, and that sleep
    time is real wall-clock the activity spends without heartbeats protecting
    against a start_to_close cutoff. The deadline must add
    llm_max_retries * llm_backoff_cap_s per category on top of the
    attempts-only total."""
    from datetime import timedelta

    no_backoff = wf._signal_brief_activity_timeout(3600.0, None, 10, 0.0)
    with_backoff = wf._signal_brief_activity_timeout(3600.0, None, 10, 120.0)
    assert with_backoff == no_backoff + timedelta(seconds=5 * 10 * 120.0)
    assert with_backoff > no_backoff

    # Zero retries means zero backoff sleeps, regardless of the configured cap.
    zero_retries = wf._signal_brief_activity_timeout(3600.0, None, 0, 120.0)
    assert zero_retries == timedelta(seconds=5 * 3600.0 * 1)


def test_signal_brief_activity_timeout_never_degenerates_to_a_non_positive_deadline():
    """An all-excluded (or malformed, over-long) exclude list must still clamp
    to at least one allowed category rather than yielding a zero/negative
    start_to_close_timeout, which Temporal would reject outright."""
    from datetime import timedelta

    result = wf._signal_brief_activity_timeout(
        3600.0, ["stocks", "crypto", "forex", "futures", "commodities"], 10, 0.0
    )
    assert result == timedelta(seconds=1 * 3600.0 * 11)
    assert result > timedelta(0)


def test_signal_brief_activity_timeout_deduplicates_the_exclude_list():
    """A duplicate-laden exclude_asset_classes (e.g. a caller-side bug
    repeating an entry) must not inflate the excluded count past the true
    number of distinct categories excluded -- an inflated excluded count
    undersizes allowed_count, and therefore the deadline, which is the
    dangerous direction for this helper to get wrong."""
    from datetime import timedelta

    deduped = wf._signal_brief_activity_timeout(3600.0, ["stocks", "stocks", "crypto"], 10, 0.0)
    distinct = wf._signal_brief_activity_timeout(3600.0, ["stocks", "crypto"], 10, 0.0)
    assert deduped == distinct
    assert deduped == timedelta(seconds=3 * 3600.0 * 11)


# ---------------------------------------------------------------------------
# Checkpoint-lookup and resume-point determination (issue #7312, first step
# of #7282 — Temporal-mode parity with thread mode's #7309/#7315). The
# workflow computes ``resume_stage_determinations`` per re-entry but does not
# yet act on it (that's #7318) -- surfaced only on the "record" and
# short-circuit return dicts for test observability.
# ---------------------------------------------------------------------------


def _checkpoint_json(
    checkpoint_cls, *, run_id: str = "run-1", generation: int = 1, **overrides: Any
) -> Dict[str, Any]:
    """Build one real ``PipelineCheckpoint`` subclass instance -- with the
    identity fields matching this file's own ``run_id``/``generation``
    convention -- and return its wire (``model_dump(mode="json")``) form,
    exactly as ``activities.py``'s ``_pipeline_checkpoints_to_wire`` produces
    it. Building from the real Pydantic classes (rather than hand-rolled
    dicts) means this fixture can't drift from the real wire shape.
    """
    from investment_team.models import StrategySpec
    from investment_team.strategy_lab import phases

    spec = StrategySpec(
        strategy_id="strat-1",
        authored_by="DesignAgent",
        asset_class="stocks",
        hypothesis="test hypothesis",
        signal_definition="test signal",
        timeframe="1d",
    )
    code = overrides.pop("code", "def run(): pass")
    base: Dict[str, Any] = {
        "run_id": run_id,
        "cycle_scope": "cycle-scope-1",
        "design_attempt": 0,
        "generation": generation,
        "spec_hash": phases.hash_spec(spec),
        "code_hash": phases.hash_code(code)
        if "code" in checkpoint_cls.model_fields
        else phases.hash_code(None),
        "captured_at": "2026-08-27T00:00:00Z",
        "budget_calls": 5,
        "gate_results": [],
        "spec": spec,
        "rationale": "because",
    }
    if "code" in checkpoint_cls.model_fields:
        base["code"] = code
    base.update(overrides)
    return checkpoint_cls(**base).model_dump(mode="json")


def _run_with_reentry_then_record(pipeline_checkpoints: List[Dict[str, Any]]) -> Dict[str, Any]:
    handlers = {
        "resolve_workflow_config_activity": lambda a: _WF_CONFIG,
        "run_design_attempt_activity": mock.Mock(
            side_effect=[
                _reentry_outcome(pipeline_checkpoints=pipeline_checkpoints),
                _record_outcome(),
            ]
        ),
    }
    with _patch_execute(handlers):
        return _run(
            {
                "prior_records": [],
                "config": _config_dict(),
                "convergence_tracker_state": {},
                "run_id": "run-1",
                "generation": 1,
            }
        )


def test_resume_determination_after_design_checkpoint_targets_review():
    from investment_team.strategy_lab.checkpoints import DesignCheckpoint

    result = _run_with_reentry_then_record([_checkpoint_json(DesignCheckpoint)])
    assert result["resume_stage_determinations"] == ["review"]


def test_resume_determination_after_review_checkpoint_targets_synthesis():
    from investment_team.strategy_lab.checkpoints import ReviewCheckpoint

    result = _run_with_reentry_then_record(
        [_checkpoint_json(ReviewCheckpoint, review_rounds_completed=2)]
    )
    assert result["resume_stage_determinations"] == ["synthesis"]


def test_resume_determination_after_synthesis_checkpoint_targets_refinement():
    from investment_team.strategy_lab.checkpoints import SynthesisCheckpoint

    result = _run_with_reentry_then_record([_checkpoint_json(SynthesisCheckpoint)])
    assert result["resume_stage_determinations"] == ["refinement"]


def test_resume_determination_after_refinement_checkpoint_targets_alignment():
    from investment_team.strategy_lab.checkpoints import RefinementCheckpoint

    result = _run_with_reentry_then_record(
        [_checkpoint_json(RefinementCheckpoint, refinement_rounds_completed=1)]
    )
    assert result["resume_stage_determinations"] == ["alignment"]


def test_resume_determination_after_alignment_checkpoint_is_none():
    """The last stage has nothing after it to resume into."""
    from investment_team.strategy_lab.checkpoints import AlignmentCheckpoint

    result = _run_with_reentry_then_record(
        [_checkpoint_json(AlignmentCheckpoint, alignment_rounds_completed=3)]
    )
    assert result["resume_stage_determinations"] == [None]


def test_resume_determination_is_none_when_no_checkpoint_exists():
    result = _run_with_reentry_then_record([])
    assert result["resume_stage_determinations"] == [None]


def test_resume_determination_computed_but_not_acted_on_across_reentries_to_short_circuit():
    """Every re-entry appends its own determination, in order, and nothing
    about the short-circuit path's existing fields changes: the workflow
    still fully re-runs every attempt from scratch (this step doesn't wire
    resume_spec/resume_design_context yet -- that's #7318)."""
    from investment_team.strategy_lab.checkpoints import ReviewCheckpoint

    def _attempt(a):
        # Each attempt's checkpoint must carry *that* attempt's own
        # design_attempt -- find_latest_checkpoint_for_attempt filters on it,
        # exactly matching how the real activity captures checkpoints against
        # whichever design_attempt it was actually invoked with.
        checkpoint = _checkpoint_json(
            ReviewCheckpoint, design_attempt=a[0]["design_attempt"], review_rounds_completed=1
        )
        return _reentry_outcome(pipeline_checkpoints=[checkpoint])

    handlers = {
        "resolve_workflow_config_activity": lambda a: _WF_CONFIG,
        "run_design_attempt_activity": _attempt,
        "build_short_circuit_record_activity": lambda a: {
            "record": {"lab_record_id": "sc-1", "status": a[0]["short_circuit_status"]},
            "convergence_tracker_state": a[0]["convergence_tracker_state"],
        },
    }
    with _patch_execute(handlers):
        result = _run(
            {
                "prior_records": [],
                "config": _config_dict(),
                "convergence_tracker_state": {},
                "run_id": "run-1",
                "generation": 1,
            }
        )
    # max_design_reentries=2 (WF_CONFIG) -> 3 attempts, each producing the
    # same "reentry" outcome -> 3 determinations, all "synthesis".
    assert result["resume_stage_determinations"] == ["synthesis", "synthesis", "synthesis"]
    assert result["record"]["status"] == "failed: spec_unimplementable"


# ---------------------------------------------------------------------------
# Cross-attempt resume consumption (issue #7318, second step of #7282 --
# Temporal-mode parity with thread mode's gated cross-attempt resume,
# #7315/PR #7469). The determination computed above is now *acted on*: the
# next attempt's activity params carry the checkpoint's state, but only when
# the raising exception declared ``spec_implicated=False`` AND the
# determination is ``PipelineStage.SYNTHESIS`` (a ``ReviewCheckpoint``).
# Every current production raise site still sets ``spec_implicated=True``
# (the ``reentry_outcome`` fixture default), so the common-path tests below
# lock in "no behavior change" and the ``spec_implicated=False`` tests stand
# in for a future raise site that has proven its failure doesn't implicate
# the checkpointed spec.
# ---------------------------------------------------------------------------


def _spec_revision(**overrides: Any) -> Dict[str, Any]:
    base = {
        "phase": "design",
        "agent": "DesignAgent",
        "timestamp": "2026-08-27T00:00:00Z",
        "before_hash": "a" * 64,
        "after_hash": "b" * 64,
        "diff": "--- before\n+++ after\n",
        "reason": "tightened entry threshold",
        "gate_failures": [],
    }
    base.update(overrides)
    return base


def _run_reentry_then_record_capturing_params(
    *, reentry_overrides: Dict[str, Any]
) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Drive one re-entry then a terminal record, capturing every
    ``run_design_attempt_activity`` call's ``params`` in order.
    """
    captured_params: List[Dict[str, Any]] = []

    def _run_design_attempt(args: tuple[Dict[str, Any]]) -> Dict[str, Any]:
        captured_params.append(args[0])
        if len(captured_params) == 1:
            return _reentry_outcome(**reentry_overrides)
        return _record_outcome()

    handlers = {
        "resolve_workflow_config_activity": lambda a: _WF_CONFIG,
        "run_design_attempt_activity": _run_design_attempt,
    }
    with _patch_execute(handlers):
        result = _run(
            {
                "prior_records": [],
                "config": _config_dict(),
                "convergence_tracker_state": {},
                "run_id": "run-1",
                "generation": 1,
            }
        )
    return result, captured_params


def test_spec_implicated_true_full_restart_carries_no_resume_state():
    """The common path (every current production raise site): a
    ``ReviewCheckpoint`` is present, but ``spec_implicated`` defaults to
    ``True`` -- the next attempt's params must carry no resume state and an
    empty drift seed, exactly matching today's full-restart behavior.
    """
    from investment_team.strategy_lab.checkpoints import ReviewCheckpoint

    checkpoint = _checkpoint_json(
        ReviewCheckpoint,
        review_rounds_completed=1,
        spec_history=[_spec_revision()],
        design_context={"rounds": 1, "critiques": [], "stop_reason": "ready", "loop_telemetry": {}},
    )
    result, captured_params = _run_reentry_then_record_capturing_params(
        reentry_overrides={"pipeline_checkpoints": [checkpoint]}
    )

    assert result["resume_stage_determinations"] == ["synthesis"]
    second_call = captured_params[1]
    assert second_call["resume_spec"] is None
    assert second_call["resume_rationale"] is None
    assert second_call["resume_design_context"] is None
    assert second_call["drift"] == {"spec_history": [], "code_history": [], "gate_timeline": []}
    assert second_call["directives"] == ["PREVIOUS SPEC UNIMPLEMENTABLE: always fails"]


def test_spec_implicated_false_resumes_from_review_checkpoint():
    """When the raising exception declares ``spec_implicated=False`` and the
    just-failed attempt's checkpoint converged through REVIEW, the next
    attempt's params carry the checkpoint's spec/rationale/design_context and
    a drift seeded with the checkpoint's own history -- and no misleading
    "SPEC UNIMPLEMENTABLE" directive is added.
    """
    from investment_team.strategy_lab.checkpoints import ReviewCheckpoint

    spec_revision = _spec_revision()
    checkpoint = _checkpoint_json(
        ReviewCheckpoint,
        review_rounds_completed=1,
        spec_history=[spec_revision],
        design_context={"rounds": 1, "critiques": [], "stop_reason": "ready", "loop_telemetry": {}},
    )
    result, captured_params = _run_reentry_then_record_capturing_params(
        reentry_overrides={"pipeline_checkpoints": [checkpoint], "spec_implicated": False}
    )

    assert result["resume_stage_determinations"] == ["synthesis"]
    second_call = captured_params[1]
    assert second_call["resume_spec"] == checkpoint["spec"]
    assert second_call["resume_rationale"] == checkpoint["rationale"]
    assert second_call["resume_design_context"] == checkpoint["design_context"]
    assert second_call["drift"]["spec_history"] == [spec_revision]
    assert second_call["directives"] == []


def test_spec_implicated_false_does_not_resume_past_review_checkpoint():
    """``spec_implicated=False`` alone isn't sufficient: a checkpoint that
    converged further than REVIEW (here, SYNTHESIS -- determination
    REFINEMENT) has no resume boundary at all, since resuming past code
    synthesis would need its own code-soundness signal that
    ``spec_implicated`` doesn't provide.
    """
    from investment_team.strategy_lab.checkpoints import SynthesisCheckpoint

    checkpoint = _checkpoint_json(SynthesisCheckpoint)
    result, captured_params = _run_reentry_then_record_capturing_params(
        reentry_overrides={"pipeline_checkpoints": [checkpoint], "spec_implicated": False}
    )

    assert result["resume_stage_determinations"] == ["refinement"]
    second_call = captured_params[1]
    assert second_call["resume_spec"] is None
    assert second_call["drift"] == {"spec_history": [], "code_history": [], "gate_timeline": []}


def test_spec_implicated_false_does_not_resume_when_no_checkpoint_exists():
    result, captured_params = _run_reentry_then_record_capturing_params(
        reentry_overrides={"pipeline_checkpoints": [], "spec_implicated": False}
    )

    assert result["resume_stage_determinations"] == [None]
    second_call = captured_params[1]
    assert second_call["resume_spec"] is None


def test_repeated_resume_does_not_duplicate_seeded_drift_history_in_short_circuit_record():
    """Codex-review-style regression guard (mirrors thread mode's
    ``test_repeated_resume_does_not_duplicate_seeded_drift_history``):
    merging a resumed attempt's *whole* drift -- including the checkpoint
    history seeded into it -- into the parent on every subsequent failure
    would duplicate that history once per resumed re-entry. Drives every
    attempt to fail not-spec-implicated with a REVIEW checkpoint carrying
    the *same* one spec revision each time; the short-circuit record's
    ``drift_collector`` must still show that revision exactly once.
    """
    from investment_team.strategy_lab.checkpoints import ReviewCheckpoint

    spec_revision = _spec_revision()
    captured_short_circuit_params: List[Dict[str, Any]] = []

    def _attempt(args: tuple[Dict[str, Any]]) -> Dict[str, Any]:
        checkpoint = _checkpoint_json(
            ReviewCheckpoint,
            design_attempt=args[0]["design_attempt"],
            review_rounds_completed=1,
            spec_history=[spec_revision],
            design_context={
                "rounds": 1,
                "critiques": [],
                "stop_reason": "ready",
                "loop_telemetry": {},
            },
        )
        # Simulates the real activity's own drift_collector: whether this
        # attempt derived the revision fresh (attempt 0, no seed) or resumed
        # past a checkpoint that already carried it (every later attempt,
        # seeded), its own accumulated child collector ends up containing
        # this exact one entry either way -- never duplicated by re-deriving
        # it on top of a seed.
        return _reentry_outcome(
            pipeline_checkpoints=[checkpoint],
            spec_implicated=False,
            drift={"spec_history": [spec_revision], "code_history": [], "gate_timeline": []},
        )

    def _short_circuit(args: tuple[Dict[str, Any]]) -> Dict[str, Any]:
        captured_short_circuit_params.append(args[0])
        return {
            "record": {"lab_record_id": "sc-1", "status": args[0]["short_circuit_status"]},
            "convergence_tracker_state": args[0]["convergence_tracker_state"],
        }

    handlers = {
        "resolve_workflow_config_activity": lambda a: _WF_CONFIG,
        "run_design_attempt_activity": _attempt,
        "build_short_circuit_record_activity": _short_circuit,
    }
    with _patch_execute(handlers):
        result = _run(
            {
                "prior_records": [],
                "config": _config_dict(),
                "convergence_tracker_state": {},
                "run_id": "run-1",
                "generation": 1,
            }
        )

    assert result["record"]["status"] == "failed: spec_unimplementable"
    drift_collector = captured_short_circuit_params[0]["drift_collector"]
    # Every attempt resumed past REVIEW using the same checkpoint's single
    # spec revision -- without the delta-only merge fix, this would grow by
    # one duplicate entry per resumed re-entry (3 attempts -> 3 entries).
    assert drift_collector["spec_history"] == [spec_revision]


def test_convergence_directive_omitted_for_non_spec_implicated_failure():
    """A ``spec_implicated=False`` failure's evidence isn't about the spec's
    soundness -- labeling it "SPEC UNIMPLEMENTABLE" would mislead a later
    full restart into needlessly revising a spec that was never at fault.
    """
    result, captured_params = _run_reentry_then_record_capturing_params(
        reentry_overrides={"pipeline_checkpoints": [], "spec_implicated": False}
    )
    assert captured_params[1]["directives"] == []
    assert result["record"] == {"lab_record_id": "rec-1"}
