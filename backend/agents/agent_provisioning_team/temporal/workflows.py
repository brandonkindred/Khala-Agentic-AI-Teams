"""Temporal workflows for the Agent Provisioning team.

Two workflows are exposed:

* ``AgentProvisioningWorkflow`` — v1, delegates to a single activity. Kept
  registered so in-flight runs can drain during a deploy, but no longer
  targeted by the default routing path.

* ``AgentProvisioningWorkflowV2`` — v2, decomposes provisioning into
  per-phase activities and fans out tool provisioning in parallel via
  ``asyncio.gather``. Each tool activity has its own retry policy and
  heartbeat, so a flaky external tool can be retried independently
  without re-doing the whole job. Accepts ``skip_phases`` /
  ``prior_results`` on resume so completed phases are restored from the
  job store instead of re-executed.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

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
        skip_phases: list[str] | None = None,
        prior_results: dict[str, Any] | None = None,
    ) -> None:
        skip = set(skip_phases or [])
        prior = prior_results or {}

        # Phase 1: setup (Docker environment).
        setup_prior = prior.get("setup") if "setup" in skip else None
        setup_result = await workflow.execute_activity(
            _activities.setup_activity_v2,
            args=[job_id, agent_id, manifest_path, access_tier_str, setup_prior],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=PHASE_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )
        environment_dump = setup_result.get("environment") if setup_result else None

        # Phase 2: credential generation.
        creds_prior = (
            prior.get("credential_generation") if "credential_generation" in skip else None
        )
        creds_result = await workflow.execute_activity(
            _activities.credentials_activity_v2,
            args=[job_id, agent_id, manifest_path, creds_prior],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=PHASE_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )
        credentials_by_tool: dict[str, dict[str, Any]] = creds_result["credentials"]

        # Phase 3: fan out per-tool provisioning (or restore from prior).
        manifest = load_manifest(manifest_path)
        tool_names = [t.name for t in manifest.tools]

        if "account_provisioning" in skip and prior.get("account_provisioning"):
            # V1 mirrors whole-phase skip (no per-tool resume), so do the same here.
            ap = prior["account_provisioning"]
            tool_results_dump = list(ap.get("tool_results") or [])
            succeeded: list[dict] = [
                {
                    "tool_name": r.get("tool_name"),
                    "provisioner_key": r.get("provisioner_key"),
                }
                for r in tool_results_dump
                if r.get("success")
            ]
            failures: list[str] = [
                f"{r.get('tool_name')}: {r.get('error')}"
                for r in tool_results_dump
                if not r.get("success")
            ]
        else:
            tools_total = len(tool_names)

            async def _one(idx: int, tool_name: str) -> Any:
                creds_dump = credentials_by_tool.get(tool_name, {})
                return await workflow.execute_activity(
                    _activities.provision_tool_activity,
                    args=[
                        job_id,
                        agent_id,
                        tool_name,
                        manifest_path,
                        access_tier_str,
                        creds_dump,
                        idx,
                        tools_total,
                    ],
                    task_queue=TASK_QUEUE,
                    start_to_close_timeout=TOOL_ACTIVITY_TIMEOUT,
                    heartbeat_timeout=TOOL_HEARTBEAT_TIMEOUT,
                    retry_policy=TOOL_RETRY_POLICY,
                )

            raw_results = await asyncio.gather(
                *[_one(i, name) for i, name in enumerate(tool_names)],
                return_exceptions=True,
            )

            # Carry the registry key through with each success so compensation
            # can look the provisioner back up (see #293).
            succeeded = []
            failures = []
            tool_results_dump = []
            for name, res in zip(tool_names, raw_results):
                if isinstance(res, BaseException):
                    failures.append(f"{name}: {res}")
                    tool_results_dump.append(
                        {"tool_name": name, "success": False, "error": str(res)}
                    )
                elif isinstance(res, dict) and res.get("success"):
                    succeeded.append(
                        {
                            "tool_name": res.get("tool_name", name),
                            "provisioner_key": res.get("provisioner_key"),
                        }
                    )
                    tool_results_dump.append(res)
                else:
                    err = res.get("error") if isinstance(res, dict) else "unknown"
                    failures.append(f"{name}: {err}")
                    tool_results_dump.append(
                        res
                        if isinstance(res, dict)
                        else {"tool_name": name, "success": False, "error": err}
                    )

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

        # Phase 4: access audit.
        audit_prior = prior.get("access_audit") if "access_audit" in skip else None
        audit_dump = await workflow.execute_activity(
            _activities.audit_activity_v2,
            args=[job_id, agent_id, manifest_path, access_tier_str, tool_results_dump, audit_prior],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=PHASE_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )

        # Phase 5: documentation.
        workspace_path = "/workspace"
        if environment_dump:
            workspace_path = environment_dump.get("workspace_path") or "/workspace"
        doc_prior = prior.get("documentation") if "documentation" in skip else None
        doc_result = await workflow.execute_activity(
            _activities.documentation_activity_v2,
            args=[
                job_id,
                agent_id,
                manifest_path,
                access_tier_str,
                credentials_by_tool,
                tool_results_dump,
                workspace_path,
                doc_prior,
            ],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=PHASE_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )
        onboarding_dump = doc_result.get("onboarding") if doc_result else None

        # Phase 6: deliver + final job_store update.
        await workflow.execute_activity(
            _activities.deliver_activity_v2,
            args=[
                job_id,
                agent_id,
                environment_dump,
                credentials_by_tool,
                tool_results_dump,
                audit_dump,
                onboarding_dump,
            ],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=PHASE_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )
