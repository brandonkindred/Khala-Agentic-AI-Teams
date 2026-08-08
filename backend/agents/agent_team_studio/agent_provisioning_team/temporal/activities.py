"""Temporal activities for the Agent Provisioning team.

Per-phase activities used by ``AgentProvisioningWorkflow``. The per-tool
provision step is its own activity (``provision_tool_activity``) so a workflow
can fan out across tools in parallel with independent retry/heartbeat policies.
Most activities take ``job_id`` as their first argument and write phase/progress
updates back to ``job_store`` so ``GET /provision/status/{job_id}`` shows live
progress without signal plumbing. Exceptions: ``list_manifest_tools_activity``
takes ``manifest_path`` only, and ``deprovision_activity`` takes ``agent_id``
first (no provision job row).

Invariants:
    * Activities heartbeat periodically for long-running work.
    * Progress / non-terminal job-store writes are best-effort and must not
      fail the activity (except durable terminal/checkpoint writes that must
      raise so Temporal retries).
    * Each activity validates required arguments with assertions on entry.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

from temporalio import activity

from agent_team_studio.agent_provisioning_team.shared import job_store as _js

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-phase, fan-out friendly activities
# ---------------------------------------------------------------------------


def _load_ctx(manifest_path: str):
    """Build a fresh orchestrator and load the agent tool manifest.

    Preconditions:
        * ``manifest_path`` is a readable YAML path (or registry key accepted by
          ``load_manifest``).
    Postconditions:
        * Returns ``(ProvisioningOrchestrator, ToolManifest)``.
    Raises:
        * Propagates import/IO/validation errors from ``load_manifest``.
        * Propagates ``OSError`` / ``PermissionError`` from
          ``ProvisioningOrchestrator.__init__`` when ``CredentialStore`` /
          ``EnvironmentStore`` cannot create their storage directories or key
          files — Temporal retries the activity.

    ``ProvisioningOrchestrator()`` is intentionally constructed per call. Its
    ``__init__`` only wires local ``CredentialStore`` / ``EnvironmentStore``
    (mkdir + optional Fernet key file) and builds in-process provisioner
    objects via ``build_default_tool_agents()`` — plain class construction, no
    network, DB pool, or config-file I/O — so a process-global cache is
    unnecessary for activity use. Do not introduce a module-level singleton
    unless profiling shows construction dominating activity latency.
    """
    from agent_team_studio.agent_provisioning_team.orchestrator import ProvisioningOrchestrator
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import load_manifest

    orch = ProvisioningOrchestrator()
    manifest = load_manifest(manifest_path)
    return orch, manifest


def _safe_job_store_log_args(*args: Any, **kwargs: Any) -> tuple[list[str], list[str]]:
    """Summarize job-store call args without logging credential payloads.

    Preconditions:
        * None — accepts arbitrary ``*args`` / ``**kwargs`` from a store call.
    Postconditions:
        * Returns ``(arg_summaries, kw_summaries)`` with identifiers / types only —
          never stringifies nested payloads that may contain secrets.
    """

    def _summarize(value: Any) -> str:
        if isinstance(value, bool) or value is None:
            return repr(value)
        if isinstance(value, (int, float)):
            return repr(value)
        if isinstance(value, str):
            return repr(value) if len(value) <= 64 else f"str(len={len(value)})"
        return f"{type(value).__name__}"

    return [_summarize(a) for a in args], [f"{k}={_summarize(v)}" for k, v in kwargs.items()]


def _best_effort_job_store(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Best-effort job_store call. Store hiccups must never fail the activity.

    Preconditions:
        * ``fn`` should be a callable (programming errors are logged and skipped).
    Postconditions:
        * On success: ``fn(*args, **kwargs)`` has run.
        * On failure / non-callable ``fn``: logs and returns without raising.
    """
    # Pass the real callable (not a name string) so renames stay searchable.
    # Incorrect args for a valid callable are still caught — progress writes
    # must not abort the activity and leave Temporal retries opaque.
    if not callable(fn):
        logger.error("job_store callable is not callable: %r", fn)
        return
    try:
        fn(*args, **kwargs)
    except Exception:
        arg_summaries, kw_summaries = _safe_job_store_log_args(*args, **kwargs)
        logger.exception(
            "job_store.%s failed: args=%s kwargs=%s",
            getattr(fn, "__name__", repr(fn)),
            arg_summaries,
            kw_summaries,
        )


def _record_phase_restored(job_id: str, phase: str, progress: int) -> None:
    """Record a skipped/restored phase progress update on the job store."""
    logger.info("Skipping %s for job=%s (restored from prior_results)", phase, job_id)
    _best_effort_job_store(
        _js.update_job,
        job_id,
        current_phase=phase,
        progress=progress,
        status_text=f"Restored {phase} from previous run",
    )


@activity.defn(name="agent_provisioning_list_manifest_tools")
def list_manifest_tools_activity(manifest_path: str) -> List[Dict[str, Any]]:
    """Return a frozen tool snapshot from the agent manifest (workflow-safe I/O).

    Temporal workflows must not read files directly. This activity loads the
    manifest once so later per-tool activities can use the same
    name/provisioner/config without re-reading a mutable file mid-run.

    Preconditions:
        * ``manifest_path`` is non-empty and readable by ``load_manifest``.
    Postconditions:
        * Returns ``[{"name", "provisioner", "config"}, ...]`` in manifest order.
    """
    assert manifest_path, "manifest_path must be non-empty"
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import load_manifest

    manifest = load_manifest(manifest_path)
    return [
        {
            "name": t.name,
            "provisioner": t.provisioner,
            "config": dict(t.config or {}),
        }
        for t in manifest.tools
    ]


# ---------------------------------------------------------------------------
# Per-agent_id ownership lock — acquired first, released last, by both
# AgentProvisioningWorkflow and AgentDeprovisioningWorkflow (shared/agent_lock.py)
# ---------------------------------------------------------------------------


@activity.defn(name="agent_provisioning_check_existing_environment")
def check_existing_environment_activity(agent_id: str, job_id: Optional[str] = None) -> bool:
    """Report whether ``agent_id`` already has an environment on record (read-only).

    Called by the workflow right after acquiring ``agent_id``'s lock, before
    setup runs, so a later failure's compensation decision can tell "this run
    created everything at ``agent_id`` from scratch" (safe to unconditionally
    tear down) apart from "``agent_id`` already had an environment before
    this run touched anything" (compensating could destroy it).

    Preconditions:
        * ``agent_id`` is non-empty.
        * ``job_id``, when given, is the calling workflow's own job id — used
          only to recognize a container THIS run's own earlier attempt
          labeled (see below); optional and defaulted so activity tasks
          already scheduled (recorded in history with the old 1-arg payload)
          before a rolling deploy still execute correctly against a newer
          worker binary.
    Postconditions:
        * When ``EnvironmentStore`` holds a record for ``agent_id``: returns
          ``True`` unless the record's own ``container_name`` is CONFIRMED
          absent from Docker (a direct ``docker inspect`` probe, not the
          record's ``status`` field). A status other than ``"running"``/
          ``"ready"`` (e.g. ``"stopped"``) still means a container may
          previously have existed for this agent — ``run_setup`` only
          fast-paths on ``"running"``, but ``docker.provision()``'s own
          idempotency state (independent of ``EnvironmentStore``) can still
          resolve to reusing that same underlying container regardless of
          what status this record carries. But a record whose container is
          verifiably GONE is stale metadata with nothing left to protect:
          ``run_setup`` will create an entirely fresh container and overwrite
          the record, so treating the stale record as "pre-existing" would
          instead leak the container THIS run creates (a later failure would
          pass ``tear_down_environment=False`` and skip tearing it down). A
          probe that can't tell (daemon unreachable, timeout — ``None``) is
          treated the same as "alive": conservatively, "might exist".
        * Also returns ``True`` — conservatively, "might exist" — when the
          record location is present but genuinely unreadable (``get()``
          returns ``None`` for that case too, indistinguishable from
          confirmed absence without ``readable()``): the registry being
          unreadable is not proof nothing is there.
        * When ``EnvironmentStore`` holds NO record at all (confirmed
          readable-and-empty) AND the deterministic container name
          (``agent-<agent_id>``, the same name every provisioner/rollback
          path in this codebase uses) is CONFIRMED absent from Docker:
          returns ``False``. The record and the container are two
          independently-losable things — a record can go missing (disk
          issue, manual cleanup, a prior compensation that removed the
          record but not the container) while the container itself, or
          ``DockerProvisionerTool``'s own separate idempotency state, is
          still there; ``docker.provision()`` would then reuse it via its
          own ``_on_reuse`` check regardless of what ``EnvironmentStore``
          says. Skipping this probe would let a later phase's failure
          authorize tearing down (or ``verify_and_remove_orphan``-ing) a
          container that predates this run, just because its record
          happened to be the thing that was lost.
        * When that container is confirmed present (or the probe is
          inconclusive): defers to ``DockerProvisionerTool.is_pre_existing``,
          which consults the container's ``khala.job_id`` label (stamped at
          creation, ``tool_agents/docker_provisioner.py``) when ``job_id`` was
          given — a label matching THIS ``job_id`` means this run's own
          earlier attempt created it (e.g. a resumed job reusing ``job_id``;
          ``start_workflow.py`` derives the Temporal workflow id
          deterministically from it), so it does not predate this run:
          returns ``False``. Anything else (no ``job_id`` given, no label, or
          a different job's label) is treated as possibly pre-existing:
          returns ``True``. See ``is_pre_existing``'s own docstring for the
          full reasoning, including why a different job's label usually
          reflects normal sequential reuse rather than a problem.
        * Never raises (``EnvironmentStore.get``/``readable`` never raise;
          the Docker probes are themselves never-raising and advisory only).
    """
    assert agent_id, "agent_id must be non-empty"
    from agent_team_studio.agent_provisioning_team.shared.environment_store import EnvironmentStore
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        DockerProvisionerTool,
    )

    env_store = EnvironmentStore()
    existing = env_store.get(agent_id)
    if existing is not None:
        return DockerProvisionerTool._container_exists(existing.container_name) is not False
    if not env_store.readable(agent_id):
        return True
    return DockerProvisionerTool.is_pre_existing(agent_id, job_id)


@activity.defn(name="agent_provisioning_acquire_lock")
def acquire_agent_lock_activity(job_id: str, agent_id: str) -> int:
    """Claim exclusive ownership of ``agent_id`` for this workflow run.

    Preconditions:
        * ``job_id`` (a provisioning ``job_id`` or a deprovisioning workflow
          id) and ``agent_id`` are non-empty.
    Postconditions:
        * On return, ``agent_id``'s lock record is owned by ``job_id``, and
          the fencing token now associated with ``agent_id`` (see
          ``AgentLockStore.acquire``) is returned. The calling workflow must
          present this token (or the value of its most recent renewal,
          whichever is more recent) on every subsequent mutating activity
          call and on ``release_agent_lock_activity``.
        * Raises ``RuntimeError`` when a different, non-expired owner
          currently holds the lock — deliberately a plain (retryable)
          exception rather than a non-retryable one, so Temporal's retry
          policy keeps polling with backoff until the lock frees or the
          activity's ``schedule_to_close_timeout`` is exhausted.
    """
    from agent_team_studio.agent_provisioning_team.shared.agent_lock import (
        AgentLockBusyError,
        AgentLockStore,
    )
    from agent_team_studio.agent_provisioning_team.temporal.constants import LOCK_TTL_S

    assert job_id, "job_id must be non-empty"
    assert agent_id, "agent_id must be non-empty"
    activity.heartbeat("acquire-lock")
    try:
        return AgentLockStore(ttl_seconds=LOCK_TTL_S).acquire(agent_id, owner=job_id)
    except AgentLockBusyError as e:
        raise RuntimeError(str(e)) from e


@activity.defn(name="agent_provisioning_release_lock")
def release_agent_lock_activity(
    job_id: str, agent_id: str, fencing_token: Optional[int] = None
) -> None:
    """Release this workflow's ownership of ``agent_id`` (best-effort, idempotent).

    Preconditions:
        * ``job_id`` and ``agent_id`` are non-empty.
    Postconditions:
        * Releases the lock only if ``job_id`` is still the current owner and
          ``fencing_token`` (when given) is not stale; a no-op otherwise
          (already released, owned by someone else, or a stale token —
          defense-in-depth, not load-bearing: the owner check alone already
          rejects a stale caller since owner and token always advance
          together).
        * May raise on a transient I/O failure so Temporal retries — the
          calling workflow wraps this activity in its own try/except so a
          persistent release failure is logged, not fatal to the workflow's
          outcome, and never masks the original failure it is cleaning up
          after.
    """
    from agent_team_studio.agent_provisioning_team.shared.agent_lock import AgentLockStore
    from agent_team_studio.agent_provisioning_team.temporal.constants import LOCK_TTL_S

    assert job_id, "job_id must be non-empty"
    assert agent_id, "agent_id must be non-empty"
    AgentLockStore(ttl_seconds=LOCK_TTL_S).release(
        agent_id, owner=job_id, fencing_token=fencing_token
    )


def _reject_stale_fencing_token(agent_id: str, fencing_token: Optional[int]) -> None:
    """Reject a resource-mutating call whose fencing token has been superseded.

    Preconditions:
        * ``agent_id`` is non-empty when ``fencing_token`` is not ``None``.
    Postconditions:
        * A no-op when ``fencing_token`` is ``None`` — legacy/replay call
          sites recorded before fencing tokens were threaded through are
          unaffected.
        * Otherwise raises ``StaleFencingTokenError`` when ``fencing_token``
          is lower than ``agent_id``'s currently recorded fencing token (see
          ``AgentLockStore.check_fencing_token``).
    """
    if fencing_token is None:
        return
    from agent_team_studio.agent_provisioning_team.shared.agent_lock import AgentLockStore
    from agent_team_studio.agent_provisioning_team.temporal.constants import LOCK_TTL_S

    AgentLockStore(ttl_seconds=LOCK_TTL_S).check_fencing_token(agent_id, fencing_token)


@activity.defn(name="agent_provisioning_setup")
def setup_activity(
    job_id: str,
    agent_id: str,
    manifest_path: str,
    prior_setup: Optional[Dict[str, Any]] = None,
    fencing_token: Optional[int] = None,
) -> Dict[str, Any]:
    """Run (or restore) the Docker/environment setup phase.

    Preconditions:
        * ``job_id`` / ``agent_id`` / ``manifest_path`` are non-empty.
        * When ``prior_setup`` is set, it is a serialized setup phase snapshot
          acceptable to ``restore_setup``.
        * ``fencing_token``, when given, is the calling workflow's current
          lease token on ``agent_id`` (from ``acquire_agent_lock_activity``).
          Checked twice, both before any mutation: first via
          ``_reject_stale_fencing_token`` (``AgentLockStore``'s own record)
          as this activity's first statement, then again inside
          ``run_setup`` against ``EnvironmentStore``/``ProvisionerStateStore``'s
          own high-water marks. A no-op both times when ``fencing_token`` is
          ``None`` (legacy/replay call sites).
    Postconditions:
        * Raises a stale-fencing-token error before any other side effect
          (including restoring from ``prior_setup``) when ``fencing_token``
          is stale.
        * Returns ``{"success": True, "environment": <dump|None>}`` reflecting
          THIS call's own outcome (including its own ``reused`` value) —
          UNLESS a stronger checkpoint from an earlier attempt of this same
          activity already exists (see below), in which case that earlier,
          stronger result is returned instead.
        * Writes setup progress (or restore status) into ``job_store``. The
          durable ``phase_results["setup"]`` checkpoint itself is never
          overwritten once present — only the first call to actually
          checkpoint (whether via ``on_registered`` on a fresh
          registration, or this fallback on the always-reused fast path)
          wins, so a Temporal retry whose fast path reuses what an earlier,
          response-lost attempt of this same activity already created
          (and durably recorded as ``reused=False``) can't replace that
          stronger ownership evidence with its own weaker ``reused=True``.
          The RETURN VALUE mirrors this: when this call's own fast path
          finds that stronger checkpoint already on record, it returns that
          checkpoint's payload rather than its own weaker one — the caller
          (the workflow) makes its ``pre_existing_environment`` correction
          from this activity's own return value, not from ``job_store``
          directly, so returning the weaker ``reused=True`` result here
          would silently drop the stronger evidence for the rest of this
          run even though it is right there on record.
        * Passes this call's own ``job_id`` through to ``run_setup`` so a
          freshly created container is labeled with it (see
          ``docker_provisioner.JOB_ID_LABEL``), giving
          ``compensate_activity``/``check_existing_environment_activity`` a
          durable, container-native way to attribute the container to this
          attempt even if the local idempotency state that would otherwise
          prove it never gets written.
        * Raises ``RuntimeError`` when a fresh setup fails.
        * When ``fencing_token`` is given and stale, raises either
          :class:`~agent_team_studio.agent_provisioning_team.shared.agent_lock.StaleFencingTokenError`
          (from the early ``_reject_stale_fencing_token`` check) or
          :class:`~agent_team_studio.agent_provisioning_team.shared.fencing.StaleFencingTokenError`
          (from ``run_setup``'s deeper store checks) — both propagated, not
          converted to ``RuntimeError``.
    """
    assert job_id, "job_id must be non-empty"
    assert agent_id, "agent_id must be non-empty"
    assert manifest_path, "manifest_path must be non-empty"
    _reject_stale_fencing_token(agent_id, fencing_token)
    from agent_team_studio.agent_provisioning_team.phases.setup import run_setup
    from agent_team_studio.agent_provisioning_team.shared.phase_state import restore_setup

    _best_effort_job_store(_js.mark_job_running, job_id)

    if prior_setup is not None:
        snap = restore_setup(prior_setup)
        _record_phase_restored(job_id, "setup", 15)
        return {
            "success": snap.success,
            "environment": snap.environment.model_dump() if snap.environment else None,
        }

    _best_effort_job_store(
        _js.update_job,
        job_id,
        current_phase="setup",
        progress=5,
        status_text="Creating Docker environment...",
    )
    orch, manifest = _load_ctx(manifest_path)
    activity.heartbeat("setup")

    checkpointed = False

    def _checkpoint_on_register(env_info) -> None:
        # Runs inside run_setup's own rollback boundary: if this durable
        # checkpoint write fails or the activity is cancelled here, run_setup
        # tears the container/env record it just registered back down instead
        # of leaking it. Only reached on a freshly created/registered
        # environment, never the already-running fast path below.
        nonlocal checkpointed
        _js.add_completed_phase(
            job_id,
            "setup",
            {"success": True, "environment": env_info.model_dump() if env_info else None},
        )
        checkpointed = True

    result = run_setup(
        agent_id=agent_id,
        manifest=manifest,
        environment_store=orch.environment_store,
        docker_provisioner=orch.tool_agents.get("docker_provisioner"),
        on_registered=_checkpoint_on_register,
        job_id=job_id,
        fencing_token=fencing_token,
    )
    if not result.success:
        raise RuntimeError(f"setup failed: {result.error}")

    payload = {
        "success": True,
        "environment": result.environment.model_dump() if result.environment else None,
    }
    if not checkpointed:
        # Fast path: an already-running environment was reused, so nothing new
        # was created here — a checkpoint failure has nothing to leak, and a
        # bare durable write (Temporal retries the whole activity) suffices.
        # But don't blindly overwrite: if an EARLIER attempt of this same
        # activity already durably checkpointed "setup" via on_registered
        # (its own completion response then got lost, so Temporal retried
        # the whole activity), that checkpoint's environment carries
        # reused=False — proof this job's own earlier try created the
        # container fresh. This retry's fast path reuses that same
        # container (reused=True here) and must not overwrite the stronger
        # ownership evidence already on record with this weaker one, or a
        # later resume reading phase_results would lose track of the fact
        # that this job created the environment, not something pre-existing.
        existing_job = _js.get_job(job_id)
        already_checkpointed = "setup" in (existing_job.get("completed_phases") or [])
        if not already_checkpointed:
            _js.add_completed_phase(job_id, "setup", payload)
        else:
            # Also RETURN the stronger checkpoint, not just preserve it on
            # disk: the workflow corrects a conservative
            # pre_existing_environment from THIS activity's own return
            # value (environment_dump.get("reused") is False), never by
            # reading job_store directly — so if this fast-path retry
            # returned its own weaker reused=True payload instead, that
            # correction would silently never fire for the rest of this
            # run, even though the stronger reused=False evidence is
            # sitting right here on record.
            existing_setup_result = (existing_job.get("phase_results") or {}).get("setup")
            if isinstance(existing_setup_result, dict):
                payload = existing_setup_result
    _best_effort_job_store(_js.update_job, job_id, progress=15, status_text="Setup complete")
    return payload


@activity.defn(name="agent_provisioning_credentials")
def credentials_activity(
    job_id: str,
    agent_id: str,
    manifest_path: str,
    prior_credentials: Optional[Dict[str, Any]] = None,
    tool_specs: Optional[List[Dict[str, Any]]] = None,
    fencing_token: Optional[int] = None,
) -> Dict[str, Any]:
    """Generate (or restore) per-tool credentials for the agent.

    Preconditions:
        * ``job_id`` / ``agent_id`` / ``manifest_path`` are non-empty.
        * When ``prior_credentials`` is set, it is a credential-phase snapshot
          acceptable to ``restore_credentials``.
        * When ``tool_specs`` is set (fresh generate path), each entry has a
          non-empty ``name`` — the same frozen snapshot used for tool fan-out.
        * ``fencing_token``, when given, is the calling workflow's current
          lease token on ``agent_id``. Checked twice, both before any
          mutation: first via ``_reject_stale_fencing_token``
          (``AgentLockStore``'s own record) as this activity's first
          statement, then again inside ``run_credential_generation`` against
          ``CredentialStore``'s own high-water mark. A no-op both times when
          ``fencing_token`` is ``None`` (legacy/replay call sites).
    Postconditions:
        * Raises a stale-fencing-token error before any other side effect
          (including restoring from ``prior_credentials``) when
          ``fencing_token`` is stale.
        * Returns ``{"success": True, "credentials": {tool_name: dump, ...}}``.
        * Job-store checkpoint never stores plaintext secrets.
        * Raises ``RuntimeError`` when credential generation fails.
    """
    assert job_id, "job_id must be non-empty"
    assert agent_id, "agent_id must be non-empty"
    assert manifest_path, "manifest_path must be non-empty"
    _reject_stale_fencing_token(agent_id, fencing_token)
    from agent_team_studio.agent_provisioning_team.phases.credential_generation import (
        get_stored_credentials,
        run_credential_generation,
    )
    from agent_team_studio.agent_provisioning_team.shared.phase_state import restore_credentials

    if prior_credentials is not None:
        snap = restore_credentials(prior_credentials)
        _record_phase_restored(job_id, "credential_generation", 30)
        # Resume reloads secrets from the Fernet CredentialStore — never from
        # job-store phase_results (which must stay redacted / reference-only).
        stored = get_stored_credentials(agent_id)
        if not stored and snap.credentials:
            # Legacy checkpoints that still carry plaintext: migrate into the
            # CredentialStore once, then overwrite the job-store checkpoint so
            # subsequent resumes cannot re-read plaintext.
            # ``store_credentials`` overwrites per tool and is safe under Temporal
            # activity retry — do not swallow store failures (retry until durable).
            # Remove this branch once job-store credential_generation checkpoints
            # no longer embed plaintext ``credentials`` maps (grep phase_results
            # for non-empty credentials dumps under credential_generation).
            from agent_team_studio.agent_provisioning_team.phases.credential_generation import (
                store_credentials_payload,
            )

            for name, cred in snap.credentials.items():
                store_credentials_payload(
                    agent_id, name, cred.model_dump(), fencing_token=fencing_token
                )
            tool_names = sorted(snap.credentials.keys()) or list(snap.tool_names)
            _js.add_completed_phase(
                job_id,
                "credential_generation",
                {"success": True, "tool_names": tool_names, "credentials": {}},
            )
            return {
                "success": snap.success,
                "credentials": {k: v.model_dump() for k, v in snap.credentials.items()},
            }
        if not stored:
            if not snap.credentials:
                raise RuntimeError(
                    f"cannot restore credential_generation for agent={agent_id}: "
                    "prior checkpoint has no credentials and CredentialStore is empty"
                )
            raise RuntimeError(
                f"cannot restore credential_generation for agent={agent_id}: "
                "CredentialStore has no credentials"
            )
        return {
            "success": True,
            "credentials": {k: v.model_dump() for k, v in stored.items()},
        }

    _best_effort_job_store(
        _js.update_job,
        job_id,
        current_phase="credential_generation",
        progress=20,
        status_text="Generating credentials...",
    )
    orch, manifest = _load_ctx(manifest_path)
    frozen_names: Optional[List[str]] = None
    if tool_specs is not None:
        frozen_names = [str(s.get("name") or "") for s in tool_specs]
        assert all(frozen_names), "tool_specs entries must include non-empty name"
    activity.heartbeat("credentials")
    result = run_credential_generation(
        agent_id=agent_id,
        manifest=manifest,
        credential_store=orch.credential_store,
        tool_names=frozen_names,
        fencing_token=fencing_token,
    )
    if not result.success:
        raise RuntimeError(f"credential generation failed: {result.error}")

    # Workflow activities still receive full credentials in-memory for this run.
    # Job-store checkpoint stores only tool-name references — no plaintext secrets.
    checkpoint = {
        "success": True,
        "tool_names": sorted(result.credentials.keys()),
        "credentials": {},
    }
    _js.add_completed_phase(job_id, "credential_generation", checkpoint)
    _best_effort_job_store(_js.update_job, job_id, progress=30, status_text="Credentials generated")
    return {
        "success": True,
        "credentials": {k: v.model_dump() for k, v in result.credentials.items()},
    }


