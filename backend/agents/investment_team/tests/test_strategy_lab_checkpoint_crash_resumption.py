"""Crash-resumption and LLM-budget-integrity tests for the Strategy Lab
design-attempt checkpoint (``ADR-012``,
``system_design/adr/ADR-012-strategy-lab-design-attempt-checkpoint-contract.md``).

``test_strategy_lab_temporal_activities.py`` already has extensive coverage
of ``persist_design_attempt_checkpoint`` / ``load_design_attempt_checkpoint``
/ ``delete_design_attempt_checkpoint`` in isolation, and of
``run_design_attempt_activity`` resuming off a checkpoint a test *hands it
directly* (``monkeypatch.setattr(act, "load_design_attempt_checkpoint",
lambda ...: checkpoint)``). None of those chain two independent activity
executions through a REAL persist -> crash -> real read round trip, which is
the actual production recovery path a genuine worker crash exercises: the
checkpoint written by a dying attempt is read back by an entirely separate,
later invocation of the same activity function. This module closes that gap
with two complementary styles:

- Direct-call tests (default, no Temporal server, run in every CI pass) call
  ``run_design_attempt_activity`` twice as plain Python, sharing one
  in-memory fake job-record store across both calls. The first call writes a
  real checkpoint via ``persist_design_attempt_checkpoint`` and then dies
  before returning -- raising a ``SystemExit`` (a ``BaseException``, not an
  ``Exception``) so it escapes every ``except`` clause in
  ``run_design_attempt_activity``, exactly as a killed worker process would:
  no activity-body cleanup/error-mapping code ever runs. The second call
  reads that checkpoint back via the real ``load_design_attempt_checkpoint``.

- One ``@pytest.mark.integration`` test drives a real, embedded
  ``temporalio.testing.WorkflowEnvironment`` + ``Worker`` + the genuine,
  unmodified ``run_design_attempt_activity``, and lets Temporal's own
  ``_ACTIVITY_RETRY`` policy redispatch the activity after a retryable
  failure -- per ``workflows.py``'s own comment on that policy, "a
  Temporal-level retry only recovers a genuine worker crash mid-activity" --
  so the resume-from-checkpoint path is exercised by production retry
  scheduling, not by a test calling the activity function twice itself.
  Mirrors ``test_strategy_lab_temporal_cancellation.py``'s harness (the only
  other real-``WorkflowEnvironment`` test for Strategy Lab).

Both styles assert the same two outcomes ``ADR-012`` requires on resume: the
design phase is not re-run (``resume_spec``/``resume_rationale``/
``resume_design_context`` are threaded from the checkpoint, and a
phase-1-only counter never increments a second time), and the LLM-call
budget is not double-charged (the resumed attempt starts from exactly the
checkpoint's boundary-time ``budget_calls``, never from zero -- which would
silently reopen already-spent headroom -- and never re-charged for Phase 1 a
second time).
"""

from __future__ import annotations

import concurrent.futures
from typing import Any, Dict
from unittest import mock

import pytest

from investment_team.strategy_lab.temporal import activities as act
from shared.temporal.testing import workflow_environment as _workflow_environment

# ---------------------------------------------------------------------------
# Shared fixture builders
# ---------------------------------------------------------------------------


def _spec_dict(**overrides: Any) -> Dict[str, Any]:
    base = {
        "strategy_id": "strat-crash-1",
        "authored_by": "DesignAgent",
        "asset_class": "stocks",
        "hypothesis": "test hypothesis",
        "signal_definition": "test signal",
        "timeframe": "1d",
    }
    base.update(overrides)
    return base


def _backtest_config_dict(**overrides: Any) -> Dict[str, Any]:
    base = {"start_date": "2023-01-01", "end_date": "2023-12-31"}
    base.update(overrides)
    return base


def _run_design_attempt_params(**overrides: Any) -> Dict[str, Any]:
    base = {
        "prior_records": [],
        "config": _backtest_config_dict(),
        "signal_brief": None,
        "exclude_asset_classes": None,
        "directives": [],
        "design_attempt": 0,
        "phase_back_count": 0,
        "drift": {"spec_history": [], "code_history": [], "gate_timeline": []},
        "gate_results": [],
        "budget_calls": 0,
        "regime_summary": None,
        "convergence_tracker_state": {},
    }
    base.update(overrides)
    return base


class _FakeRecord:
    def model_dump(self, *, mode: str = "python") -> Dict[str, Any]:
        return {"lab_record_id": "rec-resumed"}


def _fake_job_store():
    """An in-memory stand-in for the durable job-record store, backing a REAL
    ``persist_design_attempt_checkpoint`` / ``load_design_attempt_checkpoint``
    round trip. Mirrors ``_persist_run_state``'s field-level partial-merge
    semantics (only the written keys are replaced; every other field, and any
    previously-written checkpoint field for a different cycle, is untouched).

    Postconditions:
        Returns ``(store, persist, load, generation)``: ``store`` is the
        backing dict a test can inspect directly (e.g. to assert a checkpoint
        was cleaned up); ``persist``/``load``/``generation`` are callables
        shaped like ``orchestrator_api._persist_run_state`` /
        ``run_state.load_run_from_job_service`` /
        ``run_state.get_run_generation_strict`` respectively. The fixed
        generation (1) never changes across calls -- no restart happens in
        any scenario this module tests, only a same-incarnation retry.
    """
    store: Dict[str, Any] = {}

    def _persist(run_id: str, state: dict, *, create: bool = False) -> None:
        store.update(state)

    def _load(run_id: str):
        return dict(store) if store else None

    def _generation(run_id: str) -> int:
        return 1

    return store, _persist, _load, _generation


def _patch_fake_store(monkeypatch):
    """Wire ``_fake_job_store()`` into the real checkpoint persist/read/fencing
    path, and pin ``cycle_scope`` to a fixed value -- mirroring the same
    Temporal ``workflow_id`` a real retry of the *same* activity task keeps
    across attempts. Returns the backing store dict."""
    from investment_team.strategy_lab import orchestrator_api, run_state

    store, fake_persist, fake_load, fake_generation = _fake_job_store()
    monkeypatch.setattr(orchestrator_api, "_persist_run_state", fake_persist)
    monkeypatch.setattr(run_state, "load_run_from_job_service", fake_load)
    monkeypatch.setattr(run_state, "get_run_generation_strict", fake_generation)
    monkeypatch.setattr(act, "_infer_cycle_scope_from_activity_context", lambda: "run-crash-c0")
    return store


# ---------------------------------------------------------------------------
# Direct-call: crash after checkpoint write, resume skips the design phase
# ---------------------------------------------------------------------------


def test_run_design_attempt_activity_resumes_after_crash_and_skips_design_phase(monkeypatch):
    """First execution reaches the design/synthesis boundary, durably writes a
    checkpoint, then dies before returning -- raising ``SystemExit`` (a
    ``BaseException``, not caught by any of the activity's own ``except``
    clauses: ``_DesignAttemptCancelled``, ``SpecImplementabilityError``,
    ``HTTPException``, bare ``Exception``), exactly as a killed worker
    process would -- no cleanup/error-mapping code in the activity body ever
    gets a chance to run, so the checkpoint survives untouched. A second,
    independent execution -- the shape of a Temporal-level retry redispatching
    the same activity input after detecting the dead worker -- must find that
    real, durably-persisted checkpoint and skip re-running the design phase
    rather than repeating it."""
    from investment_team.models import StrategySpec
    from investment_team.strategy_lab._orchestrator_helpers import _DesignPersistContext
    from investment_team.strategy_lab.agents._llm_budget import active_budget
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    _patch_fake_store(monkeypatch)

    checkpointed_spec = StrategySpec.parse_persisted(_spec_dict(hypothesis="phase 1 output"))
    checkpointed_context = _DesignPersistContext(
        rounds=2, critiques=[], stop_reason="converged", loop_telemetry={}
    )

    phase1_runs = 0
    resume_seen: Dict[str, Any] = {}

    def _crash_then_resume(self, **kwargs):
        nonlocal phase1_runs
        if kwargs["resume_spec"] is None:
            phase1_runs += 1
            active_budget().charge()
            active_budget().charge()
            active_budget().charge()
            kwargs["checkpoint_hook"](
                "design_synthesis_boundary",
                {
                    "spec": checkpointed_spec,
                    "rationale": "phase 1 rationale",
                    "design_context": checkpointed_context,
                },
            )
            # Simulate the worker process dying right here, mid-attempt.
            raise SystemExit("simulated worker crash")
        # Resumed execution: capture what it was handed, and never redo the
        # design phase (phase1_runs must not increment again).
        resume_seen["resume_spec"] = kwargs["resume_spec"]
        resume_seen["resume_rationale"] = kwargs["resume_rationale"]
        resume_seen["resume_design_context"] = kwargs["resume_design_context"]
        return _FakeRecord()

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _crash_then_resume)

    params = _run_design_attempt_params(run_id="run-crash", generation=1, design_attempt=0)

    with pytest.raises(SystemExit):
        act.run_design_attempt_activity(params)

    assert phase1_runs == 1

    # Second, independent call -- same run/cycle/attempt/generation, exactly
    # what a Temporal-level retry redispatches after the crash.
    out = act.run_design_attempt_activity(params)

    assert phase1_runs == 1, "design phase re-ran on resume instead of being skipped"
    assert resume_seen["resume_spec"] == checkpointed_spec
    assert resume_seen["resume_rationale"] == "phase 1 rationale"
    assert resume_seen["resume_design_context"].rounds == 2
    assert out["kind"] == "record"


