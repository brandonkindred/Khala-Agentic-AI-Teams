"""
Provisioning Orchestrator: Coordinates the phase-based provisioning workflow.

Executes phases sequentially with progress callbacks for real-time tracking.
"""

import logging
import threading
from typing import Any, Callable, Dict, List, Optional

from .models import (
    DeprovisionCancelledError,
    DeprovisionResponse,
    Phase,
    ProvisioningResult,
)
from .phases.access_audit import run_access_audit
from .phases.account_provisioning import deprovision_tools, run_account_provisioning
from .phases.credential_generation import run_credential_generation
from .phases.deliver import build_final_result, run_deliver
from .phases.documentation import run_documentation
from .phases.setup import cleanup_setup, run_setup
from .shared.credential_store import CredentialStore
from .shared.environment_queries import get_agent_status_dict, list_agent_status_dicts
from .shared.environment_store import EnvironmentStore
from .shared.fencing import StaleFencingTokenError
from .shared.logging_context import install_filter as _install_log_filter
from .shared.phase_state import (
    restore_access_audit,
    restore_account_provisioning,
    restore_credentials,
    restore_documentation,
    restore_setup,
)
from .shared.tool_agent_registry import build_default_tool_agents
from .shared.tool_manifest import load_manifest

_install_log_filter()
logger = logging.getLogger(__name__)

JobUpdater = Callable[..., None]


class ProvisioningShutdownError(Exception):
    """Raised when the provisioning workflow is cancelled mid-flight
    because the FastAPI app is shutting down. After raising, the orchestrator
    has already invoked `compensate()` to roll back partial state."""

    def __init__(self, agent_id: str, phase: str) -> None:
        self.agent_id = agent_id
        self.phase = phase
        super().__init__(f"Provisioning for {agent_id} cancelled during {phase}")


