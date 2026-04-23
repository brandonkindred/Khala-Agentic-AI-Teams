"""
Provisioning Orchestrator: Coordinates the phase-based provisioning workflow.

Executes phases sequentially with progress callbacks for real-time tracking.
"""

import logging
import threading
from typing import Any, Callable, Dict, List, Optional

from .models import (
    AccessTier,
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
from .shared.environment_store import EnvironmentStore
from .shared.logging_context import install_filter as _install_log_filter
from .shared.phase_state import (
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
    has already invoked `_compensate()` to roll back partial state."""

    def __init__(self, agent_id: str, phase: str) -> None:
        self.agent_id = agent_id
        self.phase = phase
        super().__init__(f"Provisioning for {agent_id} cancelled during {phase}")


# Backwards-compat alias for callers/tests that imported the old name.
def _build_tool_agents() -> Dict[str, Any]:
    return build_default_tool_agents()


class ProvisioningOrchestrator:
    """
    Orchestrator for the agent provisioning workflow.

    Coordinates 6 phases:
    1. SETUP - Create Docker container
    2. CREDENTIAL_GENERATION - Generate passwords/tokens
    3. ACCOUNT_PROVISIONING - Create accounts in tools
    4. ACCESS_AUDIT - Verify least-privilege
    5. DOCUMENTATION - Generate onboarding docs
    6. DELIVER - Finalize and return results
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
        access_tier: AccessTier = AccessTier.STANDARD,
        job_updater: Optional[JobUpdater] = None,
        skip_phases: Optional[set] = None,
        prior_results: Optional[Dict[str, Any]] = None,
        shutdown_event: Optional[threading.Event] = None,
    ) -> ProvisioningResult:
        """
        Execute the full provisioning workflow through all phases.

        Args:
            agent_id: Unique identifier for the agent being provisioned
            manifest_path: Path to the tool manifest YAML
            access_tier: Requested access tier (default: STANDARD)
            job_updater: Optional callback for progress updates.
            skip_phases: Set of Phase values to skip (already completed on a prior run).
            prior_results: Dict of phase results from a prior run keyed by phase value string.
            shutdown_event: Optional threading.Event that signals cooperative
                cancellation at phase boundaries. When set, the orchestrator
                compensates and raises ProvisioningShutdownError.

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
            logger.info("Resuming workflow — skipping completed phases: %s", [p.value for p in skip_phases])

        # Tracks the latest tool_results the orchestrator has produced, so a
        # shutdown check mid-workflow can pass them to `_compensate()` to
        # deprovision any tools that succeeded before cancellation.
        tool_results_ref: List[Any] = []

        def _set_phase(name: str) -> None:
            _phase_var.set(name)

        def _check_shutdown(phase_name: str) -> None:
            if shutdown_event is not None and shutdown_event.is_set():
                logger.warning(
                    "Shutdown signalled for agent=%s during %s; compensating",
                    agent_id, phase_name,
                )
                self._compensate(agent_id, tool_results_ref)
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
            _update(current_phase=Phase.SETUP.value, progress=5, status_text="Creating Docker environment...")
            setup_result = run_setup(
                agent_id=agent_id,
                manifest=manifest,
                access_tier=access_tier,
                environment_store=self.environment_store,
                docker_provisioner=self.tool_agents.get("docker_provisioner"),
                progress_callback=lambda msg: _update(status_text=msg),
            )
            if not setup_result.success:
                return ProvisioningResult(
                    agent_id=agent_id, current_phase=Phase.SETUP, completed_phases=[],
                    success=False, error=setup_result.error or "Setup failed",
                )

        # -- CREDENTIAL_GENERATION --
        _set_phase(Phase.CREDENTIAL_GENERATION.value)
        _check_shutdown(Phase.CREDENTIAL_GENERATION.value)
        if Phase.CREDENTIAL_GENERATION in skip_phases and prior_results.get("credential_generation"):
            cred_result = restore_credentials(prior_results["credential_generation"])
            logger.info("Skipping CREDENTIAL_GENERATION (already completed)")
        else:
            _update(current_phase=Phase.CREDENTIAL_GENERATION.value, progress=20, status_text="Generating credentials...")
            cred_result = run_credential_generation(
                agent_id=agent_id,
                manifest=manifest,
                credential_store=self.credential_store,
                progress_callback=lambda tool, done, total: _update(
                    current_tool=tool, tools_completed=done, tools_total=total,
                    status_text=f"Generating credentials for {tool}...",
                ),
            )
            if not cred_result.success:
                cleanup_setup(agent_id, self.environment_store)
                return ProvisioningResult(
                    agent_id=agent_id, current_phase=Phase.CREDENTIAL_GENERATION,
                    completed_phases=[Phase.SETUP], environment=setup_result.environment,
                    success=False, error=cred_result.error or "Credential generation failed",
                )

        # -- ACCOUNT_PROVISIONING --
        _set_phase(Phase.ACCOUNT_PROVISIONING.value)
        _check_shutdown(Phase.ACCOUNT_PROVISIONING.value)
        if Phase.ACCOUNT_PROVISIONING in skip_phases and prior_results.get("account_provisioning"):
            account_result = restore_account_provisioning(prior_results["account_provisioning"])
            logger.info("Skipping ACCOUNT_PROVISIONING (already completed)")
        else:
            _update(
                current_phase=Phase.ACCOUNT_PROVISIONING.value, progress=35,
                tools_total=len(manifest.tools), status_text="Provisioning tool accounts...",
            )
            account_result = run_account_provisioning(
                agent_id=agent_id, manifest=manifest, credentials=cred_result.credentials,
                access_tier=access_tier, provisioners=self.tool_agents,
                environment_store=self.environment_store,
                progress_callback=lambda done, total, tool: _update(
                    current_tool=tool, tools_completed=done, tools_total=total,
                    progress=35 + int((done / max(total, 1)) * 30), status_text=f"Provisioning {tool}...",
                ),
            )

            # Compensation: if any tool failed, roll back already-provisioned
            # tools and the Docker setup so we don't leak resources or
            # encrypted credentials for a half-finished agent.
            if not account_result.success:
                logger.error(
                    "ACCOUNT_PROVISIONING failed for agent=%s: %s — rolling back",
                    agent_id, account_result.error,
                )
                self._compensate(agent_id, account_result.tool_results)
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
            audit_result = prior_results["access_audit"]
            logger.info("Skipping ACCESS_AUDIT (already completed)")
        else:
            _update(current_phase=Phase.ACCESS_AUDIT.value, progress=70, status_text="Auditing access permissions...")
            audit_result = run_access_audit(
                agent_id=agent_id, tool_results=account_result.tool_results,
                access_tier=access_tier, manifest=manifest, provisioners=self.tool_agents,
                progress_callback=lambda msg: _update(status_text=msg),
            )

        # -- DOCUMENTATION --
        _set_phase(Phase.DOCUMENTATION.value)
        _check_shutdown(Phase.DOCUMENTATION.value)
        if Phase.DOCUMENTATION in skip_phases and prior_results.get("documentation"):
            doc_result = restore_documentation(prior_results["documentation"])
            logger.info("Skipping DOCUMENTATION (already completed)")
        else:
            _update(current_phase=Phase.DOCUMENTATION.value, progress=85, status_text="Generating onboarding documentation...")
            workspace_path = (
                setup_result.environment.workspace_path if hasattr(setup_result, "environment") and setup_result.environment else "/workspace"
            )
            doc_result = run_documentation(
                agent_id=agent_id, manifest=manifest, credentials=cred_result.credentials,
                tool_results=account_result.tool_results, access_tier=access_tier,
                workspace_path=workspace_path,
                progress_callback=lambda msg: _update(status_text=msg),
            )

        # -- DELIVER --
        _set_phase(Phase.DELIVER.value)
        _check_shutdown(Phase.DELIVER.value)
        _update(current_phase=Phase.DELIVER.value, progress=95, status_text="Finalizing provisioning...")
        deliver_result = run_deliver(
            agent_id=agent_id, environment=setup_result.environment,
            credentials=cred_result.credentials, tool_results=account_result.tool_results,
            access_audit=audit_result, onboarding=doc_result.onboarding,
            environment_store=self.environment_store,
            progress_callback=lambda msg: _update(status_text=msg),
        )

        final_result = build_final_result(
            agent_id=agent_id, environment=setup_result.environment,
            credentials=cred_result.credentials, tool_results=account_result.tool_results,
            access_audit=audit_result, onboarding=doc_result.onboarding,
            deliver_result=deliver_result,
        )

        _update(current_phase=Phase.DELIVER.value, progress=100, status_text="Provisioning complete")
        return final_result

    def _compensate(
        self,
        agent_id: str,
        tool_results: List[Any],
    ) -> None:
        """Roll back partial provisioning after a phase failure.

        Best-effort: deprovisions any tools that did succeed, tears down the
        Docker environment, and removes encrypted credentials so a failed
        run doesn't leak resources or secrets to disk.
        """
        # Look each successfully-provisioned tool back up by its registry key
        # (stamped onto the result in run_account_provisioning). Prior to #293
        # this used f"{r.tool_name}_provisioner", which silently missed for
        # provisioners whose class `tool_name` differs from the registry stem
        # (e.g. PostgresProvisionerTool.tool_name == "postgresql" vs key
        # "postgres_provisioner"), leaking accounts + encrypted credentials.
        for r in tool_results:
            if not getattr(r, "success", False):
                continue
            key = getattr(r, "provisioner_key", None)
            if not key:
                logger.warning(
                    "Compensation: tool_result for %s has no provisioner_key; "
                    "skipping rollback (stale result pre-#293).",
                    getattr(r, "tool_name", "?"),
                )
                continue
            provisioner = self.tool_agents.get(key)
            if provisioner is None:
                logger.warning(
                    "Compensation: no provisioner registered for key=%s", key
                )
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
            if records:
                for rec in reversed(records):
                    try:
                        provisioner.replay_compensation(agent_id, rec.kind, rec.payload)
                    except Exception:  # noqa: BLE001 — best-effort cleanup
                        logger.exception(
                            "Compensation: replay failed kind=%s for %s", rec.kind, key
                        )
                try:
                    provisioner.clear_compensations(agent_id)
                    provisioner._state.delete(agent_id)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Compensation: post-replay state cleanup failed for %s", key
                    )
            else:
                try:
                    provisioner.deprovision(agent_id)
                except Exception:  # noqa: BLE001 — best-effort cleanup
                    logger.exception("Compensation: deprovision failed for %s", key)

        docker = self.tool_agents.get("docker_provisioner")
        if docker is not None:
            try:
                docker.deprovision(agent_id)
            except Exception:  # noqa: BLE001
                logger.exception("Compensation: docker teardown failed")

        try:
            self.credential_store.delete_credentials(agent_id)
        except Exception:  # noqa: BLE001
            logger.exception("Compensation: credential cleanup failed")

        try:
            cleanup_setup(agent_id, self.environment_store)
        except Exception:  # noqa: BLE001
            logger.exception("Compensation: environment cleanup failed")

    def deprovision(
        self,
        agent_id: str,
        force: bool = False,
    ) -> DeprovisionResponse:
        """
        Deprovision an agent: remove all resources and access.

        Args:
            agent_id: Agent to deprovision
            force: Force removal even if errors occur

        Returns:
            DeprovisionResponse with results
        """
        results: Dict[str, Any] = {}
        errors: List[str] = []

        tool_results = deprovision_tools(
            agent_id=agent_id,
            provisioners=self.tool_agents,
        )
        results["tools"] = tool_results

        for tool, success in tool_results.items():
            if not success:
                errors.append(f"Failed to deprovision {tool}")

        docker = self.tool_agents.get("docker_provisioner")
        if docker:
            docker_result = docker.deprovision(agent_id)
            results["docker"] = docker_result.success
            if not docker_result.success and docker_result.error:
                errors.append(f"Docker: {docker_result.error}")

        cred_removed = self.credential_store.delete_credentials(agent_id)
        results["credentials_removed"] = cred_removed

        env_removed = self.environment_store.remove(agent_id)
        results["environment_removed"] = env_removed

        success = len(errors) == 0 or force

        return DeprovisionResponse(
            agent_id=agent_id,
            success=success,
            details=results,
            error="; ".join(errors) if errors else None,
        )

    def get_agent_status(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """
        Get the current status of a provisioned agent.

        Args:
            agent_id: Agent to check

        Returns:
            Status dict or None if not found
        """
        env = self.environment_store.get(agent_id)
        if env is None:
            return None

        return {
            "agent_id": agent_id,
            "status": env.status,
            "container_id": env.container_id,
            "container_name": env.container_name,
            "tools_provisioned": env.tools_provisioned,
            "created_at": env.created_at,
        }

    def list_agents(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        List all provisioned agents.

        Args:
            status: Optional status filter ('running', 'ready', etc.)

        Returns:
            List of agent status dicts
        """
        environments = self.environment_store.list_all(status=status)
        return [
            {
                "agent_id": env.agent_id,
                "status": env.status,
                "container_name": env.container_name,
                "tools_provisioned": env.tools_provisioned,
                "created_at": env.created_at,
            }
            for env in environments
        ]