# ---------------------------------------------------------------------------
# Direct-call: crash after checkpoint write, resume does not double-charge budget
# ---------------------------------------------------------------------------


def test_run_design_attempt_activity_resumes_after_crash_does_not_double_charge_budget(
    monkeypatch,
):
    """Same crash-then-redispatch shape as the design-phase test above, but the
    assertion here is purely about the LLM-call budget: the resumed attempt
    must start from exactly the checkpoint's boundary-time ``budget_calls``
    (the real Phase-1 charges the crashed attempt made) -- never from zero
    (undercounting, which would silently reopen already-spent headroom) and
    never re-charged for Phase 1 a second time (overcounting -- the actual
    double-charge bug this test guards against). Also confirms the checkpoint
    is cleaned up once the resumed attempt reaches its own terminal outcome,
    closing the full write -> crash -> resume -> cleanup lifecycle."""
    from investment_team.models import StrategySpec
    from investment_team.strategy_lab._orchestrator_helpers import _DesignPersistContext
    from investment_team.strategy_lab.agents._llm_budget import active_budget
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    store = _patch_fake_store(monkeypatch)

    checkpointed_spec = StrategySpec.parse_persisted(_spec_dict())
    checkpointed_context = _DesignPersistContext(
        rounds=1, critiques=[], stop_reason="converged", loop_telemetry={}
    )

    phase1_calls = 4
    phase2_calls = 3
    snapshots: Dict[str, int] = {}

    def _crash_then_resume(self, **kwargs):
        if kwargs["resume_spec"] is None:
            for _ in range(phase1_calls):
                active_budget().charge()
            snapshots["after_phase1"] = active_budget().calls_made
            kwargs["checkpoint_hook"](
                "design_synthesis_boundary",
                {
                    "spec": checkpointed_spec,
                    "rationale": "r",
                    "design_context": checkpointed_context,
                },
            )
            raise SystemExit("simulated worker crash")
        snapshots["seen_on_resume"] = active_budget().calls_made
        for _ in range(phase2_calls):
            active_budget().charge()
        snapshots["after_phase2"] = active_budget().calls_made
        return _FakeRecord()

    monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", _crash_then_resume)

    # The pre-crash execution is dispatched with no prior budget spend at all
    # (design_attempt 0, first ever attempt of this cycle).
    params = _run_design_attempt_params(run_id="run-crash-budget", generation=1, budget_calls=0)

    with pytest.raises(SystemExit):
        act.run_design_attempt_activity(params)

    assert snapshots["after_phase1"] == phase1_calls

    # A genuine Temporal retry replays the SAME params verbatim -- budget_calls
    # is still 0 here (the pre-crash attempt's own starting point), not
    # anything about the checkpoint. If the activity seeded the budget from
    # params instead of the checkpoint, this would silently reopen
    # phase1_calls worth of already-spent headroom.
    out = act.run_design_attempt_activity(params)

    assert snapshots["seen_on_resume"] == phase1_calls, (
        "resumed attempt did not start from the checkpoint's budget -- "
        "either under-counted (reopened spent headroom) or lost the charge"
    )
    assert snapshots["after_phase2"] == phase1_calls + phase2_calls, (
        "resumed attempt's budget diverged from phase1 + phase2 -- Phase 1 "
        "was double-charged (or under-charged) across the crash/resume cycle"
    )
    assert out["budget_calls"] == phase1_calls + phase2_calls

    # Full lifecycle: the resumed attempt's own successful completion cleans
    # up the checkpoint it resumed from.
    assert store.get(f"{act._DESIGN_ATTEMPT_CHECKPOINT_FIELD_PREFIX}run-crash-c0") is None


