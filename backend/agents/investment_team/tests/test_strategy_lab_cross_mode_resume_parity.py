"""Cross-mode parity assertion for Strategy Lab cross-attempt re-entry.

``test_strategy_lab_checkpoint_crash_resumption.py`` already proves, in
thread mode and in Temporal mode *separately*, that a
``SpecImplementabilityError`` raised after partial convergence (design +
review having already reached a checkpoint) resumes past ``DESIGN``/
``REVIEW`` rather than re-running the whole pipeline. Both proofs drive the
same shared scenario, defined once in ``strategy_lab_reentry_fixtures.py``,
so they can't independently drift to different "known points of partial
convergence" or differently-shaped forced failures.

What neither of those tests does is drive both modes in one test and
directly compare their outcomes. This module closes that last gap: it
drives the identical fixture-defined scenario through both
``StrategyLabOrchestrator.run_cycle`` (thread mode) and
``StrategyLabCycleWorkflow.run`` (Temporal mode) and asserts both resolve
the re-entry to the *same* ``PipelineStage`` -- the epic's (#7269) central
cross-mode parity claim, proven end-to-end rather than asserted only in a
PR description.

Both modes' resume-stage determinations are already produced by one shared
gate (``checkpoints.py``'s ``resolve_cross_attempt_resume`` /
``determine_resume_stage``, called identically from ``orchestrator.py``'s
``run_cycle`` and from ``temporal/workflows.py``'s
``StrategyLabCycleWorkflow.run``); this test is a black-box check that both
real entry points -- not the shared gate directly -- land on the same
value, so a regression in either mode's own call site (not just the shared
gate itself) would be caught here.
"""

from __future__ import annotations

import concurrent.futures
from typing import Any, Dict
from unittest import mock

import pytest

from shared.temporal.testing import workflow_environment as _workflow_environment

from .strategy_lab_reentry_fixtures import (
    REENTRY_REVIEW_NOT_READY_ROUNDS,
    _review_loop_stubs,
    synthesis_boundary_spec_implementability_error,
)
from .strategy_lab_temporal_fixtures import build_strategy_lab_worker
from .test_strategy_lab_checkpoint_crash_resumption import (
    _backtest_config_dict,
    _config,
    _fake_job_store,
    _FakeRecord,
    _spec_dict,
)
from .test_strategy_lab_phase_transitions import _stub_pipeline_for_happy_path


def _run_thread_mode(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Drive the shared re-entry fixture through thread mode's real
    ``StrategyLabOrchestrator.run_cycle`` re-entry branch and return the
    ``PipelineStage`` it resolved the resume to.

    Mirrors ``test_cross_attempt_resume_llm_call_count_bounded_to_resumed_portion``
    in ``test_strategy_lab_checkpoint_crash_resumption.py`` -- only the
    resume stage matters here, not the LLM-call cost that test already
    covers, so the no-charge ``_review_loop_stubs`` variant is enough.
    """
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator

    # Same rationale as the sibling bounded-cost test: pin both env-derived
    # limits so this fixture's review rounds/LLM-call ceiling can't be
    # exhausted by an environment-overridable default, for reasons unrelated
    # to resume-stage resolution.
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_REVIEW_ROUNDS", "5")
    monkeypatch.setenv("STRATEGY_LAB_DESIGN_MAX_LLM_CALLS", "20")

    orch = StrategyLabOrchestrator()
    _stub_pipeline_for_happy_path(monkeypatch, orch)
    _review_loop_stubs(monkeypatch, orch, review_not_ready_rounds=REENTRY_REVIEW_NOT_READY_ROUNDS)

    real_synthesize = orch._synthesize_initial_code

    def _fail_first_attempt_not_spec_implicated(**kwargs: Any) -> Any:
        if kwargs["design_attempt"] == 0:
            raise synthesis_boundary_spec_implementability_error(
                spec=kwargs["spec"], spec_implicated=False
            )
        return real_synthesize(**kwargs)

    monkeypatch.setattr(orch, "_synthesize_initial_code", _fail_first_attempt_not_spec_implicated)

    orch.run_cycle(prior_records=[], config=_config())

    return orch.last_resume_determination


async def _run_temporal_mode(monkeypatch: pytest.MonkeyPatch) -> str:
    """Drive the identical shared re-entry fixture through Temporal mode's
    real ``StrategyLabCycleWorkflow`` re-entry branch and return the resume
    stage it resolved, as the ``str`` value the workflow itself returns.

    Mirrors ``test_design_attempt_activity_cross_attempt_resume_bounds_llm_call_cost``
    in ``test_strategy_lab_checkpoint_crash_resumption.py`` -- only the
    resume stage matters here, not the LLM-call cost that test already
    covers, so the checkpoint/error shape is copied verbatim but no budget
    charging is asserted.
    """
    from investment_team.models import StrategySpec
    from investment_team.strategy_lab import orchestrator_api, phases, run_state
    from investment_team.strategy_lab._orchestrator_helpers import _DesignPersistContext
    from investment_team.strategy_lab.checkpoints import ReviewCheckpoint
    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator
    from investment_team.strategy_lab.temporal.workflows import TASK_QUEUE, StrategyLabCycleWorkflow

    _, fake_persist, fake_load, fake_generation = _fake_job_store()
    monkeypatch.setattr(orchestrator_api, "_persist_run_state", fake_persist)
    monkeypatch.setattr(run_state, "load_run_from_job_service", fake_load)
    monkeypatch.setattr(run_state, "get_run_generation_strict", fake_generation)

    checkpointed_spec = StrategySpec.parse_persisted(_spec_dict())
    # Production's design_context.rounds/review_rounds_completed count every
    # review round including the final ready one (critique_history.append()
    # runs unconditionally each round, orchestrator_design.py), so the known
    # not-ready-round count needs +1 to match what a real converged attempt
    # would record. Same rationale as the sibling bounded-cost test.
    checkpointed_context = _DesignPersistContext(
        rounds=REENTRY_REVIEW_NOT_READY_ROUNDS + 1,
        critiques=[],
        stop_reason="converged",
        loop_telemetry={},
    )

    def _fake_run_design_attempt(self: Any, **kwargs: Any) -> Any:
        if kwargs["resume_spec"] is None:
            kwargs["checkpoint_hook"](
                "design_synthesis_boundary",
                {
                    "spec": checkpointed_spec,
                    "rationale": "r",
                    "design_context": checkpointed_context,
                },
            )
            # checkpoint_hook (above) only persists the ADR-012 SAME-attempt
            # crash-recovery store -- irrelevant here, since design_attempt 1
            # has never run before. The workflow's cross-attempt re-entry
            # branch instead reads outcome["pipeline_checkpoints"], built
            # from checkpoint_capture -- so a real ReviewCheckpoint must be
            # recorded here too, or resolve_cross_attempt_resume never
            # activates and attempt 1 re-enters this same branch instead of
            # the "resume_spec is not None" one below.
            capture = kwargs["checkpoint_capture"]
            if capture is not None:
                capture.record(
                    ReviewCheckpoint(
                        run_id=capture.run_id,
                        cycle_scope=capture.cycle_scope,
                        design_attempt=kwargs["design_attempt"],
                        generation=capture.generation,
                        spec_hash=phases.hash_spec(checkpointed_spec),
                        code_hash=phases.hash_code(None),
                        captured_at="2026-08-31T00:00:00Z",
                        budget_calls=0,
                        gate_results=[],
                        spec_history=[],
                        code_history=[],
                        gate_timeline=[],
                        spec=checkpointed_spec,
                        rationale="r",
                        design_context={
                            "rounds": REENTRY_REVIEW_NOT_READY_ROUNDS + 1,
                            "critiques": [],
                            "stop_reason": "converged",
                            "loop_telemetry": {},
                        },
                        review_rounds_completed=REENTRY_REVIEW_NOT_READY_ROUNDS + 1,
                    )
                )
            raise synthesis_boundary_spec_implementability_error(
                spec=checkpointed_spec, spec_implicated=False
            )
        assert kwargs["resume_spec"] == checkpointed_spec
        return _FakeRecord()

    cycle_input: Dict[str, Any] = {
        "prior_records": [],
        "config": _backtest_config_dict(),
        "signal_brief": None,
        "exclude_asset_classes": None,
        "convergence_tracker_state": {},
        # At least one re-entry must actually run (attempt 0 fails, attempt
        # 1 resumes) for a resume stage to be resolved at all.
        "workflow_config": {"regime_summary_enabled": False, "max_design_reentries": 1},
        "run_id": "run-cross-mode-parity",
        "generation": 1,
    }

    async with _workflow_environment() as env:
        with (
            concurrent.futures.ThreadPoolExecutor(max_workers=2) as activity_executor,
            mock.patch.object(
                StrategyLabOrchestrator, "_run_design_attempt", _fake_run_design_attempt
            ),
        ):
            worker = build_strategy_lab_worker(env, activity_executor)
            async with worker:
                handle = await env.client.start_workflow(
                    StrategyLabCycleWorkflow.run,
                    cycle_input,
                    id="strategy-lab-cross-mode-parity-test",
                    task_queue=TASK_QUEUE,
                )
                result = await handle.result()

    assert result["record"]["lab_record_id"] == "rec-resumed"
    return result["resume_stage_determinations"][-1]


@pytest.mark.strategy_lab_integration
@pytest.mark.asyncio
async def test_thread_and_temporal_modes_resume_from_same_pipeline_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Thread mode and Temporal mode, driven through the identical shared
    ``SpecImplementabilityError``-after-partial-convergence scenario, must
    resume from the same ``PipelineStage`` -- the parity claim epic #7269's
    shared checkpoint model exists to guarantee. No wall-clock timing is
    asserted anywhere in this test (``_workflow_environment()`` runs
    Temporal's own embedded, time-skipping test server); every assertion
    below is a deterministic structural/enum comparison.
    """
    from investment_team.strategy_lab.checkpoints import PipelineStage

    thread_mode_stage = _run_thread_mode(monkeypatch)
    temporal_mode_stage_str = await _run_temporal_mode(monkeypatch)

    assert thread_mode_stage is not None, "thread mode did not resolve any resume stage"
    assert temporal_mode_stage_str is not None, "Temporal mode did not resolve any resume stage"
    assert PipelineStage(temporal_mode_stage_str) is thread_mode_stage, (
        "thread mode and Temporal mode resolved DIFFERENT resume stages for the "
        "identical forced-SpecImplementabilityError-after-partial-convergence "
        f"scenario: thread={thread_mode_stage!r} temporal={temporal_mode_stage_str!r}"
    )
    # Anchor value, not just cross-mode equality -- guards against both
    # modes drifting to the same WRONG stage together (a vacuous pass).
    assert thread_mode_stage is PipelineStage.SYNTHESIS
