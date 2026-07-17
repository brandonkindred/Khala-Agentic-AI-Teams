"""
Setup phase: Create Docker container for the agent.

This is phase 1 of the provisioning workflow.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from ..models import (
    EnvironmentInfo,
    GeneratedCredentials,
    SetupResult,
)
from ..shared.environment_store import EnvironmentInfo as StoreEnvironmentInfo
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
        * On success: returns ``SetupResult(success=True, environment=...)``; the
          environment record is registered (registration is atomic — see
          ``EnvironmentStore.register``).
        * On Docker provisioning failure: returns
          ``SetupResult(success=False, error=...)``; no environment record is
          written, and the docker provisioner best-effort-removes a container its
          own failed ``docker run`` left behind (never a pre-existing same-named
          container, which may be a healthy agent).
        * On a failure after provisioning (progress callback or registration):
          a best-effort rollback runs before the exception propagates. Because
          registration is atomic, a failed register leaves no record from this
          attempt, so ownership is decided by what the store actually holds: a
          record owned by a prior or concurrent registration preserves the
          container; otherwise the container this attempt created (or a reused
          orphan with no record) is deprovisioned. Best-effort means teardown
          failures are logged rather than raised, and concurrent same-agent
          provisioning is not serialized — a narrow adoption race remains,
          tracked as separate follow-up work.
    """
    assert agent_id, "agent_id must be non-empty"
    assert manifest is not None, "manifest must be a loaded ToolManifest"
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

    # The progress callback is inside the rollback boundary: the container already
    # exists here, so a callback (e.g. a job_updater) that raises must trigger the
    # same atomic-setup cleanup as a register failure, not leak the container.
    try:
        if progress_callback:
            progress_callback("Registering environment...")

        env_info = EnvironmentInfo(
            container_id=result.details.get("container_id", ""),
            container_name=result.details.get("container_name", f"agent-{agent_id}"),
            ssh_host="localhost",
            ssh_port=result.details.get("ssh_port", 22),
            workspace_path=result.details.get("workspace_path", f"/workspace/{agent_id}"),
            status="running",
        )

        env_store.register(
            StoreEnvironmentInfo(
                agent_id=agent_id,
                container_id=env_info.container_id,
                container_name=env_info.container_name,
                ssh_host=env_info.ssh_host,
                ssh_port=env_info.ssh_port,
                workspace_path=env_info.workspace_path,
                status="running",
                tools_provisioned=existing.tools_provisioned if existing else [],
                created_at=existing.created_at if existing else None,
                # Only stamp a fresh updated_at when replacing an existing record;
                # for a brand-new registration, leave it unset so it defaults to
                # the same created_at value rather than a few microseconds before it.
                updated_at=(datetime.now(timezone.utc).isoformat() if existing else None),
            )
        )
    except Exception:
        _rollback_failed_setup(agent_id, existing, result, env_store, docker)
        raise

    return SetupResult(
        success=True,
        environment=env_info,
    )


