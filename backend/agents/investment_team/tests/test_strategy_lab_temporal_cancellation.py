"""Integration test: Strategy Lab's cooperative-cancellation contract.

Every other Strategy Lab Temporal test (``test_strategy_lab_temporal_activities.py``,
``test_strategy_lab_temporal_workflows.py``) drives ``run_design_attempt_activity``
or ``StrategyLabCycleWorkflow`` directly as plain Python, with
``activity.is_cancelled()`` / ``workflow.execute_activity`` monkeypatched --
useful for the checkpoint/loop SHAPE, but none of them touch a real Temporal
server, so none can observe the actual *timing* a terminate delivers. This
file is the first real-``WorkflowEnvironment`` test for Strategy Lab: it
drives the genuine ``StrategyLabCycleWorkflow`` **and** the genuine, unmodified
``run_design_attempt_activity`` against a real (embedded, time-skipping)
Temporal test server, so a real ``handle.terminate()`` exercises the actual
production cancellation wiring end-to-end -- its ``BackgroundHeartbeat``,
``activity.is_cancelled()`` checkpoint, and ``no_thread_cancel_exception=True``
configuration (``activities.py``'s ``run_design_attempt_activity``). A
regression to any of that wiring (e.g. someone drops the heartbeat wrapper or
the ``no_thread_cancel_exception`` flag) would fail this test, because the
real activity function -- not a hand-rolled substitute -- is what the worker
registers and executes.

The only thing stubbed is ``StrategyLabOrchestrator._run_design_attempt``
itself -- the expensive design/synthesis/refinement/verification pipeline
(LLM calls, sandboxed backtests, market data) -- via the same
``monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt", ...)``
technique ``test_strategy_lab_temporal_activities.py``'s
``test_run_design_attempt_activity_raises_cancelled_between_checkpoints``
already uses for a direct-call unit test. The stub loops calling the real
``emit`` callback the activity passes it (``_design_attempt_cancellation_checkpoint``),
exactly like a real phase pipeline would between steps, so cancellation is
still delivered and observed through the production checkpoint function, not
a reimplementation of it. ``activities._DESIGN_ATTEMPT_HEARTBEAT_INTERVAL_S``
(20s in production) is patched down for the test's own loop cadence, so a
real, unmodified heartbeat round trip still carries the terminate-triggered
cancellation back to ``is_cancelled()`` within the test's bounded wait,
instead of requiring a 20s+ test.

``handle.terminate()`` (not ``handle.cancel()``) is the primitive under test
because it is the exact one Strategy Lab's own restart path uses --
``shared.temporal.terminate_and_await_workflow_sync``, called from
``investment_team/api/main.py`` before dispatching a fresh cycle-0 workflow.

Generation fencing (the separate, already-existing correctness guarantee that
a stale activity's *write* is rejected regardless of whether cancellation
lands) is untouched by this change and not re-covered here -- its
comprehensive direct-call tests live in
``test_strategy_lab_temporal_activities.py`` (``_check_generation_fencing`` /
``persist_run_state_activity`` / ``finalize_cycle_record_activity`` coverage).
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from typing import Any, Dict
from unittest import mock

import pytest

# Worst-case bound: the stubbed design-attempt loop iterates this many times,
# sleeping this long between checks, so a totally broken cancellation path
# still finishes (uncancelled) in single-digit seconds rather than hanging
# the suite.
_FAKE_ITERATION_SLEEP_S = 0.25
_FAKE_MAX_ITERATIONS = 40  # 10s worst-case ceiling if cancellation never lands
# Patches activities._DESIGN_ATTEMPT_HEARTBEAT_INTERVAL_S (20s in production)
# down to this for the test -- see module docstring. Short enough that at
# least one real heartbeat round trip lands well inside _CANCEL_OBSERVED_BOUND_S.
_TEST_HEARTBEAT_INTERVAL_S = 0.3
# Prompt-exit bound this test asserts against -- generous relative to
# _TEST_HEARTBEAT_INTERVAL_S but far under both the stub's own 10s ceiling and
# the real workflow's _DESIGN_ATTEMPT_HEARTBEAT_TIMEOUT (90s).
_CANCEL_OBSERVED_BOUND_S = 5.0


@contextlib.asynccontextmanager
async def _workflow_environment():
    """Start a time-skipping ``WorkflowEnvironment`` with no worker attached.

    Preconditions:
        - Caller is an async test that will drive the yielded ``env`` and any
          workers itself.
    Postconditions:
        - Yields a started ``WorkflowEnvironment``. Skips the test (rather
          than failing) when the ephemeral Temporal test-server binary cannot
          be downloaded -- same egress caveat as
          ``test_coding_team_temporal_workflow.py``'s helper of the same
          name, the only other place in this repo that drives
          ``temporalio.testing.WorkflowEnvironment``. The environment is shut
          down on exit.
    """
    from temporalio.testing import WorkflowEnvironment

    try:
        test_env = await WorkflowEnvironment.start_time_skipping()
    except RuntimeError as exc:
        pytest.skip(f"Temporal ephemeral test server unavailable (no egress?): {exc}")

    async with test_env as env:
        yield env


def _make_fake_run_design_attempt(state: Dict[str, Any]):
    """Build a stand-in for ``StrategyLabOrchestrator._run_design_attempt``.

    Preconditions:
        ``state`` is a fresh dict with a ``"started"`` key holding a
        ``threading.Event``.
    Postconditions:
        Returns a callable with the same ``(self, **kwargs)`` shape
        ``monkeypatch.setattr(StrategyLabOrchestrator, "_run_design_attempt",
        ...)`` expects. Calling it sets ``state["started"]`` and
        ``state["start_time"]`` immediately, then repeatedly calls the real
        ``emit`` callback the caller passed in (production's
        ``_design_attempt_cancellation_checkpoint``) with short sleeps
        between calls -- exactly how the real phase pipeline threads
        cancellation checks between steps. Sets ``state["cancelled_at"]`` and
        re-raises the moment ``emit`` raises (cancellation observed), or sets
        ``state["ran_to_completion"] = True`` and raises ``RuntimeError`` if
        the loop exhausts ``_FAKE_MAX_ITERATIONS`` uncancelled.
    """

    def _fake_run_design_attempt(self: Any, **kwargs: Any) -> Any:
        state["start_time"] = time.monotonic()
        state["started"].set()
        emit = kwargs["emit"]

        for _ in range(_FAKE_MAX_ITERATIONS):
            try:
                emit("test-cancellation-checkpoint", {})
            except BaseException:
                state["cancelled_at"] = time.monotonic()
                raise
            time.sleep(_FAKE_ITERATION_SLEEP_S)

        state["ran_to_completion"] = True
        raise RuntimeError(
            "fake design attempt loop exhausted without observing cancellation "
            "-- cooperative cancellation regressed"
        )

    return _fake_run_design_attempt


async def _wait_until(predicate, *, timeout_s: float, message: str) -> None:
    """Poll ``predicate`` until it's true or raise ``TimeoutError`` with ``message``."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(message)
        await asyncio.sleep(0.05)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_design_attempt_activity_stops_promptly_after_workflow_terminate() -> None:
    """A real ``handle.terminate()`` stops an in-flight design-attempt activity
    promptly instead of letting it run to completion.

    Drives the real ``StrategyLabCycleWorkflow`` and the real
    ``run_design_attempt_activity`` (unmodified, registered under its
    production name), with only ``StrategyLabOrchestrator._run_design_attempt``
    stubbed (see ``_make_fake_run_design_attempt``) so this exercises the
    activity's own heartbeat/cancellation-checkpoint/
    ``no_thread_cancel_exception`` wiring for real. Passing ``workflow_config``
    directly in ``cycle_input`` short-circuits the workflow's
    ``resolve_workflow_config_activity`` / ``compute_regime_summary_activity``
    calls (regime summaries disabled), and ``max_design_reentries: 0`` makes
    the workflow call the design-attempt activity exactly once.

    Terminates the workflow via ``handle.terminate()`` -- the same primitive
    Strategy Lab's own restart path uses -- once the stub signals it has
    started, then asserts the activity observed cancellation within
    ``_CANCEL_OBSERVED_BOUND_S`` and never reached its own completion path.
    """
    import concurrent.futures

    from temporalio.worker import Worker

    from investment_team.strategy_lab.orchestrator import StrategyLabOrchestrator
    from investment_team.strategy_lab.temporal import activities as act
    from investment_team.strategy_lab.temporal.workflows import (
        TASK_QUEUE,
        StrategyLabCycleWorkflow,
    )
    from shared.temporal.worker import _build_workflow_runner

    state: Dict[str, Any] = {"started": threading.Event()}
    fake_run_design_attempt = _make_fake_run_design_attempt(state)

    cycle_input = {
        "prior_records": [],
        "config": {"start_date": "2023-01-01", "end_date": "2023-12-31"},
        "signal_brief": None,
        "exclude_asset_classes": None,
        "convergence_tracker_state": {},
        "workflow_config": {"regime_summary_enabled": False, "max_design_reentries": 0},
    }

    async with _workflow_environment() as env:
        with (
            concurrent.futures.ThreadPoolExecutor(max_workers=2) as activity_executor,
            mock.patch.object(
                StrategyLabOrchestrator, "_run_design_attempt", fake_run_design_attempt
            ),
            mock.patch.object(
                act, "_DESIGN_ATTEMPT_HEARTBEAT_INTERVAL_S", _TEST_HEARTBEAT_INTERVAL_S
            ),
        ):
            worker = Worker(
                env.client,
                task_queue=TASK_QUEUE,
                workflows=[StrategyLabCycleWorkflow],
                activities=[act.run_design_attempt_activity],
                activity_executor=activity_executor,
                max_cached_workflows=0,
                # Matches shared.temporal.worker._run_worker_async's real
                # worker construction: without the numpy/pandas passthrough,
                # validating StrategyLabCycleWorkflow re-imports
                # investment_team/__init__.py's transitive chain (.agents ->
                # .models -> .execution.metrics -> numpy) inside the
                # sandbox's isolated namespace, and numpy's C extension can't
                # be loaded a second time in the same process.
                workflow_runner=_build_workflow_runner(),
            )
            async with worker:
                handle = await env.client.start_workflow(
                    StrategyLabCycleWorkflow.run,
                    cycle_input,
                    id="strategy-lab-cancellation-test",
                    task_queue=TASK_QUEUE,
                )

                # An outstanding activity task keeps the time-skipping server's
                # auto-skip disabled, but disable it explicitly too -- this
                # section's timing bounds must reflect real wall-clock elapsed
                # time, not the server's logical clock.
                with env.auto_time_skipping_disabled():
                    await _wait_until(
                        state["started"].is_set,
                        timeout_s=_CANCEL_OBSERVED_BOUND_S,
                        message="design-attempt activity never started",
                    )

                    terminate_issued_at = time.monotonic()
                    await handle.terminate(
                        reason="test: prompt cancellation after workflow terminate"
                    )

                    await _wait_until(
                        lambda: "cancelled_at" in state,
                        timeout_s=_CANCEL_OBSERVED_BOUND_S,
                        message=(
                            "design-attempt activity did not observe cancellation "
                            f"within {_CANCEL_OBSERVED_BOUND_S}s of workflow terminate"
                        ),
                    )

    # Measured from immediately before handle.terminate() was issued, not from
    # activity startup -- any scheduler delay between the activity starting and
    # the test coroutine getting to call terminate() is not cancellation
    # latency and must not count against this bound.
    elapsed = state["cancelled_at"] - terminate_issued_at
    assert elapsed < _CANCEL_OBSERVED_BOUND_S, (
        f"design-attempt activity took {elapsed:.2f}s after terminate() to observe "
        f"cancellation, expected under {_CANCEL_OBSERVED_BOUND_S}s"
    )
    assert not state.get("ran_to_completion"), (
        "design-attempt activity ran to completion instead of being cancelled "
        "-- cooperative cancellation regressed"
    )
