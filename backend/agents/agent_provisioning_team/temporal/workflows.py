"""Temporal workflows for the Agent Provisioning team.

``AgentProvisioningWorkflow`` decomposes provisioning into per-phase activities
and fans out tool provisioning in parallel via ``asyncio.gather``.
``AgentDeprovisioningWorkflow`` tears down one agent as a single activity.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

# ``agent_provisioning_team.temporal`` package ``__init__`` has import-time
# side effects (Pattern A worker boot), so TASK_QUEUE — despite being a plain
# string — must stay inside the pass-through block with the other package imports.
with workflow.unsafe.imports_passed_through():
    from agent_provisioning_team.temporal import activities as _activities
    from agent_provisioning_team.temporal.constants import DEFAULT_WORKSPACE_PATH, TASK_QUEUE

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
    """Per-phase activities with parallel per-tool fan-out."""

    async def _run_tool_provisioning_phase(
        self,
        job_id: str,
        agent_id: str,
        manifest_path: str,
        tool_names: list[str],
        credentials_by_tool: dict[str, dict[str, Any]],
        skip: set[str],
        prior: dict[str, Any],
    ) -> tuple[list[dict], list[dict], list[str]]:
        """Fan out per-tool provision activities, or restore a prior phase dump.

        Preconditions:
            * ``tool_names`` are the manifest tool names in order.
            * ``credentials_by_tool`` is keyed by tool name.
        Postconditions:
            * Returns ``(tool_results_dump, succeeded, failures)``.
            * ``succeeded`` entries carry ``tool_name`` + ``provisioner_key``.
        """
        if "account_provisioning" in skip and prior.get("account_provisioning"):
            # Whole-phase skip (no per-tool resume) matches the prior resume contract.
            ap = prior["account_provisioning"]
            tool_results_dump = list(ap.get("tool_results") or [])
            prior_names = {r.get("tool_name") for r in tool_results_dump if r.get("tool_name")}
            current_names = set(tool_names)
            if prior_names != current_names:
                raise RuntimeError(
                    "Cannot restore account_provisioning: prior tool set "
                    f"{sorted(prior_names)} does not match current manifest "
                    f"{sorted(current_names)}. Restart the job or align the manifest."
                )
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
            return tool_results_dump, succeeded, failures

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

        # Carry the registry key with each success so compensation can look the
        # provisioner back up by provisioner_key.
        succeeded = []
        failures = []
        tool_results_dump = []
        for name, res in zip(tool_names, raw_results):
            if isinstance(res, BaseException):
                failures.append(f"{name}: {res}")
                tool_results_dump.append({"tool_name": name, "success": False, "error": str(res)})
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
                    res if isinstance(res, dict) else {"tool_name": name, "success": False, "error": err}
                )
        return tool_results_dump, succeeded, failures

    async def _compensate_failed_tools(self, agent_id: str, succeeded: list[dict]) -> None:
        """Roll back tools that succeeded when the account-provisioning phase fails.

        Preconditions:
            * ``succeeded`` entries are ``{tool_name, provisioner_key}`` dicts.
        Postconditions:
            * Invokes ``compensate_activity`` once for the partial success set.
        """
        await workflow.execute_activity(
            _activities.compensate_activity,
            args=[agent_id, succeeded],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=PHASE_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )

    async def _mark_job_failed(self, job_id: str, error: str) -> None:
        """Persist a terminal failed status for ``job_id`` before aborting the workflow."""
        await workflow.execute_activity(
            _activities.mark_job_failed_activity,
            args=[job_id, error],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=PHASE_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )

    async def _record_account_provisioning(
        self, job_id: str, agent_id: str, tool_results_dump: list[dict]
    ) -> None:
        """Checkpoint successful tool results so later-phase failures can resume."""
        await workflow.execute_activity(
            _activities.record_account_provisioning_activity,
            args=[job_id, tool_results_dump, agent_id],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=PHASE_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )

    @workflow.run
    async def run(
        self,
        job_id: str,
        agent_id: str,
        manifest_path: str,
        skip_phases: list[str] | None = None,
        prior_results: dict[str, Any] | None = None,
    ) -> None:
        """Run the full provisioning pipeline as durable per-phase activities.

        Preconditions:
            * ``job_id``, ``agent_id``, and ``manifest_path`` are non-empty.
            * When ``skip_phases`` is set, each entry is a phase value string and
              ``prior_results`` contains serializable dumps for those phases
              (including ``account_provisioning.tool_results`` when that phase
              is skipped).
        Postconditions:
            * On success: all phases ran (or were restored), and
              ``deliver_activity`` has written a terminal completed/failed job
              status.
            * On any unhandled phase failure (setup, credentials, tools, audit,
              docs, deliver): ``mark_job_failed_activity`` records terminal
              failure before the exception propagates (tool failures also
              compensate succeeded tools first).
            * After a successful tool fan-out (not a restored skip),
              ``account_provisioning`` is written to ``completed_phases`` /
              ``phase_results`` before later phases run.
        Invariants:
            * One Temporal workflow id per ``job_id`` (starter uses a stable
              prefix); resume/restart mint a new run with skip/prior args rather
              than relying on history drain.
        """
        assert job_id, "job_id must be non-empty"
        assert agent_id, "agent_id must be non-empty"
        assert manifest_path, "manifest_path must be non-empty"

        skip = set(skip_phases or [])
        prior = prior_results or {}
        terminal_failure_recorded = False
        setup_completed = False
        tools_phase_compensated = False
        account_provisioning_done = False

        try:
            # Phase 1: setup (Docker environment).
            setup_prior = prior.get("setup") if "setup" in skip else None
            setup_result = await workflow.execute_activity(
                _activities.setup_activity,
                args=[job_id, agent_id, manifest_path, setup_prior],
                task_queue=TASK_QUEUE,
                schedule_to_close_timeout=PHASE_TIMEOUT,
                retry_policy=DEFAULT_RETRY_POLICY,
            )
            environment_dump = setup_result.get("environment") if setup_result else None
            setup_completed = True

            # Phase 2: credential generation.
            creds_prior = (
                prior.get("credential_generation") if "credential_generation" in skip else None
            )
            creds_result = await workflow.execute_activity(
                _activities.credentials_activity,
                args=[job_id, agent_id, manifest_path, creds_prior],
                task_queue=TASK_QUEUE,
                schedule_to_close_timeout=PHASE_TIMEOUT,
                retry_policy=DEFAULT_RETRY_POLICY,
            )
            credentials_by_tool: dict[str, dict[str, Any]] = creds_result["credentials"]

            # Phase 3: fan out per-tool provisioning (or restore from prior).
            tool_names = await workflow.execute_activity(
                _activities.list_manifest_tools_activity,
                args=[manifest_path],
                task_queue=TASK_QUEUE,
                schedule_to_close_timeout=PHASE_TIMEOUT,
                retry_policy=DEFAULT_RETRY_POLICY,
            )
            tool_results_dump, succeeded, failures = await self._run_tool_provisioning_phase(
                job_id,
                agent_id,
                manifest_path,
                tool_names,
                credentials_by_tool,
                skip,
                prior,
            )

            if failures:
                await self._compensate_failed_tools(agent_id, succeeded)
                tools_phase_compensated = True
                err = f"Tool provisioning failed for agent {agent_id}: {'; '.join(failures)}"
                await self._mark_job_failed(job_id, err)
                terminal_failure_recorded = True
                raise RuntimeError(err)

            if "account_provisioning" not in skip:
                await self._record_account_provisioning(job_id, agent_id, tool_results_dump)
            account_provisioning_done = True

            # Phase 4: access audit.
            audit_prior = prior.get("access_audit") if "access_audit" in skip else None
            audit_dump = await workflow.execute_activity(
                _activities.audit_activity,
                args=[job_id, agent_id, manifest_path, tool_results_dump, audit_prior],
                task_queue=TASK_QUEUE,
                schedule_to_close_timeout=PHASE_TIMEOUT,
                retry_policy=DEFAULT_RETRY_POLICY,
            )

            # Phase 5: documentation.
            workspace_path = DEFAULT_WORKSPACE_PATH
            if environment_dump:
                workspace_path = environment_dump.get("workspace_path") or DEFAULT_WORKSPACE_PATH
            doc_prior = prior.get("documentation") if "documentation" in skip else None
            doc_result = await workflow.execute_activity(
                _activities.documentation_activity,
                args=[
                    job_id,
                    agent_id,
                    manifest_path,
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
                _activities.deliver_activity,
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
        except Exception as exc:
            # Credentials / manifest-list failures after setup leave a Docker env
            # (+ maybe credentials) behind; compensate([]) tears those down —
            # same as the in-process orchestrator. Tool failures already
            # compensated above.
            if setup_completed and not account_provisioning_done and not tools_phase_compensated:
                await self._compensate_failed_tools(agent_id, [])
            if not terminal_failure_recorded:
                await self._mark_job_failed(job_id, f"Provisioning failed: {exc}")
            raise


@workflow.defn(name="AgentDeprovisioningWorkflow")
class AgentDeprovisioningWorkflow:
    """Deprovision one agent's resources as a single durable activity.

    The teardown counterpart to :class:`AgentProvisioningWorkflow`. Dispatched
    execute-and-wait from the ``DELETE /environments/{agent_id}`` handler so the
    HTTP response is the workflow's result.

    Invariants:
        * Runs exactly one activity — the orchestrator's existing best-effort
          deprovision — so the whole teardown is retried atomically on
          infrastructure failure.
    """

    @workflow.run
    async def run(self, agent_id: str, force: bool = False) -> dict[str, Any]:
        """Execute deprovision and return the ``DeprovisionResponse`` dump.

        Preconditions:
            * ``agent_id`` is non-empty.
        Postconditions:
            * Returns the ``DeprovisionResponse.model_dump()`` produced by
              ``deprovision_activity``.
        """
        assert agent_id, "agent_id must be non-empty"
        return await workflow.execute_activity(
            _activities.deprovision_activity,
            args=[agent_id, force],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=PHASE_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )
