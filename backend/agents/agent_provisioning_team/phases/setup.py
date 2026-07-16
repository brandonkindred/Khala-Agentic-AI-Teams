"""
Setup phase: Create Docker container for the agent.

This is phase 1 of the provisioning workflow.
"""

import logging
from typing import Any, Callable, Dict, Optional

from ..models import (
    EnvironmentInfo,
    GeneratedCredentials,
    SetupResult,
)
from ..shared.environment_store import EnvironmentStore
from ..shared.tool_manifest import ToolManifest
from ..tool_agents.docker_provisioner import DockerProvisionerTool

logger = logging.getLogger(__name__)


def run_setup(
    agent_id: str,
    manifest: ToolManifest,
    environment_store: Optional[EnvironmentStore] = None,
    docker_provisioner: Optional[DockerProvisionerTool] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> SetupResult:
    """
    Execute the setup phase: create (or reuse) the agent's Docker container.

    Args:
        agent_id: Unique identifier for the agent
        manifest: Loaded tool manifest
        environment_store: Store for tracking environments
        docker_provisioner: Docker provisioner instance
        progress_callback: Optional callback for progress updates

    Returns:
        SetupResult with environment info

    Preconditions:
        * ``agent_id`` is a non-empty identifier.
        * ``manifest`` is a loaded ``ToolManifest``.
    Postconditions:
        * On success: returns ``SetupResult(success=True, environment=...)``; a
          newly created container is registered in ``environment_store``.
        * On Docker provisioning failure: returns
          ``SetupResult(success=False, error=...)`` — nothing is created.
        * Atomicity: if the container is created but the environment cannot be
          registered, the container is torn down (best effort) before the
          exception propagates, so a failed setup never leaks orphaned Docker
          resources — including across Temporal retries of this activity.
          Teardown is skipped only when a *running* environment record already
          exists for the ``agent_id``, which means another job (or a prior
          successful setup) owns and is using the container; tearing that down
          would break a live agent.
    """
    env_store = environment_store or EnvironmentStore()
    docker = docker_provisioner or DockerProvisionerTool()

    if progress_callback:
        progress_callback("Checking for existing environment...")

    existing = env_store.get(agent_id)
    if existing and existing.status == "running":
        return SetupResult(
            success=True,
            environment=EnvironmentInfo(
                container_id=existing.container_id,
                container_name=existing.container_name,
                ssh_host=existing.ssh_host,
                ssh_port=existing.ssh_port,
                workspace_path=existing.workspace_path,
                status="running",
            ),
        )

    if progress_callback:
        progress_callback("Creating Docker container...")

    docker_config: Dict[str, Any] = {
        "base_image": manifest.base_image,
        "workspace_path": f"/workspace/{agent_id}",
        "environment": manifest.environment,
        "expose_ssh": True,
    }

    credentials = GeneratedCredentials(
        tool_name="docker",
    )

    result = docker.provision(
        agent_id=agent_id,
        config=docker_config,
        credentials=credentials,
    )

    if not result.success:
        return SetupResult(
            success=False,
            error=result.error or "Docker container creation failed",
        )

    if progress_callback:
        progress_callback("Registering environment...")

    from ..shared.environment_store import EnvironmentInfo as EnvInfoClass

    try:
        env_info = EnvironmentInfo(
            container_id=result.details.get("container_id", ""),
            container_name=result.details.get("container_name", f"agent-{agent_id}"),
            ssh_host="localhost",
            ssh_port=result.details.get("ssh_port", 22),
            workspace_path=result.details.get("workspace_path", f"/workspace/{agent_id}"),
            status="running",
        )

        env_store.register(
            EnvInfoClass(
                agent_id=agent_id,
                container_id=env_info.container_id,
                container_name=env_info.container_name,
                ssh_host=env_info.ssh_host,
                ssh_port=env_info.ssh_port,
                workspace_path=env_info.workspace_path,
                status="running",
                tools_provisioned=[],
            )
        )
    except Exception:
        # Atomic setup: a container this setup produced must not outlive a failure
        # to record it. Reclaim it (best effort) UNLESS a *running* environment
        # record already exists for this agent_id — such a record means another job
        # (or a prior successful setup) owns and is using the container, so tearing
        # it down would break a live agent. Without that record the container is an
        # unregistered orphan — this attempt's, or a prior failed attempt's that a
        # Temporal retry is now reusing — so teardown is safe. Gating on the record
        # rather than the idempotent ``reused`` flag is deliberate: ``reused`` also
        # matches a container this same setup created on an earlier retry, and
        # trusting it there would suppress teardown forever and leak that container.
        #
        # NOTE: the read-then-teardown is not atomic, so a job that registers
        # concurrently between the two can still lose its container. Fully closing
        # that race needs agent-level serialization, tracked as separate follow-up
        # work.
        owned_elsewhere = False
        try:
            current = env_store.get(agent_id)
            owned_elsewhere = current is not None and getattr(current, "status", None) == "running"
        except Exception:
            logger.exception(
                "Setup rollback: could not read environment for agent_id=%s; "
                "proceeding with teardown",
                agent_id,
            )
        if not owned_elsewhere:
            try:
                teardown = docker.deprovision(agent_id)
                # deprovision() reports failure (e.g. a `docker stop` timeout) via
                # its result rather than raising, so inspect it — otherwise a
                # failed rollback would leave the container silently orphaned.
                if teardown is not None and not getattr(teardown, "success", True):
                    logger.error(
                        "Setup rollback: container teardown for agent_id=%s reported "
                        "failure; container may be orphaned: %s",
                        agent_id,
                        getattr(teardown, "error", None),
                    )
            except Exception:
                logger.exception(
                    "Setup rollback: failed to tear down container for agent_id=%s",
                    agent_id,
                )
        raise

    return SetupResult(
        success=True,
        environment=env_info,
    )


def cleanup_setup(
    agent_id: str,
    environment_store: Optional[EnvironmentStore] = None,
    docker_provisioner: Optional[DockerProvisionerTool] = None,
) -> bool:
    """
    Clean up a failed setup by removing any partially created resources.

    Returns:
        True if cleanup successful
    """
    env_store = environment_store or EnvironmentStore()
    docker = docker_provisioner or DockerProvisionerTool()

    docker.deprovision(agent_id)
    env_store.remove(agent_id)

    return True
