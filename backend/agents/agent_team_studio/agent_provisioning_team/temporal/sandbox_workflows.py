"""Temporal workflows for the Agent Provisioning sandbox lifecycle.

Three workflows wrap the agent-keyed sandbox pool:

* ``SandboxAcquireWorkflow`` — durably warm one agent's sandbox (execute-and-wait
  from the ``/warm`` route and the Agent Console runner).
* ``SandboxTeardownWorkflow`` — durably stop + evict one agent's sandbox.
* ``SandboxReaperWorkflow`` — a single self-scheduling durable loop that replaces
  the old ``asyncio.create_task(run_idle_reaper())`` background task. It sleeps,
  runs one reap activity, then ``continue_as_new`` to bound history. Run under a
  fixed workflow id so at most one instance exists across restarts/replicas.

Determinism: the reaper workflow body only ``sleep``s, calls the reap activity,
and continues-as-new — it reads no wall clock or environment (the idle threshold
is read inside ``sandbox_reap_activity``).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from agent_team_studio.agent_provisioning_team.temporal import sandbox_activities as _sb
    from agent_team_studio.agent_provisioning_team.temporal.constants import (
        SANDBOX_ACQUIRE_TIMEOUT_S,
        SANDBOX_REAP_TIMEOUT_S,
        SANDBOX_REAPER_INTERVAL_S,
        SANDBOX_TASK_QUEUE,
        SANDBOX_TEARDOWN_TIMEOUT_S,
    )

SANDBOX_RETRY_POLICY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=5),
    maximum_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
)

# Acquire has two permanent failures that must not be retried: an unknown agent
# (registry miss) and Docker being unavailable. Temporal matches these by the
# raised exception's class name.
SANDBOX_ACQUIRE_RETRY_POLICY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=5),
    maximum_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
    non_retryable_error_types=["UnknownAgentError", "DockerUnavailableError"],
)


@workflow.defn(name="SandboxAcquireWorkflow")
class SandboxAcquireWorkflow:
    """Warm one agent's sandbox durably."""

    @workflow.run
    async def run(self, agent_id: str) -> dict[str, Any]:
        """Run ``sandbox_acquire_activity`` and return the handle dump.

        Preconditions:
            * ``agent_id`` is non-empty.
        Postconditions:
            * Returns ``SandboxHandle.model_dump(mode="json")``.
        """
        assert agent_id, "agent_id must be non-empty"
        return await workflow.execute_activity(
            _sb.sandbox_acquire_activity,
            args=[agent_id],
            task_queue=SANDBOX_TASK_QUEUE,
            start_to_close_timeout=timedelta(seconds=SANDBOX_ACQUIRE_TIMEOUT_S),
            retry_policy=SANDBOX_ACQUIRE_RETRY_POLICY,
        )


@workflow.defn(name="SandboxTeardownWorkflow")
class SandboxTeardownWorkflow:
    """Tear down one agent's sandbox durably."""

    @workflow.run
    async def run(self, agent_id: str) -> None:
        """Run ``sandbox_teardown_activity``.

        Preconditions:
            * ``agent_id`` is non-empty.
        Postconditions:
            * The sandbox is stopped and evicted, or the activity raises.
        """
        assert agent_id, "agent_id must be non-empty"
        await workflow.execute_activity(
            _sb.sandbox_teardown_activity,
            args=[agent_id],
            task_queue=SANDBOX_TASK_QUEUE,
            start_to_close_timeout=timedelta(seconds=SANDBOX_TEARDOWN_TIMEOUT_S),
            retry_policy=SANDBOX_RETRY_POLICY,
        )


@workflow.defn(name="SandboxReaperWorkflow")
class SandboxReaperWorkflow:
    """Durable, single-instance idle reaper (replaces the asyncio task).

    Invariants:
        * At most one instance runs (started under a fixed workflow id).
        * The body is deterministic — ``workflow.sleep`` + one activity +
          ``continue_as_new``; no env/clock reads here.
    """

    @workflow.run
    async def run(self, interval_s: int = SANDBOX_REAPER_INTERVAL_S) -> None:
        """Sleep one interval, reap idle sandboxes once, then continue-as-new.

        Preconditions:
            * ``interval_s`` is a positive integer number of seconds.
        Postconditions:
            * Exactly one reap activity runs per iteration; the workflow restarts
              itself via ``continue_as_new`` so history stays bounded. A reap tick
              that fails even after ``SANDBOX_RETRY_POLICY``'s retries is logged
              and swallowed — the reaper always reaches ``continue_as_new`` so a
              single bad tick (e.g. Docker briefly unreachable) can never
              permanently kill this single-instance workflow.
        """
        assert interval_s > 0, "interval_s must be positive"
        await workflow.sleep(timedelta(seconds=interval_s))
        try:
            await workflow.execute_activity(
                _sb.sandbox_reap_activity,
                task_queue=SANDBOX_TASK_QUEUE,
                start_to_close_timeout=timedelta(seconds=SANDBOX_REAP_TIMEOUT_S),
                retry_policy=SANDBOX_RETRY_POLICY,
            )
        except Exception:
            workflow.logger.exception(
                "Sandbox idle-reap tick failed after retries; will try again next interval"
            )
        workflow.continue_as_new(interval_s)