@activity.defn(name="agent_provisioning_provision_tool")
def provision_tool_activity(
    job_id: str,
    agent_id: str,
    tool_name: str,
    credentials_dump: Dict[str, Any],
    tools_total: int,
    provisioner: str,
    tool_config: Optional[Dict[str, Any]] = None,
    fencing_token: Optional[int] = None,
) -> Dict[str, Any]:
    """Provision a single tool — one activity per tool so fan-out is natural.

    Preconditions:
        * ``tool_name`` / ``provisioner`` are non-empty (from the workflow
          manifest snapshot — not re-read from disk).
        * ``credentials_dump`` is a serializable ``GeneratedCredentials`` dump
          for this tool.
        * ``tools_total`` must be ``> 0``.
        * ``fencing_token``, when given, is the calling workflow's current
          lease token on ``agent_id``. Checked twice, both before any
          mutation: first via ``_reject_stale_fencing_token``
          (``AgentLockStore``'s own record) as this activity's first
          statement, then again inside ``agent.provision(...)`` against
          this tool's ``ProvisionerStateStore`` high-water mark. A no-op
          both times when ``fencing_token`` is ``None`` (legacy/replay call
          sites).
    Postconditions:
        * Raises a stale-fencing-token error before any other side effect
          when ``fencing_token`` is stale.
        * Returns ``ToolProvisionResult.model_dump()`` from the provisioner
          with ``provisioner_key`` set to the registry key (needed by
          ``compensate()`` — built-in provisioners leave it ``None``).
        * Does **not** write ``EnvironmentStore`` — parallel fan-out can run in
          different worker processes, so tool lists are recorded once after the
          gather in ``record_account_provisioning_activity``.
        * Raises ``RuntimeError`` when the provisioner is unknown.
        * Updates ``job_store`` with the current tool / phase progress.
          Does not write ``tools_completed`` — parallel fan-out indexes are not
          completion counts and would race/regress under ``asyncio.gather``.
        * When ``job_id`` is truthy AND ``provisioner`` is
          ``"docker_provisioner"``, injects ``job_id`` into ``config`` under
          ``docker_provisioner.JOB_ID_CONFIG_KEY`` before calling
          ``agent.provision(...)``, so ``DockerProvisionerTool`` can stamp
          the ``khala.job_id`` container label used to disambiguate
          self-leaked containers from pre-existing ones during compensation.
          Scoped to that one provisioner specifically — not injected
          generically into every provisioner's ``config`` — because at least
          one other provisioner (``generic_provisioner``) echoes its whole
          ``config`` dict verbatim into persisted/returned state
          (``credentials.extra``/``details``), which is not redacted for an
          unrecognized key like this one; injecting unconditionally would
          leak this internal id into checkpoints and API responses.
        * When ``fencing_token`` is given and stale, raises either
          :class:`~agent_team_studio.agent_provisioning_team.shared.agent_lock.StaleFencingTokenError`
          or :class:`~agent_team_studio.agent_provisioning_team.shared.fencing.StaleFencingTokenError`
          (see the two check points above) — both registered as
          non-retryable (``TOOL_RETRY_POLICY``) so Temporal does not keep
          retrying a doomed-to-fail-again fan-out call.
    """
    from agent_team_studio.agent_provisioning_team.models import GeneratedCredentials
    from agent_team_studio.agent_provisioning_team.shared.tool_agent_registry import (
        build_default_tool_agents,
    )
    from agent_team_studio.agent_provisioning_team.tool_agents.docker_provisioner import (
        JOB_ID_CONFIG_KEY,
    )

    assert tool_name, "tool_name must be non-empty"
    assert provisioner, "provisioner must be non-empty"
    assert tools_total > 0, "tools_total must be > 0"
    _reject_stale_fencing_token(agent_id, fencing_token)
    _best_effort_job_store(
        _js.update_job,
        job_id,
        current_phase="account_provisioning",
        current_tool=tool_name,
        tools_total=tools_total,
        status_text=f"Provisioning {tool_name}...",
    )

    provisioners = build_default_tool_agents()
    agent = provisioners.get(provisioner)
    if agent is None:
        raise RuntimeError(f"unknown provisioner {provisioner}")

    creds = GeneratedCredentials.model_validate(credentials_dump)

    tool_config_dict = dict(tool_config or {})
    if job_id and provisioner == "docker_provisioner":
        tool_config_dict[JOB_ID_CONFIG_KEY] = job_id

    activity.heartbeat(f"provisioning {tool_name}")
    result = agent.provision(
        agent_id=agent_id,
        config=tool_config_dict,
        credentials=creds,
        fencing_token=fencing_token,
    )
    # Mirror run_account_provisioning: stamp the registry key so compensate()
    # can look the provisioner back up (built-ins leave provisioner_key=None).
    # Also force tool_name to the snapshot name — provisioners may return
    # their own stem (e.g. generic_provisioner → "generic") which would break
    # resume tool-set matching and EnvironmentStore recording.
    result.provisioner_key = provisioner
    result.tool_name = tool_name
    return result.model_dump()


