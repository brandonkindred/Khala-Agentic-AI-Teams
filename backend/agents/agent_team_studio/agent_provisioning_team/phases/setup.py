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
from ..tool_agents.docker_provisioner import JOB_ID_CONFIG_KEY, DockerProvisionerTool

logger = logging.getLogger(__name__)


def run_setup(
    agent_id: str,
    manifest: ToolManifest,
    environment_store: Optional[EnvironmentStore] = None,
    docker_provisioner: Optional[DockerProvisionerTool] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
    on_registered: Optional[Callable[[EnvironmentInfo], None]] = None,
    job_id: Optional[str] = None,
    fencing_token: Optional[int] = None,
) -> SetupResult:
    """
    Execute the setup phase: create (or reuse) the agent's Docker container.

    Args:
        agent_id: Unique identifier for the agent
        manifest: Loaded tool manifest
        environment_store: Store for tracking environments
        docker_provisioner: Docker provisioner instance
        progress_callback: Optional callback for progress updates
        on_registered: Optional hook run immediately after a freshly created
            (or docker-reused-but-freshly-registered) environment is
            registered, receiving the registered ``EnvironmentInfo``. Runs
            inside the same rollback boundary as registration itself — a
            durable checkpoint write (e.g. a Temporal activity's job-store
            record) belongs here, not after ``run_setup`` returns, so that a
            checkpoint failure tears the container back down instead of
            leaking it. Not called on the fast path (an already-``running``
            record reused with nothing freshly created) — there is no new
            infrastructure there for a checkpoint failure to leak.
        job_id: The calling workflow's own job id. When given, is stamped as
            a ``khala.job_id`` Docker label on the container this call
            creates, so a later ``compensate_activity``/
            ``check_existing_environment_activity`` can positively attribute
            it to this attempt even if the local idempotency state that
            would otherwise prove it never gets written.
        fencing_token: Caller's fencing token (see ``shared.fencing``);
            ``None`` skips enforcement.

    Returns:
        SetupResult with environment info

    Preconditions:
        * ``agent_id`` is a non-empty identifier.
        * ``manifest`` is a loaded ``ToolManifest``.
    Postconditions:
        * On success: returns ``SetupResult(success=True, environment=...)``; the
          environment record is registered (registration is atomic — see
          ``EnvironmentStore.register``).
        * The already-``running``-record fast path is only taken when Docker
          also confirms (or can't rule out) that the container is still
          there — a record whose container is CONFIRMED gone falls through
          to ``docker.provision()`` instead of returning a broken
          ``success=True`` result pointing at a dead ``container_id``.
        * On Docker provisioning failure: returns
          ``SetupResult(success=False, error=...)``; no environment record is
          written, and the docker provisioner best-effort-removes a container its
          own failed ``docker run`` left behind (never a pre-existing same-named
          container, which may be a healthy agent).
        * On a failure after provisioning (progress callback, registration, or
          ``on_registered``): a best-effort rollback runs before the exception
          propagates. Because registration is atomic, a failed register leaves
          no record from this attempt, so ownership is decided by what the
          store actually holds: a record owned by a prior or concurrent
          registration preserves the container; otherwise the container this
          attempt created (or a reused orphan with no record) is deprovisioned.
          Best-effort means teardown failures are logged rather than raised,
          and concurrent same-agent provisioning is not serialized — a narrow
          adoption race remains, tracked as separate follow-up work.
    """
    assert agent_id, "agent_id must be non-empty"
    assert manifest is not None, "manifest must be a loaded ToolManifest"
    env_store = environment_store or EnvironmentStore()
    docker = docker_provisioner or DockerProvisionerTool()

    if progress_callback:
        progress_callback("Checking for existing environment...")

    existing = env_store.get(agent_id)
    # The fast path trusts a "running" record without otherwise verifying
    # anything — so a record surviving after its container was destroyed
    # out-of-band (or docker's own idempotency state was separately lost)
    # would deliver success=True pointing at a dead container_id. Only a
    # CONFIRMED-absent container disqualifies the fast path; alive or
    # unknown (daemon unreachable) both still trust it, same conservative
    # default used elsewhere. Falling through to docker.provision() below
    # lets DockerProvisionerTool's own reuse check (_on_reuse) detect and
    # clear this same staleness and create a fresh container on retry.
    if (
        existing
        and existing.status == "running"
        and docker._container_exists(existing.container_name) is not False
    ):
        return SetupResult(
            success=True,
            environment=EnvironmentInfo(
                container_id=existing.container_id,
                container_name=existing.container_name,
                ssh_host=existing.ssh_host,
                ssh_port=existing.ssh_port,
                workspace_path=existing.workspace_path,
                status="running",
                reused=True,
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
    if job_id:
        docker_config[JOB_ID_CONFIG_KEY] = job_id

    credentials = GeneratedCredentials(
        tool_name="docker",
    )

    result = docker.provision(
        agent_id=agent_id,
        config=docker_config,
        credentials=credentials,
        fencing_token=fencing_token,
    )

    if not result.success:
        return SetupResult(
            success=False,
            error=result.error or "Docker container creation failed",
        )

    # The progress callback is inside the rollback boundary: the container already
    # exists here, so a callback (e.g. a job_updater) that raises must trigger the
    # same atomic-setup cleanup as a register failure, not leak the container.
    registered = False
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
            reused=bool(result.details.get("reused", False)),
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
            ),
            fencing_token=fencing_token,
        )
        # Set only after register() itself succeeds — _rollback_failed_setup
        # uses this to tell "we just wrote the record ourselves" (on_registered
        # then failed) apart from "register() failed and never landed."
        registered = True
        if on_registered is not None:
            on_registered(env_info)
    except Exception:
        _rollback_failed_setup(
            agent_id, existing, result, env_store, docker, registered, fencing_token=fencing_token
        )
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
    registered_by_this_call: bool = False,
    *,
    fencing_token: Optional[int] = None,
) -> None:
    """Best-effort teardown after a failure between provisioning and commit.

    Ownership rule when ``registered_by_this_call`` is ``False`` (``register``
    itself failed): because ``EnvironmentStore.register`` is atomic, a failed
    register from this attempt leaves NO record, so any record the store holds
    now belongs to someone else — a prior successful setup (``existing``, read
    before this attempt wrote anything) or a concurrent job that registered
    after our pre-check. Containers backed by such a record are preserved; a
    container this attempt created, or a reused orphan with no record, is
    deprovisioned. A prior non-running record is left in place for continuity
    (its ``created_at`` / ``tools_provisioned`` survive, and a non-``running``
    status cannot short-circuit a retry's fast path).

    When ``registered_by_this_call`` is ``True`` (``register`` itself
    succeeded and a *later* step — e.g. ``on_registered`` — then failed), that
    "current record belongs to someone else" heuristic does not apply: the
    record in the store is unambiguously this attempt's own write (its
    ``container_id`` matching ``result`` is expected, not evidence of a
    concurrent adopter). Two sub-cases:

    * ``result.details["reused"]`` is truthy AND ``existing is not None``: a
      non-``running`` ``existing`` record (e.g. a delivered agent's
      ``"ready"`` record) skips ``run_setup``'s fast path yet still resolves
      to a docker-level reuse — ``register`` succeeding there doesn't mean
      *this* attempt's own ``docker.provision`` call created a fresh
      container, so the container is never deprovisioned. The record is
      restored to ``existing`` (undoing exactly this attempt's overwrite)
      rather than deleted, so a healthy, already-delivered agent doesn't
      disappear from the registry until some unrelated later attempt happens
      to rediscover its container.
    * Otherwise (a genuinely fresh container, or a reused orphan with no
      prior record): the record is removed unconditionally — the
      record-then-container ordering matches :func:`cleanup_setup`, so a
      later ``run_setup`` can't fast-path onto a ``running`` record backed by
      a container that's about to be deprovisioned — and the container is
      deprovisioned only when not reused (an adopted orphan predates this
      attempt just as much as the ``existing``-record case above).

    Preconditions:
        * ``agent_id`` is non-empty.
        * ``result`` is the successful ``ToolProvisionResult`` for this attempt
          (a container exists or was reused for ``agent_id``).
        * ``existing`` is the environment record read before this attempt wrote
          anything (or ``None``).
        * ``registered_by_this_call`` is ``True`` iff this attempt's own
          ``env_store.register(...)`` call already completed before the
          failure being rolled back.
        * ``fencing_token``, when given, is threaded into every mutating
          store/provisioner call below; each is already individually wrapped
          in its own best-effort ``try/except Exception`` (see Postconditions),
          so a rejected :class:`~agent_team_studio.agent_provisioning_team.shared.fencing.StaleFencingTokenError`
          is logged and that one step skipped, same as any other rollback-step
          failure — this path never re-raises.
    Postconditions:
        * Never raises; teardown / restore failures are logged so they cannot
          mask the original setup error.
        * When ``registered_by_this_call`` is ``False``: the environment store
          is not modified (nothing this attempt wrote survived, and other
          owners' records are not touched).
        * When ``registered_by_this_call`` is ``True`` and the container was
          reused with a prior ``existing`` record: that record is restored
          (best-effort) and the container is never deprovisioned.
        * When ``registered_by_this_call`` is ``True`` otherwise: this
          attempt's own record is removed, and the container is deprovisioned
          only if that removal succeeded AND the container was not reused (a
          reused orphan predates this attempt and is preserved) — if removal
          itself raises, the container is left alone too, so the (now-stale)
          surviving record and the surviving container stay consistent with
          each other rather than the record claiming ``running`` for a
          container that's actually gone.

    Concurrent same-agent provisioning is not serialized, so a job that adopts
    this attempt's container and registers between the ownership read below and
    the deprovision call can still lose it — tracked as separate follow-up work.
    """
    assert agent_id, "agent_id must be non-empty"
    if registered_by_this_call:
        reused = bool(result.details.get("reused", False))
        if reused and existing is not None:
            # register() succeeding doesn't mean this attempt's OWN docker.provision
            # call created a fresh container — a non-"running" existing record
            # (e.g. a delivered agent's "ready" record) skips run_setup's fast
            # path but still resolves to docker-level reuse here. The container
            # predates this attempt and must not be destroyed just because this
            # attempt's own bookkeeping failed — but neither should the prior
            # record be deleted outright: `existing` is what env_store held
            # before this attempt overwrote it, so restoring it verbatim
            # undoes exactly this attempt's own write, leaving the delivered
            # agent still discoverable in the registry instead of vanishing
            # from it until some later attempt happens to rediscover the
            # container and re-register from scratch.
            try:
                env_store.register(existing, fencing_token=fencing_token)
            except Exception:
                logger.exception(
                    "Setup rollback: failed to restore agent_id=%s's prior "
                    "environment record after a reused-container failure; "
                    "registry may be stale until a later attempt rediscovers "
                    "the container",
                    agent_id,
                )
            return
        try:
            env_store.remove(agent_id, fencing_token=fencing_token)
        except Exception:
            # Never let a record-removal failure (e.g. a now-read-only registry
            # directory) escape and mask the original setup error — but also
            # do NOT fall through to deprovision the container below: doing so
            # would leave the still-present record (status="running") pointing
            # at a container that no longer exists, which a later run_setup's
            # fast path would trust as healthy. Same ordering rule as
            # cleanup_setup: skip teardown when the record can't be removed
            # first, so record and container stay consistent (both survive)
            # rather than silently lying about what's still there.
            logger.exception(
                "Setup rollback: failed to remove this attempt's own environment "
                "record for agent_id=%s; preserving the container so record and "
                "container stay consistent",
                agent_id,
            )
            return
        if reused:
            # Reused container with no prior env_store record at all (an
            # orphan this attempt adopted) — nothing to restore, and the
            # record this attempt wrote is already removed above.
            return
    else:
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
        teardown = docker.deprovision(agent_id, fencing_token=fencing_token)
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
    *,
    fencing_token: Optional[int] = None,
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

    env_store.remove(agent_id, fencing_token=fencing_token)
    teardown = docker.deprovision(agent_id, fencing_token=fencing_token)
    if not teardown.success:
        logger.error(
            "Cleanup: container teardown for agent_id=%s failed; container may "
            "survive (provisioner state retained for retry): %s",
            agent_id,
            teardown.error,
        )
        return False

    return True