# ---------------------------------------------------------------------------
# Integration: real Temporal retry redispatches after a simulated crash
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_design_attempt_activity_resumes_from_checkpoint_after_temporal_retry(
    monkeypatch,
) -> None:
    """A real Temporal-level retry (``workflows.py``'s ``_ACTIVITY_RETRY`` --
    "a Temporal-level retry only recovers a genuine worker crash
    mid-activity", per that module's own comment) redispatches
    ``run_design_attempt_activity`` after a retryable failure. The
    redispatched execution must resume from the checkpoint the first, crashed
    execution wrote, skip re-running the design phase, and not double-charge
    the LLM-call budget.

    Drives the real, unmodified ``run_design_attempt_activity`` and
    ``StrategyLabCycleWorkflow`` against a real (embedded, time-skipping)
    Temporal test server -- production retry scheduling decides when the
    second execution happens, not the test calling the activity twice
    itself. Only ``StrategyLabOrchestrator._run_design_attempt`` is stubbed
    (the expensive design/synthesis pipeline); the durable job-record store
    the checkpoint reads/writes go through is the same in-memory fake the
    direct-call tests above use, so this test needs no live job service.
    """
    from temporalio.worker import Worker

    from investment_team.models import StrategySpec
    from investment_team.strategy_lab import orchestrator_api, run_state
    from investment_team.strategy_lab._orchestrator_helpers import _DesignPersistContext
    from investment_team.strategy_lab.agents._llm_budget import active_budget
    from investment_team.strategy_lab.exceptions import StrategyLabLLMError
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator
    from investment_team.strategy_lab.temporal.workflows import (
        TASK_QUEUE,
        StrategyLabCycleWorkflow,
    )
    from shared.temporal.worker import _build_workflow_runner

    _, fake_persist, fake_load, fake_generation = _fake_job_store()
    monkeypatch.setattr(orchestrator_api, "_persist_run_state", fake_persist)
    monkeypatch.setattr(run_state, "load_run_from_job_service", fake_load)
    monkeypatch.setattr(run_state, "get_run_generation_strict", fake_generation)

    checkpointed_spec = StrategySpec.parse_persisted(_spec_dict())
    checkpointed_context = _DesignPersistContext(
        rounds=1, critiques=[], stop_reason="converged", loop_telemetry={}
    )
    phase1_calls = 2
    phase2_calls = 1
    state: Dict[str, Any] = {"phase1_runs": 0, "budget_seen": []}

    def _fake_run_design_attempt(self: Any, **kwargs: Any) -> Any:
        if kwargs["resume_spec"] is None:
            state["phase1_runs"] += 1
            for _ in range(phase1_calls):
                active_budget().charge()
            state["budget_seen"].append(("phase1", active_budget().calls_made))
            kwargs["checkpoint_hook"](
                "design_synthesis_boundary",
                {
                    "spec": checkpointed_spec,
                    "rationale": "r",
                    "design_context": checkpointed_context,
                },
            )
            # Retryable outcome (not "fatal") -- workflows.py's own comment
            # documents this Temporal-level retry as the mechanism that
            # recovers a genuine worker crash mid-activity.
            raise StrategyLabLLMError("simulated worker crash mid-attempt", outcome="exhausted")
        for _ in range(phase2_calls):
            active_budget().charge()
        state["budget_seen"].append(("resume", active_budget().calls_made))
        return _FakeRecord()

    cycle_input = {
        "prior_records": [],
        "config": _backtest_config_dict(),
        "signal_brief": None,
        "exclude_asset_classes": None,
        "convergence_tracker_state": {},
        "workflow_config": {"regime_summary_enabled": False, "max_design_reentries": 0},
        "run_id": "run-crash-integration",
        "generation": 1,
    }

    async with _workflow_environment() as env:
        with (
            concurrent.futures.ThreadPoolExecutor(max_workers=2) as activity_executor,
            mock.patch.object(
                StrategyLabOrchestrator, "_run_design_attempt", _fake_run_design_attempt
            ),
        ):
            worker = Worker(
                env.client,
                task_queue=TASK_QUEUE,
                workflows=[StrategyLabCycleWorkflow],
                activities=[act.run_design_attempt_activity],
                activity_executor=activity_executor,
                max_cached_workflows=0,
                # See test_strategy_lab_temporal_cancellation.py's identical
                # worker construction for why this is required: without it,
                # validating StrategyLabCycleWorkflow re-imports
                # investment_team's numpy/pandas transitive chain inside the
                # sandbox's isolated namespace, crashing numpy's C extension.
                workflow_runner=_build_workflow_runner(),
            )
            async with worker:
                handle = await env.client.start_workflow(
                    StrategyLabCycleWorkflow.run,
                    cycle_input,
                    id="strategy-lab-crash-resumption-test",
                    task_queue=TASK_QUEUE,
                )
                result = await handle.result()

    assert state["phase1_runs"] == 1, "design phase re-ran after the simulated crash"
    assert state["budget_seen"] == [
        ("phase1", phase1_calls),
        ("resume", phase1_calls + phase2_calls),
    ], "LLM-call budget was double-charged (or lost) across the crash/resume cycle"
    assert result["record"]["lab_record_id"] == "rec-resumed"