@activity.defn(name="agent_provisioning_audit")
def audit_activity(
    job_id: str,
    agent_id: str,
    manifest_path: str,
    tool_results_dump: List[Dict[str, Any]],
    prior_audit: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run (or restore) the access-audit phase after tools are provisioned.

    Preconditions:
        * ``tool_results_dump`` entries are serializable ``ToolProvisionResult``
          dumps when ``prior_audit`` is absent.
        * When ``prior_audit`` is set, it is acceptable to ``restore_access_audit``.
    Postconditions:
        * Returns the ``AccessAuditResult`` dump.
        * Records the phase in ``job_store`` on a fresh audit run.
    """
    assert job_id, "job_id must be non-empty"
    assert agent_id, "agent_id must be non-empty"
    assert manifest_path, "manifest_path must be non-empty"
    from agent_team_studio.agent_provisioning_team.models import ToolProvisionResult
    from agent_team_studio.agent_provisioning_team.phases.access_audit import run_access_audit
    from agent_team_studio.agent_provisioning_team.shared.phase_state import restore_access_audit

    if prior_audit is not None:
        result = restore_access_audit(prior_audit)
        _record_phase_restored(job_id, "access_audit", 75)
        return result.model_dump()

    _best_effort_job_store(
        _js.update_job,
        job_id,
        current_phase="access_audit",
        progress=70,
        status_text="Auditing access permissions...",
    )
    tool_results = [ToolProvisionResult.model_validate(t) for t in tool_results_dump]
    activity.heartbeat("access_audit")
    result = run_access_audit(
        agent_id=agent_id,
        tool_results=tool_results,
    )
    payload = result.model_dump()
    _js.add_completed_phase(job_id, "access_audit", payload)
    _best_effort_job_store(_js.update_job, job_id, progress=80, status_text="Access audit complete")
    return payload


@activity.defn(name="agent_provisioning_documentation")
def documentation_activity(
    job_id: str,
    agent_id: str,
    manifest_path: str,
    credentials_dump: Dict[str, Dict[str, Any]],
    tool_results_dump: List[Dict[str, Any]],
    workspace_path: str,
    prior_documentation: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Generate (or restore) onboarding documentation for the agent.

    Preconditions:
        * ``credentials_dump`` / ``tool_results_dump`` match the models used by
          ``run_documentation`` when ``prior_documentation`` is absent.
        * ``workspace_path`` is a non-empty path string.
    Postconditions:
        * Returns ``{"success": <bool>, "onboarding": <dump|None>}``.
        * Records the documentation phase in ``job_store`` on a fresh run.
    """
    assert job_id, "job_id must be non-empty"
    assert agent_id, "agent_id must be non-empty"
    assert manifest_path, "manifest_path must be non-empty"
    assert workspace_path, "workspace_path must be non-empty"
    from agent_team_studio.agent_provisioning_team.models import (
        GeneratedCredentials,
        ToolProvisionResult,
    )
    from agent_team_studio.agent_provisioning_team.phases.documentation import run_documentation
    from agent_team_studio.agent_provisioning_team.shared.phase_state import restore_documentation
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import load_manifest

    if prior_documentation is not None:
        snap = restore_documentation(prior_documentation)
        _record_phase_restored(job_id, "documentation", 90)
        return {
            "success": snap.success,
            "onboarding": snap.onboarding.model_dump() if snap.onboarding else None,
        }

    _best_effort_job_store(
        _js.update_job,
        job_id,
        current_phase="documentation",
        progress=85,
        status_text="Generating onboarding documentation...",
    )
    manifest = load_manifest(manifest_path)
    credentials = {k: GeneratedCredentials.model_validate(v) for k, v in credentials_dump.items()}
    tool_results = [ToolProvisionResult.model_validate(t) for t in tool_results_dump]
    activity.heartbeat("documentation")
    result = run_documentation(
        agent_id=agent_id,
        manifest=manifest,
        credentials=credentials,
        tool_results=tool_results,
        workspace_path=workspace_path,
    )
    payload = {
        "success": result.success,
        "onboarding": result.onboarding.model_dump() if result.onboarding else None,
    }
    _js.add_completed_phase(job_id, "documentation", payload)
    _best_effort_job_store(
        _js.update_job, job_id, progress=92, status_text="Documentation complete"
    )
    return payload


@activity.defn(name="agent_provisioning_deliver")
def deliver_activity(
    job_id: str,
    agent_id: str,
    environment_dump: Optional[Dict[str, Any]],
    credentials_dump: Dict[str, Dict[str, Any]],
    tool_results_dump: List[Dict[str, Any]],
    audit_dump: Optional[Dict[str, Any]],
    onboarding_dump: Optional[Dict[str, Any]],
    fencing_token: Optional[int] = None,
) -> Dict[str, Any]:
    """Finalize provisioning and mark the job completed or failed.

    Preconditions:
        * Upstream phase dumps (environment / credentials / tools / audit /
          onboarding) are None or valid model dumps for the deliver phase.
    Postconditions:
        * Returns ``{"success": <bool>, "error": <str|None>}``.
        * Marks the job completed (redacted result) or failed in ``job_store``.
        * Raises when the terminal job-store write fails so Temporal retries
          (status must not stay running after a successful deliver).
        * ``fencing_token``, when given, is checked twice, both before any
          mutation: first via ``_reject_stale_fencing_token``
          (``AgentLockStore``'s own record) as this activity's first
          statement — closing the window where the ``EnvironmentStore``
          high-water mark has not yet been bumped by a reclaiming owner's
          setup, so ``run_deliver``'s own ``update_status`` check alone could
          still accept a stale caller — then again inside ``run_deliver``
          against ``EnvironmentStore``'s high-water mark. A no-op both times
          when ``fencing_token`` is ``None`` (legacy/replay call sites).
        * When ``fencing_token`` is given and stale, raises either
          :class:`~agent_team_studio.agent_provisioning_team.shared.agent_lock.StaleFencingTokenError`
          or :class:`~agent_team_studio.agent_provisioning_team.shared.fencing.StaleFencingTokenError`
          (see the two check points above).
    """
    assert job_id, "job_id must be non-empty"
    assert agent_id, "agent_id must be non-empty"
    _reject_stale_fencing_token(agent_id, fencing_token)
    from agent_team_studio.agent_provisioning_team.models import (
        AccessAuditResult,
        EnvironmentInfo,
        GeneratedCredentials,
        OnboardingPacket,
        ToolProvisionResult,
    )
    from agent_team_studio.agent_provisioning_team.phases.deliver import (
        build_final_result,
        redact_credentials_for_response,
        run_deliver,
    )
    from agent_team_studio.agent_provisioning_team.shared.environment_store import EnvironmentStore

    _best_effort_job_store(
        _js.update_job,
        job_id,
        current_phase="deliver",
        progress=95,
        status_text="Finalizing provisioning...",
    )

    environment = EnvironmentInfo.model_validate(environment_dump) if environment_dump else None
    credentials = {k: GeneratedCredentials.model_validate(v) for k, v in credentials_dump.items()}
    tool_results = [ToolProvisionResult.model_validate(t) for t in tool_results_dump]
    audit = AccessAuditResult.model_validate(audit_dump) if audit_dump else None
    onboarding = OnboardingPacket.model_validate(onboarding_dump) if onboarding_dump else None

    activity.heartbeat("deliver")
    deliver_result = run_deliver(
        agent_id=agent_id,
        environment=environment,
        credentials=credentials,
        tool_results=tool_results,
        access_audit=audit,
        onboarding=onboarding,
        environment_store=EnvironmentStore(),
        fencing_token=fencing_token,
    )

    final = build_final_result(
        agent_id=agent_id,
        environment=environment,
        credentials=credentials,
        tool_results=tool_results,
        access_audit=audit,
        onboarding=onboarding,
        deliver_result=deliver_result,
    )

    if final.success:
        redacted = redact_credentials_for_response(final)
        _js.mark_job_completed(job_id, result=redacted.model_dump())
    else:
        _js.mark_job_failed(job_id, error=final.error or "Provisioning failed")

    return {"success": final.success, "error": final.error}


@activity.defn(name="agent_provisioning_record_account_provisioning")
def record_account_provisioning_activity(
    job_id: str,
    tool_results_dump: List[Dict[str, Any]],
    agent_id: str = "",
    fencing_token: Optional[int] = None,
) -> Dict[str, Any]:
    """Persist a successful account-provisioning checkpoint for ``/resume``.

    Preconditions:
        * ``job_id`` is non-empty.
        * ``tool_results_dump`` is the serializable per-tool result list.
        * ``agent_id`` is non-empty when environment tool recording is required.
        * ``fencing_token``, when given, is the calling workflow's current
          lease token on ``agent_id``. Checked twice, both before any
          mutation: first via ``_reject_stale_fencing_token``
          (``AgentLockStore``'s own record) as this activity's first
          statement, then again inside ``store_credentials_payload`` and
          ``EnvironmentStore.add_tools`` against their own high-water marks.
          A no-op both times when ``fencing_token`` is ``None``
          (legacy/replay call sites).
    Postconditions:
        * Raises a stale-fencing-token error before any other side effect
          when ``fencing_token`` is stale.
        * ``completed_phases`` includes ``account_provisioning`` and
          ``phase_results`` carries sanitized tool results (no plaintext
          ``credentials``; sensitive ``details`` redacted).
        * When ``agent_id`` is set, successful tool results that carry a
          ``credentials`` dump are written to ``CredentialStore`` (including
          enriched fields) so resume can rebuild documentation/deliver material
          after the checkpoint strips plaintext.
        * Job progress reports ``tools_completed`` / ``tools_total`` from the
          finished result list so status polls no longer show ``0/N``.
        * When ``agent_id`` is set, successful tool names are written once via
          ``EnvironmentStore.add_tools`` (safe after parallel fan-out).
        * Raises when job-store / credential-store writes fail so Temporal
          retries the checkpoint before later phases run.
        * When ``fencing_token`` is given and stale, raises either
          :class:`~agent_team_studio.agent_provisioning_team.shared.agent_lock.StaleFencingTokenError`
          or :class:`~agent_team_studio.agent_provisioning_team.shared.fencing.StaleFencingTokenError`
          (see the two check points above).
    """
    assert job_id, "job_id must be non-empty"
    _reject_stale_fencing_token(agent_id, fencing_token)
    from agent_team_studio.agent_provisioning_team.phases.deliver import (
        sanitize_tool_results_for_checkpoint,
    )

    results = list(tool_results_dump)
    tools_total = len(results)
    tools_completed = sum(1 for r in results if isinstance(r, dict) and r.get("success"))
    # Persist provisioner-enriched credentials (connection_string, SSH keys, …)
    # into CredentialStore before the job-store checkpoint strips them. Resume
    # after this phase reloads enrichment from the store — sanitized
    # ``tool_results`` intentionally keep ``credentials=None``.
    if agent_id:
        from agent_team_studio.agent_provisioning_team.phases.credential_generation import (
            store_credentials_payload,
        )
        from agent_team_studio.agent_provisioning_team.shared.environment_store import (
            EnvironmentStore,
        )

        for raw in results:
            if not isinstance(raw, dict) or not raw.get("success"):
                continue
            tool_name = raw.get("tool_name")
            creds = raw.get("credentials")
            if isinstance(tool_name, str) and tool_name and isinstance(creds, dict):
                store_credentials_payload(agent_id, tool_name, creds, fencing_token=fencing_token)

        names = [
            r.get("tool_name")
            for r in results
            if isinstance(r, dict) and r.get("success") and r.get("tool_name")
        ]
        EnvironmentStore().add_tools(
            agent_id, [n for n in names if isinstance(n, str)], fencing_token=fencing_token
        )

    # Job-store checkpoint must not retain plaintext credentials / connection strings.
    sanitized = sanitize_tool_results_for_checkpoint(results)
    payload = {"success": True, "tool_results": sanitized}
    _js.add_completed_phase(job_id, "account_provisioning", payload)
    _js.update_job(
        job_id,
        progress=60,
        status_text="Account provisioning complete",
        current_tool=None,
        tools_completed=tools_completed,
        tools_total=tools_total,
    )
    return payload


@activity.defn(name="agent_provisioning_compensate")
def compensate_activity(
    agent_id: str,
    succeeded_tools: List[Dict[str, Any]],
    job_id: Optional[str] = None,
    tear_down_environment: bool = True,
    fencing_token: Optional[int] = None,
) -> None:
    """Roll back a partially-provisioned agent (best effort).

    Preconditions:
        * ``agent_id`` identifies the agent whose tools should be rolled back.
        * ``fencing_token``, when given, is the calling workflow's current
          lease token on ``agent_id``. Checked twice, both before any
          mutation: first via ``_reject_stale_fencing_token``
          (``AgentLockStore``'s own record) as this activity's first
          statement (before constructing ``ProvisioningOrchestrator``), then
          again inside ``ProvisioningOrchestrator.compensate``, per-resource,
          against each store's own high-water mark. A no-op both times when
          ``fencing_token`` is ``None`` (legacy/replay call sites).
        * ``succeeded_tools`` entries are dicts with ``tool_name`` and
          ``provisioner_key`` (registry key, e.g. ``"postgres_provisioner"``).
          The orchestrator looks provisioners up by that registry key. An
          optional ``reused`` flag (from the provisioner's own
          ``details.reused``) marks an entry as idempotently reused rather
          than created by this attempt — ``ProvisioningOrchestrator.compensate``
          excludes those from rollback, since tearing one down would destroy
          an account that predates this attempt.
        * When ``job_id`` is set, it identifies the job whose completed-phase
          checkpoints must be cleared after teardown.
        * ``tear_down_environment`` is ``False`` when ``agent_id``'s Docker
          environment predates this workflow run (e.g. a re-run against an
          already-delivered agent) and must be preserved — ``succeeded_tools``
          rollback still runs either way, since those are always this
          attempt's own creation.
    Postconditions:
        * Raises ``StaleFencingTokenError`` before any other side effect
          (before ``ProvisioningOrchestrator`` is even constructed) when
          ``fencing_token`` is stale.
        * Invokes ``ProvisioningOrchestrator.compensate`` once, passing
          ``tear_down_environment`` through. Failures inside compensation are
          absorbed by the orchestrator (best effort) — every step there,
          including the Docker teardown, is individually try/except-wrapped
          so one failing step doesn't block the others — including a stale
          ``fencing_token`` for any individual resource, which is logged and
          skipped rather than raised (see
          ``ProvisioningOrchestrator.compensate``'s per-resource fencing
          contract).
        * When ``job_id`` is set: clears ``completed_phases`` / ``phase_results``
          immediately after ``compensate`` returns — BEFORE the docker
          verification below, which can raise — so a later ``/resume`` can
          never skip credential_generation (or setup) on the strength of
          checkpoints that survived only because this activity happened to
          fail on its final check. ``compensate`` may already have torn down
          CredentialStore / Docker / the env record by that point regardless
          of whether verification below then raises. Missing jobs are a
          no-op; job-store write failures raise for Temporal retry.
        * Independent of ``tear_down_environment``: when a docker provisioner
          is registered and ``EnvironmentStore`` confirms NO record currently
          exists for ``agent_id`` (checked live, right here — not inferred
          from ``tear_down_environment`` or from how ``compensate`` happened
          to fare), raises ``RuntimeError`` if the docker provisioner still
          confirms (or can't rule out) a live container for ``agent_id`` —
          checked directly against the deterministic container name via
          ``verify_and_remove_orphan``, not merely the provisioner's own
          idempotency state, since a container can exist with no
          corresponding state row (e.g. persisting that state failed right
          after ``docker run`` succeeded, and the best-effort removal that
          followed that specific failure also failed) — a state-only check
          would then see nothing to clean up and report false success.
          Raising here (rather than silently returning as if teardown
          succeeded) lets Temporal's own retry policy give teardown another
          attempt instead of leaking the container — the workflow's
          setup-failure path in particular relies on this activity actually
          retrying when its local rollback fails, INCLUDING when
          ``tear_down_environment`` was conservatively ``False`` because the
          pre-run ownership check itself failed rather than because an
          environment genuinely predates this run.
        * A live record for ``agent_id`` skips this verification entirely: a
          container that record still legitimately references must not be
          probed-and-removed by name out from under it. Checking
          ``EnvironmentStore`` fresh here (rather than trusting
          ``tear_down_environment`` or inferring safety from whether
          ``compensate`` happened to raise internally) is deliberate: a
          genuinely pre-existing environment leaves its record untouched by
          ``compensate`` either way, so it always still has one; a record
          whose removal itself failed inside ``compensate`` also still has
          one; only the "nothing ever got registered, or it did and was
          since removed" case has none, and that is exactly when a container
          matching the name might be orphaned. When no record exists at all
          AND ``tear_down_environment`` is ``False`` (ownership was never
          settled), defers to ``DockerProvisionerTool.is_pre_existing`` —
          the same shared, label-aware ownership check
          ``check_existing_environment_activity`` uses — since a name match
          alone cannot tell "predates this run" apart from "this run's own
          leak"; see that method's own docstring for the full reasoning.
          When ``tear_down_environment`` is ``True``, ownership is already
          settled — this run is known to have created/own the environment —
          so that check is skipped entirely: a surviving container in that
          case is unconditionally this run's own leaked orphan to reclaim
          via ``verify_and_remove_orphan``.
    """
    _reject_stale_fencing_token(agent_id, fencing_token)
    from agent_team_studio.agent_provisioning_team.orchestrator import ProvisioningOrchestrator

    orch = ProvisioningOrchestrator()
    shims = [
        SimpleNamespace(
            tool_name=t.get("tool_name", ""),
            provisioner_key=t.get("provisioner_key"),
            success=True,
            # Mirrors ToolProvisionResult's shape (a `.details` dict) so
            # `compensate` reads reuse the same way for both the Temporal
            # shim path here and the in-process ToolProvisionResult path.
            details={"reused": bool(t.get("reused", False))},
        )
        for t in succeeded_tools
    ]
    orch.compensate(
        agent_id, shims, tear_down_environment=tear_down_environment, fencing_token=fencing_token
    )

    if job_id:
        # Compensate tears down Docker, env, and CredentialStore — no prior
        # phase remains safe to skip on resume. Cleared before the
        # (possibly-raising) verification below so a raise there can never
        # leave stale, resumable checkpoints over already-torn-down state.
        _js.clear_completed_phases(job_id)

    docker = orch.tool_agents.get("docker_provisioner")
    if docker is not None:
        env_store = orch.environment_store
        # Live, not the (possibly stale/conservative) tear_down_environment
        # flag: unreadable counts as "might have a record" — conservative,
        # same as everywhere else this ambiguity shows up.
        record_may_exist = env_store.get(agent_id) is not None or not env_store.readable(agent_id)
        if not record_may_exist and not tear_down_environment:
            # No EnvironmentStore record, and ownership is still ambiguous
            # (tear_down_environment=False) — but the record and the
            # container are independently losable (mirrors
            # check_existing_environment_activity's own reasoning): a
            # pre-existing container can still be sitting there under the
            # deterministic name with no record at all, e.g. the setup
            # name-conflict path where Docker/idempotency state was lost
            # but the container itself predates this run. Defer to the same
            # shared, label-aware ownership check before concluding there is
            # nothing left to protect.
            #
            # When tear_down_environment=True, ownership is already settled
            # — this run is known to have created/own the environment — so
            # this check must NOT run: a container that still exists there
            # is our own leaked orphan to reclaim, not something to protect
            # from verify_and_remove_orphan.
            record_may_exist = docker.is_pre_existing(agent_id, job_id)
        if not record_may_exist and not docker.verify_and_remove_orphan(agent_id):
            raise RuntimeError(
                f"compensate_activity: docker teardown for agent_id={agent_id!r} did not "
                "complete (container still confirmed alive, or its state is unknown, "
                "after compensate)"
            )


@activity.defn(name="agent_provisioning_mark_job_failed")
def mark_job_failed_activity(job_id: str, error: str) -> None:
    """Record a terminal failure for a provisioning job in ``job_store``.

    Used when the workflow aborts before ``deliver_activity`` (e.g. after tool
    compensation) so ``GET /provision/status/{job_id}`` does not stay ``running``.

    Preconditions:
        * ``job_id`` is non-empty.
        * ``error`` is a non-empty human-readable failure reason.
    Postconditions:
        * ``mark_job_failed`` is written to ``job_store`` so status polls leave
          ``running``/``pending``.
        * Raises when the job-store write fails so Temporal retries the
          terminal status update before the workflow abort completes.
    """
    assert job_id, "job_id must be non-empty"
    assert error, "error must be non-empty"
    _js.mark_job_failed(job_id, error=error)


# ---------------------------------------------------------------------------
# Deprovision — single activity wrapping the orchestrator's teardown
# ---------------------------------------------------------------------------


@activity.defn(name="agent_provisioning_deprovision")
def deprovision_activity(
    agent_id: str, force: bool = False, fencing_token: Optional[int] = None
) -> Dict[str, Any]:
    """Deprovision an agent's resources durably.

    Thin durable wrapper over ``ProvisioningOrchestrator.deprovision`` — which
    already deprovisions each tool, tears down the Docker environment, and
    removes encrypted credentials + the environment record, aggregating
    best-effort errors. Kept as a single activity rather than a per-tool fan-out
    because deprovision is fast and the existing method already reports per-tool
    success in its ``details``.

    Preconditions:
        * ``agent_id`` is a non-empty string identifying a (possibly already
          partially removed) agent.
        * Runs inside a Temporal activity worker for the Agent Provisioning
          task queue.
        * ``fencing_token``, when given, is the calling workflow's current
          lease token on ``agent_id``. Checked twice, both before any
          mutation: first via ``_reject_stale_fencing_token``
          (``AgentLockStore``'s own record) as this activity's first
          statement (before constructing ``ProvisioningOrchestrator``), then
          again inside ``ProvisioningOrchestrator.deprovision`` against each
          resource's own high-water mark. A no-op both times when
          ``fencing_token`` is ``None`` (legacy/replay call sites).
    Postconditions:
        * Raises ``StaleFencingTokenError`` before any other side effect
          (before ``ProvisioningOrchestrator`` is even constructed) when
          ``fencing_token`` is stale.
        * Returns ``DeprovisionResponse.model_dump()`` — a JSON-serializable dict
          with ``agent_id``/``success``/``details``/``error``. Cleanup is
          best-effort: ``success`` is ``True`` when no tool errored or ``force``
          was set. The activity does not raise on ordinary partial-cleanup
          failure (the response carries the error), so Temporal does not retry
          a run that was intentionally reported as a soft failure.
        * Exception to the above: when ``fencing_token`` is given and the
          Docker/credential/environment teardown rejects it as stale, this
          activity DOES raise
          :class:`~agent_team_studio.agent_provisioning_team.shared.fencing.StaleFencingTokenError`
          (registered non-retryable) rather than folding it into a soft
          ``success=False`` response — a resumed-but-stale caller must get a
          clean, non-retryable failure, not an infinite retry into the same
          rejection. Tool-provisioner-level rejections (via
          ``deprovision_tools``) remain folded into the per-provisioner
          ``details["tools"]`` map, unchanged — each provisioner tracks its
          own fencing high-water mark independently, so one's rejection must
          not abort teardown of the others.
        * Heartbeats (and checks ``activity.is_cancelled()``) between each
          per-tool teardown call via a checkpoint passed into the orchestrator.
          If cancellation is observed, ``DeprovisionCancelledError`` propagates
          out of this activity uncaught rather than a soft-failure response —
          consuming that signal to gate the workflow is a follow-up change.
    """
    assert agent_id, "agent_id must be non-empty"
    _reject_stale_fencing_token(agent_id, fencing_token)
    from agent_team_studio.agent_provisioning_team.orchestrator import ProvisioningOrchestrator

    activity.heartbeat("deprovision")

    def _cancellation_checkpoint() -> bool:
        activity.heartbeat("deprovision")
        return activity.is_cancelled()

    response = ProvisioningOrchestrator().deprovision(
        agent_id,
        force=force,
        cancellation_checkpoint=_cancellation_checkpoint,
        fencing_token=fencing_token,
    )
    return response.model_dump()