class ProvisioningOrchestrator:
    """In-process phase engine used by Temporal activities and tests.

    HTTP provision/resume/restart/deprovision go through Temporal only — that
    path is the production source of truth for phase ordering, compensation,
    and durable checkpoints.

    Temporal activities call the shared phase functions (and ``compensate`` /
    ``deprovision``) rather than ``run_workflow``. ``run_workflow`` remains a
    sequential in-process coordinator for unit tests and any non-HTTP callers
    that need the same phase *functions* with a ``shutdown_event`` / progress
    callback. Do not extend ``run_workflow`` with behavior that the Temporal
    workflow does not also implement; prefer changing shared phase modules so
    both paths stay aligned.
    """

    def __init__(
        self,
        credential_store: Optional[CredentialStore] = None,
        environment_store: Optional[EnvironmentStore] = None,
        tool_agents: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.credential_store = credential_store or CredentialStore()
        self.environment_store = environment_store or EnvironmentStore()
        self.tool_agents = tool_agents or build_default_tool_agents()

    def run_workflow(
        self,
        agent_id: str,
        manifest_path: str,
        job_updater: Optional[JobUpdater] = None,
        skip_phases: Optional[set] = None,
        prior_results: Optional[Dict[str, Any]] = None,
        shutdown_event: Optional[threading.Event] = None,
        *,
        fencing_token: Optional[int] = None,
    ) -> ProvisioningResult:
        """
        Execute the full provisioning workflow through all phases.

        Args:
            agent_id: Unique identifier for the agent being provisioned
            manifest_path: Path to the tool manifest YAML
            job_updater: Optional callback for progress updates.
            skip_phases: Set of Phase values to skip (already completed on a prior run).
            prior_results: Dict of phase results from a prior run keyed by phase value string.
            shutdown_event: Optional threading.Event that signals cooperative
                cancellation at phase boundaries. When set, the orchestrator
                compensates and raises ProvisioningShutdownError.
            fencing_token: Caller's fencing token (see ``shared.fencing``);
                ``None`` skips enforcement. Threaded into every phase call
                and into ``compensate()`` so a stale caller's mutations are
                rejected exactly as they would be on the Temporal path.

        Returns:
            ProvisioningResult with complete provisioning information
        """
        skip_phases = skip_phases or set()
        prior_results = prior_results or {}
        # Bind correlation IDs onto contextvars so every log line from
        # here down carries agent_id / phase via the logging filter.
        # The orchestrator is typically invoked inside its own background
        # thread, so the contextvar binding is isolated per-run.
        from .shared.logging_context import _agent_id_var, _phase_var

        _agent_id_var.set(agent_id)
        _phase_var.set("init")
        if skip_phases:
            logger.info(
                "Resuming workflow — skipping completed phases: %s", [p.value for p in skip_phases]
            )

        # Tracks the latest tool_results the orchestrator has produced, so a
        # shutdown check mid-workflow can pass them to `compensate()` to
        # deprovision any tools that succeeded before cancellation.
        tool_results_ref: List[Any] = []

        def _set_phase(name: str) -> None:
            _phase_var.set(name)

        def _check_shutdown(phase_name: str) -> None:
            if shutdown_event is not None and shutdown_event.is_set():
                logger.warning(
                    "Shutdown signalled for agent=%s during %s; compensating",
                    agent_id,
                    phase_name,
                )
                self.compensate(agent_id, tool_results_ref, fencing_token=fencing_token)
                raise ProvisioningShutdownError(agent_id=agent_id, phase=phase_name)

        def _update(
            current_phase: Optional[str] = None,
            progress: Optional[int] = None,
            current_tool: Optional[str] = None,
            tools_completed: Optional[int] = None,
            tools_total: Optional[int] = None,
            status_text: Optional[str] = None,
        ) -> None:
            if job_updater:
                job_updater(
                    current_phase=current_phase,
                    progress=progress,
                    current_tool=current_tool,
                    tools_completed=tools_completed,
                    tools_total=tools_total,
                    status_text=status_text,
                )

        try:
            manifest = load_manifest(manifest_path)
        except Exception as e:
            return ProvisioningResult(
                agent_id=agent_id,
                current_phase=Phase.SETUP,
                success=False,
                error=f"Failed to load manifest: {str(e)}",
            )

        # -- SETUP --
        _set_phase(Phase.SETUP.value)
        _check_shutdown(Phase.SETUP.value)
        if Phase.SETUP in skip_phases and prior_results.get("setup"):
            setup_result = restore_setup(prior_results["setup"])
            logger.info("Skipping SETUP (already completed)")
        else:
            _update(
                current_phase=Phase.SETUP.value,
                progress=5,
                status_text="Creating Docker environment...",
            )
            setup_result = run_setup(
                agent_id=agent_id,
                manifest=manifest,
                environment_store=self.environment_store,
                docker_provisioner=self.tool_agents.get("docker_provisioner"),
                progress_callback=lambda msg: _update(status_text=msg),
                fencing_token=fencing_token,
            )
            if not setup_result.success:
                return ProvisioningResult(
                    agent_id=agent_id,
                    current_phase=Phase.SETUP,
                    completed_phases=[],
                    success=False,
                    error=setup_result.error or "Setup failed",
                )

        # -- CREDENTIAL_GENERATION --
        _set_phase(Phase.CREDENTIAL_GENERATION.value)
        _check_shutdown(Phase.CREDENTIAL_GENERATION.value)
        if Phase.CREDENTIAL_GENERATION in skip_phases and prior_results.get(
            "credential_generation"
        ):
            cred_result = restore_credentials(prior_results["credential_generation"])
            logger.info("Skipping CREDENTIAL_GENERATION (already completed)")
        else:
            _update(
                current_phase=Phase.CREDENTIAL_GENERATION.value,
                progress=20,
                status_text="Generating credentials...",
            )
            cred_result = run_credential_generation(
                agent_id=agent_id,
                manifest=manifest,
                credential_store=self.credential_store,
                progress_callback=lambda tool, done, total: _update(
                    current_tool=tool,
                    tools_completed=done,
                    tools_total=total,
                    status_text=f"Generating credentials for {tool}...",
                ),
                fencing_token=fencing_token,
            )
            if not cred_result.success:
                cleanup_setup(agent_id, self.environment_store, fencing_token=fencing_token)
                return ProvisioningResult(
                    agent_id=agent_id,
                    current_phase=Phase.CREDENTIAL_GENERATION,
                    completed_phases=[Phase.SETUP],
                    environment=setup_result.environment,
                    success=False,
                    error=cred_result.error or "Credential generation failed",
                )

        # -- ACCOUNT_PROVISIONING --
        _set_phase(Phase.ACCOUNT_PROVISIONING.value)
        _check_shutdown(Phase.ACCOUNT_PROVISIONING.value)
        if Phase.ACCOUNT_PROVISIONING in skip_phases and prior_results.get("account_provisioning"):
            account_result = restore_account_provisioning(prior_results["account_provisioning"])
            logger.info("Skipping ACCOUNT_PROVISIONING (already completed)")
        else:
            _update(
                current_phase=Phase.ACCOUNT_PROVISIONING.value,
                progress=35,
                tools_total=len(manifest.tools),
                status_text="Provisioning tool accounts...",
            )
            account_result = run_account_provisioning(
                agent_id=agent_id,
                manifest=manifest,
                credentials=cred_result.credentials,
                provisioners=self.tool_agents,
                environment_store=self.environment_store,
                progress_callback=lambda done, total, tool: _update(
                    current_tool=tool,
                    tools_completed=done,
                    tools_total=total,
                    progress=35 + int((done / max(total, 1)) * 30),
                    status_text=f"Provisioning {tool}...",
                ),
                fencing_token=fencing_token,
            )

            # Compensation: if any tool failed, roll back already-provisioned
            # tools and the Docker setup so we don't leak resources or
            # encrypted credentials for a half-finished agent.
            if not account_result.success:
                logger.error(
                    "ACCOUNT_PROVISIONING failed for agent=%s: %s — rolling back",
                    agent_id,
                    account_result.error,
                )
                self.compensate(agent_id, account_result.tool_results, fencing_token=fencing_token)
                return ProvisioningResult(
                    agent_id=agent_id,
                    current_phase=Phase.ACCOUNT_PROVISIONING,
                    completed_phases=[Phase.SETUP, Phase.CREDENTIAL_GENERATION],
                    environment=setup_result.environment,
                    success=False,
                    error=account_result.error or "Account provisioning failed",
                )

            tool_results_ref[:] = list(account_result.tool_results or [])

        # -- ACCESS_AUDIT --
        _set_phase(Phase.ACCESS_AUDIT.value)
        _check_shutdown(Phase.ACCESS_AUDIT.value)
        if Phase.ACCESS_AUDIT in skip_phases and prior_results.get("access_audit"):
            audit_result = restore_access_audit(prior_results["access_audit"])
            logger.info("Skipping ACCESS_AUDIT (already completed)")
        else:
            _update(
                current_phase=Phase.ACCESS_AUDIT.value,
                progress=70,
                status_text="Auditing access permissions...",
            )
            audit_result = run_access_audit(
                agent_id=agent_id,
                tool_results=account_result.tool_results,
                progress_callback=lambda msg: _update(status_text=msg),
            )

        # -- DOCUMENTATION --
        _set_phase(Phase.DOCUMENTATION.value)
        _check_shutdown(Phase.DOCUMENTATION.value)
        if Phase.DOCUMENTATION in skip_phases and prior_results.get("documentation"):
            doc_result = restore_documentation(prior_results["documentation"])
            logger.info("Skipping DOCUMENTATION (already completed)")
        else:
            _update(
                current_phase=Phase.DOCUMENTATION.value,
                progress=85,
                status_text="Generating onboarding documentation...",
            )
            workspace_path = (
                setup_result.environment.workspace_path
                if hasattr(setup_result, "environment") and setup_result.environment
                else "/workspace"
            )
            doc_result = run_documentation(
                agent_id=agent_id,
                manifest=manifest,
                credentials=cred_result.credentials,
                tool_results=account_result.tool_results,
                workspace_path=workspace_path,
                progress_callback=lambda msg: _update(status_text=msg),
            )

        # -- DELIVER --
        _set_phase(Phase.DELIVER.value)
        _check_shutdown(Phase.DELIVER.value)
        _update(
            current_phase=Phase.DELIVER.value, progress=95, status_text="Finalizing provisioning..."
        )
        deliver_result = run_deliver(
            agent_id=agent_id,
            environment=setup_result.environment,
            credentials=cred_result.credentials,
            tool_results=account_result.tool_results,
            access_audit=audit_result,
            onboarding=doc_result.onboarding,
            environment_store=self.environment_store,
            progress_callback=lambda msg: _update(status_text=msg),
            fencing_token=fencing_token,
        )

        final_result = build_final_result(
            agent_id=agent_id,
            environment=setup_result.environment,
            credentials=cred_result.credentials,
            tool_results=account_result.tool_results,
            access_audit=audit_result,
            onboarding=doc_result.onboarding,
            deliver_result=deliver_result,
        )

        _update(
            current_phase=Phase.DELIVER.value, progress=100, status_text="Provisioning complete"
        )
        return final_result

    def compensate(
        self,
        agent_id: str,
        tool_results: List[Any],
        tear_down_environment: bool = True,
        *,
        fencing_token: Optional[int] = None,
    ) -> None:
        """Roll back partial provisioning after a phase failure.

        Public entry point for Temporal ``compensate_activity`` and in-process
        shutdown compensation. Best-effort: deprovisions any tools that did
        succeed, tears down the Docker environment, and removes encrypted
        credentials so a failed run doesn't leak resources or secrets to disk.

        ``fencing_token``, when given, is checked per-resource (each
        provisioner tracks its own high-water mark independently) rather
        than once for the whole call: a stale token for one resource does
        not imply the others have moved on too, since a replacement owner
        may not have touched every resource yet. A rejected resource is
        logged and skipped — consistent with this method's existing
        best-effort, one-failure-never-blocks-the-rest idiom — rather than
        aborting the rest of compensation.

        Preconditions:
            * ``tool_results`` entries are the tool results this ATTEMPT
              itself produced — rolling those back always runs (except for
              entries whose ``details.reused`` is true — see below),
              regardless of ``tear_down_environment``.
        Postconditions:
            * A result whose ``details.reused`` is true is skipped entirely:
              ``reused`` means the provisioner found and idempotently reused
              an existing account rather than creating a new one, so it is
              never this attempt's own creation — rolling it back (or
              purging its credential entry) would destroy/invalidate a
              resource that predates this attempt, independent of
              ``tear_down_environment``.
            * Every other tool has its ``details.reused``-derived rollback
              attempted (replay-compensation or ``deprovision``, whichever
              applies), and only when that rollback CONFIRMS success — a
              replay with no individual step failures, or a
              ``DeprovisionResult(success=True)`` — is its generated
              credential entry purged from the credential store
              (``CredentialStore.delete_tool_credentials``). Rollback
              "not raising" is not enough: ``deprovision()`` commonly reports
              failure via ``DeprovisionResult(success=False)`` rather than an
              exception, and a replay step can fail without escaping its own
              try/except — purging the credential in either case would strip
              the only remaining way to reach an account that may still be
              live. The credentials phase generates a fresh secret for every
              tool upfront, so once (and only once) a tool's account is
              confirmed torn back down, that secret no longer corresponds to
              anything live and is safe to discard.
            * When a replay step fails, the provisioner's persisted
              compensation records and idempotency state row are left
              intact rather than cleared — clearing them would make an
              un-replayed (or partially-applied) step's side effect
              permanently unreachable by a future retry of this same
              method; ``list_compensations`` must still return every
              record next time so the retry can pick up where this
              attempt left off.
            * ``tear_down_environment=False`` skips the Docker / whole-agent
              credential-file / environment-record teardown entirely, while
              tool rollback above still runs unconditionally (modulo the
              ``reused`` exclusion). Callers set this ``False`` when
              ``agent_id``'s environment predates this attempt (e.g. a re-run
              against an already-delivered agent) and must be preserved — a
              newly-provisioned tool from THIS attempt still gets rolled
              back, but the environment this attempt never created is left
              untouched.
            * ``tear_down_environment=True``'s credential-file cleanup is
              NOT an unconditional ``delete_credentials(agent_id)`` when any
              tool was excluded from the per-tool purge above (reused, or an
              attempted rollback that never confirmed success): it instead
              purges every OTHER currently-stored tool's entry individually,
              leaving those excluded ones alone. An unconditional delete
              here would otherwise strip the only remaining way to reach an
              account whose rollback may not have actually landed, even
              though the environment/container around it is being torn
              down — that account is frequently an external resource (e.g.
              a database), not something destroying the container alone
              would also destroy.
            * Whether it is safe to independently reclaim an
              orphaned container by name afterward (e.g.
              ``compensate_activity``'s own ``verify_and_remove_orphan``
              follow-up) is not reported here — callers needing that answer
              should check ``EnvironmentStore`` directly rather than infer it
              from how this method's internal steps happened to fare, since
              ``cleanup_setup`` raising does not always mean the record
              survived (e.g. the record removal itself can succeed and a
              later step in the same call still raise).
        """
        # Look each successfully-provisioned tool back up by its registry key
        # (stamped onto the result in run_account_provisioning). Prior to #293
        # this used f"{r.tool_name}_provisioner", which silently missed for
        # provisioners whose class `tool_name` differs from the registry stem
        # (e.g. PostgresProvisionerTool.tool_name == "postgresql" vs key
        # "postgres_provisioner"), leaking accounts + encrypted credentials.
        #
        # Every tool this loop does NOT confirm-and-purge itself (reused, no
        # rollback attempted, or an attempted rollback that didn't confirm
        # success) is tracked here so the tear_down_environment=True cleanup
        # below can exclude it from the credential purge — that cleanup used
        # to be an unconditional delete_credentials(agent_id), which
        # defeated this same preservation the moment the environment (not
        # just an individual tool) was being torn down.
        preserved_credentials: set[str] = set()
        for r in tool_results:
            if not getattr(r, "success", False):
                continue
            tool_name = getattr(r, "tool_name", None)
            if bool((getattr(r, "details", None) or {}).get("reused", False)):
                logger.info(
                    "Compensation: skipping rollback for %s — this attempt reused a "
                    "pre-existing account rather than creating one",
                    tool_name or "?",
                )
                if tool_name:
                    preserved_credentials.add(tool_name)
                continue
            key = getattr(r, "provisioner_key", None)
            if not key:
                logger.warning(
                    "Compensation: tool_result for %s has no provisioner_key; "
                    "skipping rollback (stale result pre-#293).",
                    tool_name or "?",
                )
                if tool_name:
                    preserved_credentials.add(tool_name)
                continue
            provisioner = self.tool_agents.get(key)
            if provisioner is None:
                logger.warning("Compensation: no provisioner registered for key=%s", key)
                if tool_name:
                    preserved_credentials.add(tool_name)
                continue
            if fencing_token is not None:
                try:
                    provisioner._state.check_fencing_token(agent_id, fencing_token)
                except StaleFencingTokenError:
                    logger.warning(
                        "Compensation: stale fencing token for %s; skipping rollback", key
                    )
                    # A stale token means a newer owner already reclaimed this
                    # resource; its rollback is skipped, so its credential must
                    # be PRESERVED (not purged by the tear-down cleanup below) —
                    # the account may still be live under the new owner. Mirrors
                    # every other skip path here (no provisioner, reused).
                    if tool_name:
                        preserved_credentials.add(tool_name)
                    continue
            # Prefer persisted per-step compensations when the provisioner
            # registered any during `_do_provision`: replay in LIFO order,
            # then drop the whole state row. Provisioners that register
            # nothing keep the legacy whole-tool `deprovision()` fallback.
            records: List[Any] = []
            try:
                records = list(provisioner.list_compensations(agent_id))
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Compensation: could not read records for %s; falling back to deprovision",
                    key,
                )
            # Tracks whether rollback actually landed, not merely whether it
            # avoided raising — deprovision() reports many failures via
            # DeprovisionResult(success=False) rather than an exception, and a
            # replay step can individually fail without raising past its own
            # try/except. Only a confirmed-successful rollback should purge
            # the credential entry below; otherwise the account may still be
            # live and the credential is the only way left to reach it.
            rollback_succeeded = False
            if records:
                replay_failed = False
                for rec in reversed(records):
                    try:
                        provisioner.replay_compensation(agent_id, rec.kind, rec.payload)
                    except Exception:  # noqa: BLE001 — best-effort cleanup
                        logger.exception(
                            "Compensation: replay failed kind=%s for %s", rec.kind, key
                        )
                        replay_failed = True
                if replay_failed:
                    # Preserve the persisted records and state row rather
                    # than clearing them: a step that failed to replay left
                    # its side effect (partially) intact, and a Temporal
                    # retry of this whole activity needs list_compensations
                    # to still return every record — including the ones that
                    # DID replay successfully, since replaying an idempotent
                    # step twice is safe but losing track of one that never
                    # ran is not — to finish the rollback. Clearing here
                    # would make that side effect permanently unreachable by
                    # any future compensation attempt.
                    logger.error(
                        "Compensation: preserving compensation records for %s "
                        "(agent_id=%s) after a replay step failed, so a retry can "
                        "still attempt them",
                        key,
                        agent_id,
                    )
                else:
                    try:
                        provisioner.clear_compensations(agent_id, fencing_token=fencing_token)
                        provisioner._state.delete(agent_id, fencing_token=fencing_token)
                        rollback_succeeded = True
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "Compensation: post-replay state cleanup failed for %s", key
                        )
            else:
                try:
                    deprovision_result = provisioner.deprovision(
                        agent_id, fencing_token=fencing_token
                    )
                    rollback_succeeded = bool(getattr(deprovision_result, "success", False))
                    if not rollback_succeeded:
                        logger.error(
                            "Compensation: deprovision reported failure for %s "
                            "(agent_id=%s, error=%s); credential entry preserved since "
                            "the account may still be live",
                            key,
                            agent_id,
                            getattr(deprovision_result, "error", None),
                        )
                except Exception:  # noqa: BLE001 — best-effort cleanup
                    logger.exception("Compensation: deprovision failed for %s", key)

            if tool_name and rollback_succeeded:
                try:
                    self.credential_store.delete_tool_credentials(
                        agent_id, tool_name, fencing_token=fencing_token
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("Compensation: credential cleanup failed for %s", tool_name)
            elif tool_name:
                preserved_credentials.add(tool_name)

        if not tear_down_environment:
            return

        docker = self.tool_agents.get("docker_provisioner")
        if docker is not None:
            try:
                docker.deprovision(agent_id, fencing_token=fencing_token)
            except Exception:  # noqa: BLE001
                logger.exception("Compensation: docker teardown failed")

        try:
            if preserved_credentials:
                # A plain delete_credentials(agent_id) here would defeat the
                # per-tool preservation above the moment the environment
                # itself is also being torn down — purge only the entries
                # NOT deliberately preserved (reused, or a rollback that
                # didn't confirm success) instead of the whole file.
                #
                # list_tool_names (not get_credentials) so a tool name that
                # exists ONLY in a legacy candidate — never migrated to the
                # primary file — still gets enumerated here and purged via
                # delete_tool_credentials below; get_credentials stops at
                # whichever single candidate it reads first and would
                # silently skip a legacy-only tool, leaving its stale
                # credential behind after this same compensation pass.
                stored = self.credential_store.list_tool_names(agent_id)
                for stored_tool_name in stored:
                    if stored_tool_name in preserved_credentials:
                        continue
                    self.credential_store.delete_tool_credentials(
                        agent_id, stored_tool_name, fencing_token=fencing_token
                    )
            else:
                self.credential_store.delete_credentials(agent_id, fencing_token=fencing_token)
        except Exception:  # noqa: BLE001
            logger.exception("Compensation: credential cleanup failed")

        try:
            cleanup_setup(agent_id, self.environment_store, fencing_token=fencing_token)
        except Exception:  # noqa: BLE001
            logger.exception("Compensation: environment cleanup failed")

    def deprovision(
        self,
        agent_id: str,
        force: bool = False,
        *,
        cancellation_checkpoint: Optional[Callable[[], bool]] = None,
        fencing_token: Optional[int] = None,
    ) -> DeprovisionResponse:
        """
        Deprovision an agent: remove all resources and access.

        Args:
            agent_id: Agent to deprovision
            force: Force removal even if errors occur
            cancellation_checkpoint: Optional callable polled before each
                per-tool teardown call (each provisioner in the loop, plus the
                explicit Docker teardown call below). When it returns ``True``,
                deprovision stops before issuing that call and raises
                ``DeprovisionCancelledError`` instead of returning — the caller
                (a Temporal activity wrapper) uses this to distinguish an
                interrupted run from one that completed the full sequence.
            fencing_token: Caller's fencing token (see ``shared.fencing``);
                ``None`` skips enforcement. Tool-provisioner rejections are
                folded into the per-provisioner ``tools`` results (matching
                ``deprovision_tools``'s existing best-effort contract,
                unchanged here). The Docker/credential/environment calls
                below are, as before this parameter existed, not wrapped in
                a try/except — a stale-token rejection from any of them
                propagates as :class:`~agent_team_studio.agent_provisioning_team.shared.fencing.StaleFencingTokenError`
                exactly as any other exception already would.

        Returns:
            DeprovisionResponse with results

        Raises:
            DeprovisionCancelledError: ``cancellation_checkpoint`` signalled
                cancellation before all per-tool teardown calls completed.
        """
        results: Dict[str, Any] = {}
        errors: List[str] = []

        tool_results = deprovision_tools(
            agent_id=agent_id,
            provisioners=self.tool_agents,
            checkpoint=cancellation_checkpoint,
            fencing_token=fencing_token,
        )
        results["tools"] = tool_results

        for provisioner_key, success in tool_results.items():
            if not success:
                errors.append(f"Failed to deprovision provisioner '{provisioner_key}'")

        docker = self.tool_agents.get("docker_provisioner")
        if docker:
            if cancellation_checkpoint is not None and cancellation_checkpoint():
                raise DeprovisionCancelledError(agent_id, results)
            docker_result = docker.deprovision(agent_id, fencing_token=fencing_token)
            results["docker"] = docker_result.success
            if not docker_result.success and docker_result.error:
                errors.append(f"Docker: {docker_result.error}")

        cred_removed = self.credential_store.delete_credentials(
            agent_id, fencing_token=fencing_token
        )
        results["credentials_removed"] = cred_removed

        env_removed = self.environment_store.remove(agent_id, fencing_token=fencing_token)
        results["environment_removed"] = env_removed

        success = len(errors) == 0 or force

        return DeprovisionResponse(
            agent_id=agent_id,
            success=success,
            details=results,
            error="; ".join(errors) if errors else None,
        )

    def get_agent_status(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Get the current status of a provisioned agent."""
        return get_agent_status_dict(self.environment_store, agent_id)

    def list_agents(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """List all provisioned agents, optionally filtered by status."""
        return list_agent_status_dicts(self.environment_store, status=status)
