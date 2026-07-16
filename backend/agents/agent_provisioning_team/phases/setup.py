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
          registered, the container is torn down and this attempt's own env record
          (if the failed register wrote one) is cleared — both best effort —
          before the exception propagates, so a failed setup never leaks orphaned
          Docker resources or a stale record that a retry's early-return would
          short-circuit onto, including across Temporal retries of this activity.
          Teardown is skipped only when a *reused* container already has an
          environment record (of any status — ``running``, ``ready``, ...), which
          means a prior successful setup or a concurrent job owns and is using it;
          tearing that down would break a live agent.
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
        # to record it. Only reclaim a container THIS attempt is responsible for:
        #   * one we created (provision did not report it as ``reused``) — it is
        #     ours, tear it down; or
        #   * a reused container that has NO environment record — an orphan left by
        #     a prior failed attempt/retry, safe (and, across retries, necessary)
        #     to reclaim.
        # A reused container that already HAS an environment record is owned by a
        # prior successful setup or a concurrent job — its status may be "running",
        # "ready" (a completed agent, per deliver), or anything else — so deleting
        # it would break a live agent. Existence of the record, NOT its status, is
        # the ownership signal, so re-provisioning a completed "ready" agent whose
        # register transiently fails does not destroy it.
        #
        # Ownership evidence starts from the record read BEFORE this attempt's
        # register (``existing``, captured at the top of run_setup), which is
        # immune to a register write that truncates/corrupts the prior record. For
        # a reused container with no such record, a fresh read additionally catches
        # a concurrent job that registered after our pre-check — but that read is
        # best-effort: EnvironmentStore.get CAN raise (its _read_env_data calls
        # Path.exists outside the OSError handler, which can fail on e.g. EACCES),
        # and a failed re-read must not abort this rollback or mask the original
        # error, so we log and fall back to reclaiming.
        #
        # NOTE: not atomic — a container we created can be adopted by a concurrent
        # job before this rollback runs, so this still races concurrent
        # provisioning of the same agent_id. Fully closing that race needs
        # agent-level serialization, tracked as separate follow-up work.
        reused = bool(result.details.get("reused", False))
        owned_by_other = reused and existing is not None
        if reused and not owned_by_other:
            try:
                owned_by_other = env_store.get(agent_id) is not None
            except Exception:
                logger.exception(
                    "Setup rollback: ownership re-read failed for agent_id=%s; "
                    "proceeding with reclaim",
                    agent_id,
                )
        if not owned_by_other:
            try:
                teardown = docker.deprovision(agent_id)
                # deprovision() reports failure (e.g. a `docker stop` timeout) via
                # its result rather than raising, so inspect it — otherwise a
                # failed rollback would leave the container silently orphaned.
                if not getattr(teardown, "success", True):
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
            # Clear any record this failed registration left (register can write a
            # complete record and then raise on flush/close), so a retry's
            # running-only early-return does not short-circuit onto the container
            # we just deleted. Best effort; must not mask the original error. Safe
            # because we only reach here when no *other* owner holds a record, so
            # the record, if any, is this attempt's.
            try:
                env_store.remove(agent_id)
            except Exception:
                logger.exception(
                    "Setup rollback: failed to remove env record for agent_id=%s",
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
