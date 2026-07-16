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
    """Durable Temporal workflow that orchestrates agent provisioning end-to-end.

    Runs one job through setup → credentials → parallel per-tool provision →
    audit → documentation → deliver. Resume/restart pass ``skip_phases`` /
    ``prior_results`` rather than replaying history.

    Invariants:
        * Stable Temporal workflow id per ``job_id`` (starter prefix).
        * Tool fan-out uses ``asyncio.gather``; checkpoint + env-store updates
          happen once after the gather succeeds.
        * Failures after setup but before account-provisioning success
          compensate (possibly with an empty tool list) before marking failed.
    """

    @staticmethod
    def _restore_account_provisioning_from_prior(
        ap: dict[str, Any],
        tool_names: list[str],
    ) -> tuple[list[dict], list[dict], list[str]]:
        """Rebuild tool phase results from a prior ``account_provisioning`` dump.

        Preconditions:
            * ``ap`` is the prior ``account_provisioning`` phase payload.
            * ``tool_names`` are the current manifest tool names in order.
        Postconditions:
            * Returns ``(tool_results_dump, succeeded, failures)``.
        """
        tool_results_dump = list(ap.get("tool_results") or [])
        prior_names = {r.get("tool_name") for r in tool_results_dump if r.get("tool_name")}
        current_names = set(tool_names)
        succeeded: list[dict] = [
            {
                "tool_name": r.get("tool_name"),
                "provisioner_key": r.get("provisioner_key"),
            }
            for r in tool_results_dump
            if r.get("success")
        ]
        if prior_names != current_names:
            return (
                tool_results_dump,
                succeeded,
                [
                    "Cannot restore account_provisioning: prior tool set "
                    f"{sorted(prior_names)} does not match current manifest "
                    f"{sorted(current_names)}. Restart the job or align the manifest."
                ],
            )
        failures: list[str] = [
            f"{r.get('tool_name')}: {r.get('error')}"
            for r in tool_results_dump
            if not r.get("success")
        ]
        return tool_results_dump, succeeded, failures

    @staticmethod
    def _merge_enriched_credentials(
        credentials_by_tool: dict[str, dict[str, Any]], tool_results_dump: list[dict]
    ) -> dict[str, dict[str, Any]]:
        """Merge provisioner-enriched credential fields back into the credential map.

        Provisioners may mutate ``GeneratedCredentials`` during per-tool provision
        (for example connection strings or SSH key material). Documentation and
        deliver need those enriched values, while the raw credential-generation
        snapshot only contains the pre-provision fields.

        Preconditions:
            * ``credentials_by_tool`` is keyed by tool name.
            * ``tool_results_dump`` entries are serializable tool-result dumps.
        Postconditions:
            * Returns a new mapping where any successful tool result carrying a
              ``credentials`` dump overlays that dump onto the same tool key.
            * Tools without enriched credentials preserve their original entry.
        """
        merged = {name: dict(payload) for name, payload in credentials_by_tool.items()}
        for result in tool_results_dump:
            if not isinstance(result, dict) or not result.get("success"):
                continue
            tool_name = result.get("tool_name")
            creds = result.get("credentials")
            if not tool_name or not isinstance(creds, dict):
                continue
            base = dict(merged.get(tool_name, {}))
            base.update(creds)
            merged[tool_name] = base
        return merged

    async def _run_tool_provisioning_phase(
        self,
        job_id: str,
        agent_id: str,
        tool_specs: list[dict],
        credentials_by_tool: dict[str, dict[str, Any]],
        skip: set[str],
        prior: dict[str, Any],
    ) -> tuple[list[dict], list[dict], list[str]]:
        """Fan out per-tool provision activities, or restore a prior phase dump.

        Preconditions:
            * ``tool_specs`` are ``{name, provisioner, config}`` dicts in order.
            * ``credentials_by_tool`` is keyed by tool name.
        Postconditions:
            * Returns ``(tool_results_dump, succeeded, failures)``.
            * ``succeeded`` entries carry ``tool_name`` + ``provisioner_key``.
        """
        tool_names = [s["name"] for s in tool_specs]
        if "account_provisioning" in skip and prior.get("account_provisioning"):
            return self._restore_account_provisioning_from_prior(
                prior["account_provisioning"],
                tool_names,
            )

        tools_total = len(tool_specs)

        async def _one(spec: dict) -> Any:
            tool_name = spec["name"]
            creds_dump = credentials_by_tool.get(tool_name, {})
            return await workflow.execute_activity(
                _activities.provision_tool_activity,
                args=[
                    job_id,
                    agent_id,
                    tool_name,
                    creds_dump,
                    tools_total,
                    spec["provisioner"],
                    spec.get("config") or {},
                ],
                task_queue=TASK_QUEUE,
                start_to_close_timeout=TOOL_ACTIVITY_TIMEOUT,
                heartbeat_timeout=TOOL_HEARTBEAT_TIMEOUT,
                retry_policy=TOOL_RETRY_POLICY,
            )

        raw_results = await asyncio.gather(
            *[_one(spec) for spec in tool_specs],
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
                err = (res.get("error") if isinstance(res, dict) else None) or "unknown"
                failures.append(f"{name}: {err}")
                tool_results_dump.append(
                    res if isinstance(res, dict) else {"tool_name": name, "success": False, "error": err}
                )
        return tool_results_dump, succeeded, failures

    async def _compensate_failed_tools(
        self, agent_id: str, succeeded: list[dict], job_id: str
    ) -> None:
        """Roll back tools that succeeded when the account-provisioning phase fails.

        Preconditions:
            * ``succeeded`` entries are ``{tool_name, provisioner_key}`` dicts.
            * ``job_id`` is non-empty (used to clear completed-phase checkpoints).
        Postconditions:
            * Invokes ``compensate_activity`` once for the partial success set.
        """
        await workflow.execute_activity(
            _activities.compensate_activity,
            args=[agent_id, succeeded, job_id],
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

    async def _execute_setup_phase(
        self,
        job_id: str,
        agent_id: str,
        manifest_path: str,
        skip: set[str],
        prior: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Run or restore setup; return ``(setup_result, environment_dump)``."""
        setup_prior = prior.get("setup") if "setup" in skip else None
        setup_result = await workflow.execute_activity(
            _activities.setup_activity,
            args=[job_id, agent_id, manifest_path, setup_prior],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=PHASE_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )
        environment_dump = setup_result.get("environment") if setup_result else None
        return setup_result, environment_dump

    async def _execute_credentials_phase(
        self,
        job_id: str,
        agent_id: str,
        manifest_path: str,
        skip: set[str],
        prior: dict[str, Any],
        tool_specs: list[dict[str, Any]] | None,
    ) -> dict[str, dict[str, Any]]:
        """Run or restore credential generation; return credentials keyed by tool."""
        creds_prior = prior.get("credential_generation") if "credential_generation" in skip else None
        creds_result = await workflow.execute_activity(
            _activities.credentials_activity,
            args=[job_id, agent_id, manifest_path, creds_prior, tool_specs],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=PHASE_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )
        return creds_result["credentials"]

    async def _execute_audit_phase(
        self,
        job_id: str,
        agent_id: str,
        manifest_path: str,
        tool_results_dump: list[dict],
        skip: set[str],
        prior: dict[str, Any],
    ) -> Any:
        """Run or restore access audit."""
        audit_prior = prior.get("access_audit") if "access_audit" in skip else None
        return await workflow.execute_activity(
            _activities.audit_activity,
            args=[job_id, agent_id, manifest_path, tool_results_dump, audit_prior],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=PHASE_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )

    async def _execute_documentation_phase(
        self,
        job_id: str,
        agent_id: str,
        manifest_path: str,
        credentials_by_tool: dict[str, dict[str, Any]],
        tool_results_dump: list[dict],
        workspace_path: str,
        skip: set[str],
        prior: dict[str, Any],
    ) -> Any:
        """Run or restore documentation generation."""
        doc_prior = prior.get("documentation") if "documentation" in skip else None
        return await workflow.execute_activity(
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

    async def _execute_deliver_phase(
        self,
        job_id: str,
        agent_id: str,
        environment_dump: dict[str, Any] | None,
        credentials_by_tool: dict[str, dict[str, Any]],
        tool_results_dump: list[dict],
        audit_dump: Any,
        onboarding_dump: Any,
    ) -> None:
        """Run deliver and final job-store terminal update."""
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
              compensate succeeded tools first). Credentials / manifest-list
              failures after setup call ``compensate_activity`` with an empty
              succeeded list to tear down the Docker env / credentials.
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
        succeeded_tools: list[dict] = []

        try:
            _, environment_dump = await self._execute_setup_phase(
                job_id, agent_id, manifest_path, skip, prior
            )
            setup_completed = True

            # Freeze manifest tools once for credential + provision phases so a
            # mid-run file edit cannot change the tool set under us.
            tool_specs = await workflow.execute_activity(
                _activities.list_manifest_tools_activity,
                args=[manifest_path],
                task_queue=TASK_QUEUE,
                schedule_to_close_timeout=PHASE_TIMEOUT,
                retry_policy=DEFAULT_RETRY_POLICY,
            )

            credentials_by_tool = await self._execute_credentials_phase(
                job_id, agent_id, manifest_path, skip, prior, tool_specs
            )

            tool_results_dump, succeeded, failures = await self._run_tool_provisioning_phase(
                job_id,
                agent_id,
                tool_specs,
                credentials_by_tool,
                skip,
                prior,
            )
            succeeded_tools = list(succeeded)
            credentials_by_tool = self._merge_enriched_credentials(credentials_by_tool, tool_results_dump)

            if failures:
                await self._compensate_failed_tools(agent_id, succeeded, job_id)
                tools_phase_compensated = True
                err = f"Tool provisioning failed for agent {agent_id}: {'; '.join(failures)}"
                await self._mark_job_failed(job_id, err)
                terminal_failure_recorded = True
                raise RuntimeError(err)

            if "account_provisioning" not in skip:
                await self._record_account_provisioning(job_id, agent_id, tool_results_dump)
            account_provisioning_done = True

            audit_dump = await self._execute_audit_phase(
                job_id, agent_id, manifest_path, tool_results_dump, skip, prior
            )

            workspace_path = DEFAULT_WORKSPACE_PATH
            if environment_dump:
                workspace_path = environment_dump.get("workspace_path") or DEFAULT_WORKSPACE_PATH
            doc_result = await self._execute_documentation_phase(
                job_id,
                agent_id,
                manifest_path,
                credentials_by_tool,
                tool_results_dump,
                workspace_path,
                skip,
                prior,
            )
            onboarding_dump = doc_result.get("onboarding") if doc_result else None

            await self._execute_deliver_phase(
                job_id,
                agent_id,
                environment_dump,
                credentials_by_tool,
                tool_results_dump,
                audit_dump,
                onboarding_dump,
            )
        except Exception as exc:
            # Pre-tool failures leave Docker/credentials behind → compensate([]).
            # Fan-out completed but checkpoint/later phase not durable yet →
            # compensate the tools that already succeeded.
            # Nested try/except: compensation / terminal writes must not mask
            # the original failure if Temporal activity retries are exhausted.
            if setup_completed and not account_provisioning_done and not tools_phase_compensated:
                try:
                    await self._compensate_failed_tools(agent_id, succeeded_tools, job_id)
                except Exception as comp_exc:
                    workflow.logger.error(
                        "Compensation failed after provisioning error for job=%s: %s (original=%s)",
                        job_id,
                        comp_exc,
                        exc,
                    )
            if not terminal_failure_recorded:
                try:
                    await self._mark_job_failed(job_id, f"Provisioning failed: {exc}")
                except Exception as mark_exc:
                    workflow.logger.error(
                        "mark_job_failed failed after provisioning error for job=%s: %s (original=%s)",
                        job_id,
                        mark_exc,
                        exc,
                    )
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
