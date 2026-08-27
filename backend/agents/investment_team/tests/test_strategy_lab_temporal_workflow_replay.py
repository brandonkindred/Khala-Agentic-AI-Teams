"""Determinism-replay proof for ``StrategyLabCycleWorkflow``.

Closes out the last unmet acceptance criterion of the Temporal-mode wiring
work (thread mode's ``run_cycle`` and Temporal mode's
``StrategyLabCycleWorkflow.run`` sharing one implementation of the
directive-gathering and terminal-guard logic via
``strategy_lab.cycle_control``, through ``temporal/dto.py``'s adapters — see
those modules' docstrings): a real workflow-history replay confirming the
shared functions introduce no nondeterminism when driven inside an actual
temporalio workflow sandbox, rather than the monkeypatched-``execute_activity``
unit tests in ``test_strategy_lab_temporal_workflows.py`` (control-flow shape
only, no sandbox) or the restricted-call guard in
``test_strategy_lab_temporal_sandbox.py`` (proves no *disallowed* call is
made, not that history replays cleanly).

Pattern mirrors ``software_engineering_team/tests/test_coding_team_temporal_workflow.py``
(the only other place in this repo driving a real embedded Temporal test
server): start a time-skipping ``WorkflowEnvironment``, run a ``Worker`` with
fake activities registered under the *same* ``@activity.defn`` names the real
ones use (Temporal dispatches by registered name, not Python object
identity), drive the workflow to completion, fetch its history, and replay
that history with ``Replayer`` — a nondeterminism in workflow code raises
during replay.
"""

from __future__ import annotations

import contextlib
from typing import Any, Dict

import pytest


def _config_dict(**overrides: Any) -> Dict[str, Any]:
    """Local copy of ``test_strategy_lab_temporal_workflows.py``'s helper of
    the same name -- kept file-local rather than imported so this module has
    no dependency on that file's private (underscore-prefixed) internals.
    """
    base = {"start_date": "2023-01-01", "end_date": "2023-12-31"}
    base.update(overrides)
    return base


_WF_CONFIG = {
    "design_review_rounds": 20,
    "design_review_stall_rounds": 3,
    "mechanical_repair_enabled": True,
    "code_conformance_retries": 2,
    "design_max_llm_calls": 120,
    "regime_summary_enabled": False,
    "max_design_reentries": 2,
}


def _reentry_outcome(**overrides: Any) -> Dict[str, Any]:
    """Local copy of ``test_strategy_lab_temporal_workflows.py``'s helper of
    the same name -- see ``_config_dict``'s docstring for why this module
    keeps its own copy instead of importing it.
    """
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
    """Local copy of ``test_strategy_lab_temporal_workflows.py``'s helper of
    the same name -- see ``_config_dict``'s docstring for why this module
    keeps its own copy instead of importing it.
    """
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


@contextlib.asynccontextmanager
async def _workflow_environment():
    """Start a time-skipping ``WorkflowEnvironment`` with no worker attached.

    Duplicated from ``test_coding_team_temporal_workflow.py``'s helper of the
    same name rather than imported from a shared module: this repo's
    Temporal tests each build their own small local harness rather than
    sharing one across test modules (see
    ``test_strategy_lab_temporal_cancellation.py`` and
    ``test_strategy_lab_checkpoint_crash_resumption.py``, the two other
    ``investment_team`` test files with an identical local copy of this same
    helper -- the latter's docstring states the convention explicitly).

    Preconditions:
        - Caller is an async test that will drive the yielded ``env`` and any
          workers itself.
    Postconditions:
        - Yields a started ``WorkflowEnvironment``. Skips the test (rather
          than failing) when the ephemeral Temporal test-server binary
          cannot be downloaded — same egress caveat as every other copy of
          this helper. The environment is shut down on exit.
    """
    from temporalio.testing import WorkflowEnvironment

    try:
        test_env = await WorkflowEnvironment.start_time_skipping()
    except RuntimeError as exc:
        pytest.skip(f"Temporal ephemeral test server unavailable (no egress?): {exc}")

    async with test_env as env:
        yield env


def _make_fake_activities(
    *, design_attempt_outcomes: list[Dict[str, Any]], config_overrides: Dict[str, Any]
):
    """Build fake activities registered under the real activity names.

    Preconditions:
        - ``design_attempt_outcomes`` is non-empty; each entry is a full
          ``run_design_attempt_activity``-shaped result dict (see
          ``_reentry_outcome``/``_record_outcome``).
        - ``config_overrides`` supplies ``_WF_CONFIG``-shaped overrides (e.g.
          ``max_design_reentries``) for the fake config-resolution activity.
    Postconditions:
        - Returns a list of ``@activity.defn``-decorated callables suitable
          for ``Worker(activities=...)``, registered under
          ``strategy_lab_resolve_workflow_config``,
          ``strategy_lab_run_design_attempt``, and
          ``strategy_lab_build_short_circuit_record`` — the three activities
          ``StrategyLabCycleWorkflow.run`` can reach with
          ``regime_summary_enabled=False`` and no market-data-skip outcome.
        - ``strategy_lab_run_design_attempt`` returns ``design_attempt_outcomes``
          in order for the first ``len(design_attempt_outcomes)`` calls, then
          repeats the final outcome for any further call (it does not raise
          or stop once exhausted).
    """
    from temporalio import activity

    calls = {"design_attempt": 0}

    @activity.defn(name="strategy_lab_resolve_workflow_config")
    def _fake_resolve_workflow_config() -> Dict[str, Any]:
        base = dict(_WF_CONFIG)
        base.update(config_overrides)
        return base

    @activity.defn(name="strategy_lab_run_design_attempt")
    def _fake_run_design_attempt(params: Dict[str, Any]) -> Dict[str, Any]:
        index = calls["design_attempt"]
        calls["design_attempt"] += 1
        return design_attempt_outcomes[min(index, len(design_attempt_outcomes) - 1)]

    @activity.defn(name="strategy_lab_build_short_circuit_record")
    def _fake_build_short_circuit_record(params: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "record": {
                "lab_record_id": "short-circuit-rec-1",
                "status": "failed: spec_unimplementable",
            },
            "convergence_tracker_state": params["convergence_tracker_state"],
        }

    return [
        _fake_resolve_workflow_config,
        _fake_run_design_attempt,
        _fake_build_short_circuit_record,
    ]


async def _run_cycle_workflow_and_replay(*, design_attempt_outcomes, config_overrides):
    """Drive ``StrategyLabCycleWorkflow`` to completion, then replay its history.

    Preconditions:
        - See ``_make_fake_activities``.
    Postconditions:
        - Returns the workflow's result dict. Raises if history replay
          detects nondeterminism, or the workflow itself fails/times out.
        - Skips (does not fail) the test if the ephemeral Temporal test
          server is unavailable.
    """
    import concurrent.futures

    from temporalio.worker import Replayer, Worker

    from investment_team.strategy_lab.temporal.workflows import (
        TASK_QUEUE,
        StrategyLabCycleWorkflow,
    )
    from shared.temporal.worker import _build_workflow_runner

    fake_activities = _make_fake_activities(
        design_attempt_outcomes=design_attempt_outcomes, config_overrides=config_overrides
    )

    cycle_input = {
        "prior_records": [],
        "config": _config_dict(),
        "signal_brief": None,
        "exclude_asset_classes": None,
        "convergence_tracker_state": {},
        "run_id": "replay-test-run-1",
        "cycle_index": 0,
        "generation": 1,
        "batch_cache_key": None,
    }

    async with _workflow_environment() as env:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as activity_executor:
            worker = Worker(
                env.client,
                task_queue=TASK_QUEUE,
                workflows=[StrategyLabCycleWorkflow],
                activities=fake_activities,
                activity_executor=activity_executor,
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
                    id="strategy-lab-cycle-workflow-replay-test",
                    task_queue=TASK_QUEUE,
                )
                result = await handle.result()
                history = await handle.fetch_history()

    # Same passthrough runner as the Worker above -- Replayer re-validates the
    # workflow by re-registering it, hitting the identical numpy/pandas
    # re-import crash without it.
    await Replayer(
        workflows=[StrategyLabCycleWorkflow], workflow_runner=_build_workflow_runner()
    ).replay_workflow(history)
    return result


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_after_reentry_then_record_is_deterministic() -> None:
    """One re-entry (exercising ``gather_convergence_directives`` and the
    directive-append on re-entry) followed by a successful record — the
    common path — replays without a nondeterminism error.
    """
    result = await _run_cycle_workflow_and_replay(
        design_attempt_outcomes=[_reentry_outcome(), _record_outcome()],
        config_overrides={"max_design_reentries": 2},
    )

    assert result["record"] == {"lab_record_id": "rec-1"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_after_reentry_budget_exhausted_is_deterministic() -> None:
    """Every attempt re-enters until the budget is exhausted, reaching the
    terminal-guard ``require_short_circuit_inputs`` call (via ``dto.py``'s
    adapter into ``cycle_control.py``) and the short-circuit record build —
    the path #7305's terminal-guard wiring covers — and still replays
    without a nondeterminism error.
    """
    result = await _run_cycle_workflow_and_replay(
        design_attempt_outcomes=[_reentry_outcome()],
        config_overrides={"max_design_reentries": 1},
    )

    assert result["record"] == {
        "lab_record_id": "short-circuit-rec-1",
        "status": "failed: spec_unimplementable",
    }
    assert "convergence_tracker_state" in result
