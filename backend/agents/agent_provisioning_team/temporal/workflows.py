"""Temporal workflows for the Agent Provisioning team.

Two workflows are exposed:

* ``AgentProvisioningWorkflow`` — v1, delegates to a single activity. Kept
  as the compatibility path for the current API.

* ``AgentProvisioningWorkflowV2`` — v2, decomposes provisioning into
  per-phase activities and fans out tool provisioning in parallel via
  ``asyncio.gather``. Each tool activity has its own retry policy and
  heartbeat, so a flaky external tool can be retried independently
  without re-doing the whole job. On any tool failure a compensation
  activity is invoked to roll back partially-provisioned resources.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from agent_provisioning_team.shared.tool_manifest import load_manifest
    from agent_provisioning_team.temporal import activities as _activities
    from agent_provisioning_team.temporal.constants import TASK_QUEUE

PROVISIONING_TIMEOUT = timedelta(hours=4)
PHASE_TIMEOUT = timedelta(minutes=20)
TOOL_ACTIVITY_TIMEOUT = timedelta(minutes=15)
TOOL_HEARTBEAT_TIMEOUT = timedelta(minutes=2)

DEFAULT_RETRY_POLICY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=30),
    maximum_interval=timedelta(minutes=2),
    backoff_coefficient=2.0,
)

TOOL_RETRY_POLICY = RetryPolicy(
    maximum_attempts=4,
    initial_interval=timedelta(seconds=15),
    maximum_interval=timedelta(minutes=2),
    backoff_coefficient=2.0,
    non_retryable_error_types=["ValueError"],
)


@workflow.defn(name="AgentProvisioningWorkflow")
class AgentProvisioningWorkflow:
    """v1: Runs one provisioning job as a single activity."""

    @workflow.run
    async def run(
        self,
        job_id: str,
        agent_id: str,
        manifest_path: str,
        access_tier_str: str,
    ) -> None:
        await workflow.execute_activity(
            _activities.run_provisioning_activity,
            args=[job_id, agent_id, manifest_path, access_tier_str],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=PROVISIONING_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )


@workflow.defn(name="AgentProvisioningWorkflowV2")
class AgentProvisioningWorkflowV2:
    """v2: Per-phase activities with parallel per-tool fan-out."""

    @workflow.run
    async def run(
        self,
        job_id: str,
        agent_id: str,
        manifest_path: str,
        access_tier_str: str,
    ) -> None:
        # Phase 1: setup (Docker environment).
        await workflow.execute_activity(
            _activities.setup_activity_v2,
            args=[agent_id, manifest_path, access_tier_str],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=PHASE_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )

        # Phase 2: credential generation.
        creds_result = await workflow.execute_activity(
            _activities.credentials_activity_v2,
            args=[agent_id, manifest_path],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=PHASE_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )
        credentials_by_tool = creds_result["credentials"]

        # Phase 3: fan out per-tool provisioning.
        manifest = load_manifest(manifest_path)
        tool_names = [t.name for t in manifest.tools]

        async def _one(tool_name: str):
            creds_dump = credentials_by_tool.get(tool_name, {})
            return await workflow.execute_activity(
                _activities.provision_tool_activity,
                args=[agent_id, tool_name, manifest_path, access_tier_str, creds_dump],
                task_queue=TASK_QUEUE,
                start_to_close_timeout=TOOL_ACTIVITY_TIMEOUT,
                heartbeat_timeout=TOOL_HEARTBEAT_TIMEOUT,
                retry_policy=TOOL_RETRY_POLICY,
            )

        results = await asyncio.gather(
            *[_one(name) for name in tool_names],
            return_exceptions=True,
        )

        succeeded: list[str] = []
        failures: list[str] = []
        for name, res in zip(tool_names, results):
            if isinstance(res, BaseException):
                failures.append(f"{name}: {res}")
            elif isinstance(res, dict) and res.get("success"):
                succeeded.append(name)
            else:
                failures.append(f"{name}: {res.get('error') if isinstance(res, dict) else 'unknown'}")

        if failures:
            # Compensation: roll back the ones that did succeed.
            await workflow.execute_activity(
                _activities.compensate_activity_v2,
                args=[agent_id, succeeded],
                task_queue=TASK_QUEUE,
                schedule_to_close_timeout=PHASE_TIMEOUT,
                retry_policy=DEFAULT_RETRY_POLICY,
            )
            raise RuntimeError(
                f"Tool provisioning failed for agent {agent_id}: {'; '.join(failures)}"
            )
