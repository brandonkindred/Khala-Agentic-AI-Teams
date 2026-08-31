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

A third section below covers a distinct re-entry shape: thread mode's
cross-*attempt* resume (``StrategyLabOrchestrator.run_cycle`` re-entering
after a ``SpecImplementabilityError`` between design attempts within the
same cycle -- see ``checkpoints.py`` and ``exceptions.py``'s
``spec_implicated`` contract), as opposed to this file's Temporal same-
*attempt* crash recovery above. ``test_strategy_lab_cross_attempt_resume.py``
already proves the right pipeline stages are skipped/re-run on such a
resume via stub call counters; the tests below close the gap that leaves --
that skipping those stages actually bounds the cycle's real LLM-call cost to
the resumed portion of the pipeline, not the full pipeline again.
"""

from __future__ import annotations

import concurrent.futures
from typing import Any, Dict, List
from unittest import mock

import pytest

from investment_team.strategy_lab import orchestrator as orchestrator_module
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


# ---------------------------------------------------------------------------
# Integration: Temporal-mode cross-attempt re-entry LLM-call count is bounded
# ---------------------------------------------------------------------------
#
# The integration test above covers a same-*attempt* Temporal-level retry
# (a genuine worker crash mid-attempt). This test covers a distinct shape:
# the workflow's own cross-*attempt* re-entry loop
# (``StrategyLabCycleWorkflow.run``'s ``resolve_cross_attempt_resume`` gate
# in ``strategy_lab/temporal/workflows.py``), which re-dispatches a brand
# new ``design_attempt`` after ``SpecImplementabilityError``. It is the
# Temporal-mode analog of this file's own thread-mode section below
# (``test_cross_attempt_resume_llm_call_count_bounded_to_resumed_portion``):
# same claim -- a re-entry that resumes from a checkpoint which converged
# through REVIEW pays only for the resumed portion of the pipeline
# (synthesis onward), not DESIGN+REVIEW again -- proven here through the
# real workflow re-entry loop and the real, unmodified
# ``run_design_attempt_activity`` instead of a direct orchestrator call.


@pytest.mark.integration
@pytest.mark.asyncio
async def test_design_attempt_activity_cross_attempt_resume_bounds_llm_call_cost(
    monkeypatch,
) -> None:
    """Attempt 0 charges a known DESIGN+REVIEW cost, checkpoints at the
    design/synthesis boundary, then fails with
    ``SpecImplementabilityError(spec_implicated=False)``. The workflow's
    re-entry loop resolves that checkpoint as a usable cross-attempt resume
    (``determine_resume_stage`` is ``PipelineStage.SYNTHESIS``) and
    re-dispatches ``run_design_attempt_activity`` for attempt 1 with the
    checkpointed spec threaded in as ``resume_spec``. Attempt 1 must not
    re-pay the DESIGN+REVIEW cost -- only the (separately known) cost of the
    resumed portion.
    """
    from temporalio.worker import Worker

    from investment_team.models import StrategySpec
    from investment_team.strategy_lab import orchestrator_api, run_state
    from investment_team.strategy_lab._orchestrator_helpers import _DesignPersistContext
    from investment_team.strategy_lab.agents._llm_budget import active_budget
    from investment_team.strategy_lab.exceptions import SpecImplementabilityError
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
    phase1_calls = 3
    phase2_calls = 2
    state: Dict[str, Any] = {"budget_seen": []}

    def _fake_run_design_attempt(self: Any, **kwargs: Any) -> Any:
        if kwargs["resume_spec"] is None:
            for _ in range(phase1_calls):
                active_budget().charge()
            state["budget_seen"].append(("attempt0", active_budget().calls_made))
            kwargs["checkpoint_hook"](
                "design_synthesis_boundary",
                {
                    "spec": checkpointed_spec,
                    "rationale": "r",
                    "design_context": checkpointed_context,
                },
            )
            raise SpecImplementabilityError(
                "forced fail at synthesis boundary, not spec-implicated",
                failure_phase="synthesis",
                last_spec=checkpointed_spec,
                last_code="",
                spec_implicated=False,
            )
        assert kwargs["resume_spec"] == checkpointed_spec
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
        # Unlike the same-attempt crash test above, this needs at least one
        # re-entry to actually run (attempt 0 fails, attempt 1 resumes).
        "workflow_config": {"regime_summary_enabled": False, "max_design_reentries": 1},
        "run_id": "run-reentry-integration",
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
                    id="strategy-lab-reentry-resume-test",
                    task_queue=TASK_QUEUE,
                )
                result = await handle.result()

    assert result["resume_stage_determinations"] == ["synthesis"], (
        "cross-attempt resume was not activated -- attempt 1 did not resume from "
        "the attempt-0 checkpoint"
    )
    assert state["budget_seen"] == [
        ("attempt0", phase1_calls),
        ("resume", phase1_calls + phase2_calls),
    ], (
        "re-entry's LLM-call cost was not bounded to the resumed portion -- "
        "DESIGN+REVIEW was paid for again instead of being skipped"
    )
    assert result["record"]["lab_record_id"] == "rec-resumed"


# ---------------------------------------------------------------------------
# Thread mode: cross-attempt re-entry LLM-call count is bounded
# ---------------------------------------------------------------------------
#
# The design/review stubs below charge the same per-cycle LLMCallBudget the
# real agents charge (via ``charge_active_budget()``), mirroring
# ``test_strategy_lab_design_loop.py``'s ``_charging_run`` helper and this
# file's own ``active_budget().charge()`` stubs above -- there is no
# ``MockLLMClient`` anywhere in this suite, so a stub that charges is the
# established way to make an LLM round-trip's cost observable in a test.
# Everything downstream of design+review (synthesis, and -- on the no-
# market-data short circuit these tests reuse -- refinement/alignment) is
# ``CodeSynthesisAgent``/a structural no-op, so it is genuinely free
# (``_llm_budget.py``'s ``LLMCallBudget`` docstring: only ``CodeSynthesisAgent``
# and ``AnalysisAgent`` never charge); design+review is where the epic's
# "known number of refinement rounds" cost actually lives, and it is the
# only phase this test needs to bound.


def _config() -> Any:
    from investment_team.models import BacktestConfig

    return BacktestConfig(
        start_date="2023-01-01",
        end_date="2023-12-31",
        initial_capital=100_000.0,
        benchmark_symbol="SPY",
        transaction_cost_bps=5.0,
        slippage_bps=2.0,
    )


def _charging_design_stubs(monkeypatch, orch, *, review_not_ready_rounds: int) -> int:
    """Wire ``design_agent.run``/``revise`` and ``design_review_agent.run`` so
    each simulated call charges one unit of the active LLM budget, and the
    reviewer returns ``ready=False`` for ``review_not_ready_rounds`` rounds
    (a known number of design-refinement rounds) before converging on each
    design attempt -- so a fully re-run attempt pays the identical, known
    cost every time (the review-round counter resets on every ``run()``,
    i.e. at the start of each design attempt, mirroring one real design
    attempt's shape rather than accumulating across attempts).

    Postconditions:
        Returns the exact number of budget units one design attempt charges:
        one ``run()`` + one ``review()`` per round (``review_not_ready_rounds``
        not-ready rounds plus the final ready round) + one ``revise()`` per
        not-ready round.
    """
    from investment_team.strategy_lab.agents._llm_budget import charge_active_budget
    from investment_team.strategy_lab.agents.design_review import SpecCritique

    from .test_strategy_lab_phase_transitions import _spec_dict

    def _revised_spec_dict() -> Dict[str, Any]:
        revised = _spec_dict()
        revised["hypothesis"] = "revised hypothesis"
        return revised

    review_calls = {"n": 0}

    def _run(**_kw: Any) -> Any:
        review_calls["n"] = 0
        charge_active_budget()
        return _spec_dict(), "scripted rationale"

    def _review(*_a: Any, **_kw: Any) -> SpecCritique:
        charge_active_budget()
        review_calls["n"] += 1
        if review_calls["n"] <= review_not_ready_rounds:
            return SpecCritique(ready=False, rationale="tighten entry threshold")
        return SpecCritique(ready=True, rationale="ok")

    def _revise(*_a: Any, **_kw: Any) -> Any:
        charge_active_budget()
        return _revised_spec_dict(), "revised rationale"

    monkeypatch.setattr(orch.design_agent, "run", _run)
    monkeypatch.setattr(orch.design_review_agent, "run", _review)
    monkeypatch.setattr(orch.design_agent, "revise", _revise)

    return 1 + (review_not_ready_rounds + 1) + review_not_ready_rounds


def _capture_cycle_budget(monkeypatch) -> List[Any]:
    """Monkeypatch ``orchestrator_module.LLMCallBudget`` to a wrapper that
    still constructs the real class but stashes every instance ``run_cycle``
    creates, so the test can read ``.calls_made`` after the call returns.
    ``run_cycle`` never exposes the budget on its return value in thread
    mode (that field is a Temporal-activity-output concept, from
    ``run_design_attempt_activity``'s ``out["budget_calls"]`` above) -- one
    instance is created per cycle (``orchestrator.py``, bound for the whole
    attempt loop via ``use_budget``), so ``captured[0]`` is the whole
    cycle's real cost.
    """
    captured: List[Any] = []
    real_cls = orchestrator_module.LLMCallBudget

    def _capturing(*args: Any, **kwargs: Any) -> Any:
        budget = real_cls(*args, **kwargs)
        captured.append(budget)
        return budget

    monkeypatch.setattr(orchestrator_module, "LLMCallBudget", _capturing)
    return captured


@pytest.mark.strategy_lab_integration
def test_cross_attempt_resume_llm_call_count_bounded_to_resumed_portion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attempt 0 converges DESIGN+REVIEW after one real not-ready round, then
    fails at the synthesis boundary with ``spec_implicated=False``. Attempt 1
    resumes past DESIGN+REVIEW (``last_resume_determination is
    PipelineStage.SYNTHESIS`` -- the one resume shape ``run_cycle`` consumes
    today, per #7315/PR #7469) straight into synthesis, which is free. The
    cycle's *total* LLM-call cost must equal exactly one design attempt's
    worth of charges -- proving design+review's cost was not paid again on
    re-entry, i.e. the re-entry's cost is bounded to the resumed portion of
    the pipeline rather than the full pipeline.
    """
    from investment_team.strategy_lab.checkpoints import PipelineStage
    from investment_team.strategy_lab.exceptions import SpecImplementabilityError
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    from .test_strategy_lab_phase_transitions import _stub_pipeline_for_happy_path

    # Pin both env-derived limits rather than rely on their (generous, but
    # environment-overridable) defaults: this fixture needs >= 2 review
    # rounds per attempt and, on the full-restart contrast test, >= 2x this
    # attempt's charges -- a smaller configured limit would exhaust the
    # budget or cap review rounds before the forced failure, for reasons
    # unrelated to re-entry (Codex review finding).
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_ROUNDS", "5")
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_MAX_LLM_CALLS", "20")

    orch = StrategyLabOrchestrator()
    _stub_pipeline_for_happy_path(monkeypatch, orch)
    phase1_calls = _charging_design_stubs(monkeypatch, orch, review_not_ready_rounds=1)
    captured_budgets = _capture_cycle_budget(monkeypatch)

    real_synthesize = orch._synthesize_initial_code

    def _fail_first_attempt_not_spec_implicated(**kwargs: Any) -> Any:
        if kwargs["design_attempt"] == 0:
            raise SpecImplementabilityError(
                "forced fail at synthesis boundary, not spec-implicated",
                failure_phase="synthesis",
                last_spec=kwargs["spec"],
                last_code="",
                spec_implicated=False,
            )
        return real_synthesize(**kwargs)

    monkeypatch.setattr(orch, "_synthesize_initial_code", _fail_first_attempt_not_spec_implicated)

    record = orch.run_cycle(prior_records=[], config=_config())

    assert orch.last_resume_determination is PipelineStage.SYNTHESIS
    assert len(captured_budgets) == 1, "run_cycle must bind exactly one budget for the whole cycle"
    # Bounded to the resumed portion: design+review charged once (attempt
    # 0), not twice (attempt 0 AND a re-derived attempt 1) -- a full restart
    # would cost 2 * phase1_calls instead (see the companion test below).
    assert captured_budgets[0].calls_made == phase1_calls
    assert record.backtest.status.startswith("failed")


@pytest.mark.strategy_lab_integration
def test_full_restart_reentry_pays_full_pipeline_llm_call_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Companion/contrast case: an otherwise-identical fixture, but the
    triggering failure declares ``spec_implicated=True`` (every current
    production raise site's default), so ``run_cycle`` falls back to a full
    restart on re-entry instead of resuming. Design+review is paid for
    twice -- once per attempt -- proving the bounded case above is a real,
    tight regression guard against the unbounded cost this test locks in as
    the (undesirable, but currently-still-possible for any real raise site
    today) alternative.
    """
    from investment_team.strategy_lab.checkpoints import PipelineStage
    from investment_team.strategy_lab.exceptions import SpecImplementabilityError
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    from .test_strategy_lab_phase_transitions import _stub_pipeline_for_happy_path

    # Pin both env-derived limits -- see the identical note on the bounded-
    # resume test above; this contrast case needs headroom for TWICE one
    # attempt's charges (a full restart pays design+review twice).
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_ROUNDS", "5")
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_MAX_LLM_CALLS", "20")

    orch = StrategyLabOrchestrator()
    _stub_pipeline_for_happy_path(monkeypatch, orch)
    phase1_calls = _charging_design_stubs(monkeypatch, orch, review_not_ready_rounds=1)
    captured_budgets = _capture_cycle_budget(monkeypatch)

    real_synthesize = orch._synthesize_initial_code

    def _fail_first_attempt_spec_implicated(**kwargs: Any) -> Any:
        if kwargs["design_attempt"] == 0:
            raise SpecImplementabilityError(
                "forced fail at synthesis boundary, spec-implicated",
                failure_phase="synthesis",
                last_spec=kwargs["spec"],
                last_code="",
                spec_implicated=True,
            )
        return real_synthesize(**kwargs)

    monkeypatch.setattr(orch, "_synthesize_initial_code", _fail_first_attempt_spec_implicated)

    record = orch.run_cycle(prior_records=[], config=_config())

    assert orch.last_resume_determination is PipelineStage.SYNTHESIS
    assert len(captured_budgets) == 1
    # Full restart: design+review is re-derived (and re-charged) for attempt 1.
    assert captured_budgets[0].calls_made == 2 * phase1_calls
    assert record.backtest.status.startswith("failed")