def _rollback_failed_setup(
    agent_id: str,
    existing,
    result,
    env_store: EnvironmentStore,
    docker: DockerProvisionerTool,
) -> None:
    """Best-effort teardown after a failure between provisioning and registration.

    Ownership rule: because ``EnvironmentStore.register`` is atomic, a failed
    register from this attempt leaves NO record, so any record the store holds
    now belongs to someone else — a prior successful setup (``existing``, read
    before this attempt wrote anything) or a concurrent job that registered
    after our pre-check. Containers backed by such a record are preserved; a
    container this attempt created, or a reused orphan with no record, is
    deprovisioned. A prior non-running record is left in place for continuity
    (its ``created_at`` / ``tools_provisioned`` survive, and a non-``running``
    status cannot short-circuit a retry's fast path).

    Preconditions:
        * ``agent_id`` is non-empty.
        * ``result`` is the successful ``ToolProvisionResult`` for this attempt
          (a container exists or was reused for ``agent_id``).
        * ``existing`` is the environment record read before this attempt wrote
          anything (or ``None``).
    Postconditions:
        * Never raises; teardown failures are logged so they cannot mask the
          original setup error.
        * The environment store is not modified (nothing this attempt wrote
          survived, and other owners' records are not touched).

    Concurrent same-agent provisioning is not serialized, so a job that adopts
    this attempt's container and registers between the ownership read below and
    the deprovision call can still lose it — tracked as separate follow-up work.
    """
    assert agent_id, "agent_id must be non-empty"
    reused = bool(result.details.get("reused", False))
    if reused and existing is not None:
        # A prior successful setup owns this container; its record is intact
        # (atomic register), whatever its status ("running", "ready", ...).
        return
    current = env_store.get(agent_id)  # never raises (store contract)
    if reused:
        if current is not None:
            # Ours never landed, so this record is a concurrent owner's.
            return
        # A missing record only proves an orphan when the registry itself is
        # readable: get() maps unreadable-store errors (e.g. EACCES) to None,
        # and destroying a reused container on masked evidence could kill a
        # healthy agent whose record simply cannot be read right now.
        if not env_store.readable(agent_id):
            logger.error(
                "Setup rollback: environment registry unreadable for agent_id=%s; "
                "preserving reused container (ownership unknown)",
                agent_id,
            )
            return
        # Reused orphan (no record anywhere): reclaim it.
    elif current is not None and current.container_id == result.details.get("container_id", ""):
        # We created the container, and the current record identifies THAT
        # container — a concurrent job registered it; it is theirs now. Compare
        # container identity, not the whole record: an unrelated field bump on
        # a record for a DIFFERENT (e.g. the old, non-running) container — say
        # a concurrent add_tool/update_status touching `updated_at` — must not
        # be mistaken for adoption of the container this attempt just created.
        return
    # Remaining created-path cases: no record at all, or the untouched prior
    # non-running record (kept for continuity) — either way the fresh container
    # is exclusively this attempt's, so tear it down.
    try:
        teardown = docker.deprovision(agent_id)
        # deprovision() reports failure (e.g. a `docker stop` timeout) via its
        # result rather than raising; surface it or the container leaks silently.
        if not teardown.success:
            logger.error(
                "Setup rollback: container teardown for agent_id=%s reported "
                "failure; container may be orphaned: %s",
                agent_id,
                teardown.error,
            )
    except Exception:
        logger.exception(
            "Setup rollback: failed to tear down container for agent_id=%s",
            agent_id,
        )


def cleanup_setup(
    agent_id: str,
    environment_store: Optional[EnvironmentStore] = None,
    docker_provisioner: Optional[DockerProvisionerTool] = None,
) -> bool:
    """Clean up a failed setup by removing its environment record and container.

    Preconditions:
        * ``agent_id`` is non-empty.
    Postconditions:
        * The environment record is removed FIRST, then the container is
          deprovisioned. If the record removal raises, the container is left in
          place so record and container stay consistent — deleting the container
          while a ``running`` record survived would let a later ``run_setup``'s
          fast path short-circuit onto a dead container.
        * Returns ``True`` only when the record was removed AND the container
          teardown reported success. A teardown that reports failure is logged
          and yields ``False``; the provisioner keeps its state row in that case
          (see ``DockerProvisionerTool.deprovision``), so the surviving
          container remains reachable by agent id for a later retry.
    """
    assert agent_id, "agent_id must be non-empty"
    env_store = environment_store or EnvironmentStore()
    docker = docker_provisioner or DockerProvisionerTool()

    env_store.remove(agent_id)
    teardown = docker.deprovision(agent_id)
    if not teardown.success:
        logger.error(
            "Cleanup: container teardown for agent_id=%s failed; container may "
            "survive (provisioner state retained for retry): %s",
            agent_id,
            teardown.error,
        )
        return False

    return True
