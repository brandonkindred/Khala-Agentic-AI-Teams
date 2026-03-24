"""
Provisioning Orchestrator: Coordinates the phase-based provisioning workflow.

Executes phases sequentially with progress callbacks for real-time tracking.
"""

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
from .shared.tool_manifest import load_manifest
from .tool_agents.docker_provisioner import DockerProvisionerTool
from .tool_agents.generic_provisioner import GenericProvisionerTool
from .tool_agents.git_provisioner import GitProvisionerTool
from .tool_agents.postgres_provisioner import PostgresProvisionerTool
from .tool_agents.redis_provisioner import RedisProvisionerTool

JobUpdater = Callable[..., None]


def _build_tool_agents() -> Dict[str, Any]:
    """Build the default set of tool provisioner agents."""
    return {
        "docker_provisioner": DockerProvisionerTool(),
        "postgres_provisioner": PostgresProvisionerTool(),
        "git_provisioner": GitProvisionerTool(),
        "redis_provisioner": RedisProvisionerTool(),
        "generic_provisioner": GenericProvisionerTool(),
    }


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
        self.tool_agents = tool_agents or _build_tool_agents()

    def run_workflow(
        self,
        agent_id: str,
        manifest_path: str,
        access_tier: AccessTier = AccessTier.STANDARD,
        job_updater: Optional[JobUpdater] = None,
    ) -> ProvisioningResult:
        """
        Execute the full provisioning workflow through all phases.

        Args:
            agent_id: Unique identifier for the agent being provisioned
            manifest_path: Path to the tool manifest YAML
            access_tier: Requested access tier (default: STANDARD)
            job_updater: Optional callback for progress updates.
                        Signature: job_updater(
                            current_phase: str,
                            progress: int,
                            current_tool: Optional[str] = None,
                            tools_completed: Optional[int] = None,
                            tools_total: Optional[int] = None,
                            status_text: Optional[str] = None,
                        )

        Returns:
            ProvisioningResult with complete provisioning information
        """

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

        _update(
            current_phase=Phase.SETUP.value,
            progress=5,
            status_text="Creating Docker environment...",
        )

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
                agent_id=agent_id,
                current_phase=Phase.SETUP,
                completed_phases=[],
                success=False,
                error=setup_result.error or "Setup failed",
            )

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
        )

        if not cred_result.success:
            cleanup_setup(agent_id, self.environment_store)
            return ProvisioningResult(
                agent_id=agent_id,
                current_phase=Phase.CREDENTIAL_GENERATION,
                completed_phases=[Phase.SETUP],
                environment=setup_result.environment,
                success=False,
                error=cred_result.error or "Credential generation failed",
            )

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
            access_tier=access_tier,
            provisioners=self.tool_agents,
            environment_store=self.environment_store,
            progress_callback=lambda done, total, tool: _update(
                current_tool=tool,
                tools_completed=done,
                tools_total=total,
                progress=35 + int((done / max(total, 1)) * 30),
                status_text=f"Provisioning {tool}...",
            ),
        )

        _update(
            current_phase=Phase.ACCESS_AUDIT.value,
            progress=70,
            status_text="Auditing access permissions...",
        )

        audit_result = run_access_audit(
            agent_id=agent_id,
            tool_results=account_result.tool_results,
            access_tier=access_tier,
            manifest=manifest,
            provisioners=self.tool_agents,
            progress_callback=lambda msg: _update(status_text=msg),
        )

        _update(
            current_phase=Phase.DOCUMENTATION.value,
            progress=85,
            status_text="Generating onboarding documentation...",
        )

        workspace_path = (
            setup_result.environment.workspace_path if setup_result.environment else "/workspace"
        )

        doc_result = run_documentation(
            agent_id=agent_id,
            manifest=manifest,
            credentials=cred_result.credentials,
            tool_results=account_result.tool_results,
            access_tier=access_tier,
            workspace_path=workspace_path,
            progress_callback=lambda msg: _update(status_text=msg),
        )

        _update(
            current_phase=Phase.DELIVER.value,
            progress=95,
            status_text="Finalizing provisioning...",
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
            current_phase=Phase.DELIVER.value,
            progress=100,
            status_text="Provisioning complete",
        )

        return final_result

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
