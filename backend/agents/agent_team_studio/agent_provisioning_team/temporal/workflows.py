"""Temporal workflows for the Agent Provisioning team.

``AgentProvisioningWorkflow`` decomposes provisioning into per-phase activities
and fans out tool provisioning in parallel via ``asyncio.gather``.
``AgentDeprovisioningWorkflow`` tears down one agent as a single activity.

Phase *functions* live under ``phases/`` and are shared with
``ProvisioningOrchestrator.run_workflow`` (in-process tests / non-HTTP callers).
This Temporal workflow is the production sequencing source of truth for HTTP
provision/resume/restart; keep activity order and compensation aligned with
``orchestrator.run_workflow`` when changing either path.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

# Import activities/constants via the pass-through block so the temporalio
# workflow sandbox does not re-execute their module bodies during registration.
# Prefer ``temporal.constants`` over the package ``__init__`` for TASK_QUEUE so
# we never pull the heavy activities list into the workflow sandbox path.
with workflow.unsafe.imports_passed_through():
    from agent_team_studio.agent_provisioning_team.shared.fencing import StaleFencingTokenError
    from agent_team_studio.agent_provisioning_team.temporal import activities as _activities
    from agent_team_studio.agent_provisioning_team.temporal.constants import (
        DEFAULT_WORKSPACE_PATH,
        LOCK_ACQUIRE_TIMEOUT_S,
        TASK_QUEUE,
    )

PHASE_TIMEOUT = timedelta(minutes=20)
TOOL_ACTIVITY_TIMEOUT = timedelta(minutes=15)
TOOL_HEARTBEAT_TIMEOUT = timedelta(minutes=2)

# AgentDeprovisioningWorkflow's own detection deadline for a stuck
# deprovision_activity, strictly shorter than that activity's
# schedule_to_close_timeout (PHASE_TIMEOUT). Once schedule_to_close_timeout
# itself elapses, Temporal has already resolved the activity handle (as a
# timeout failure) from the workflow's point of view — cancelling a handle
# that's no longer pending is a no-op — so this margin exists purely to give
# the workflow a chance to request cancellation, and await its acknowledgement,
# *while the activity is still outstanding*.
DEPROVISION_CANCEL_GRACE = timedelta(minutes=2)
DEPROVISION_SOFT_TIMEOUT = PHASE_TIMEOUT - DEPROVISION_CANCEL_GRACE

# Detects a deprovision_activity worker that stops responding entirely (crash,
# thread-pool starvation, etc.) via Temporal's own heartbeat-timeout mechanism,
# instead of only discovering it once the far larger PHASE_TIMEOUT/
# DEPROVISION_SOFT_TIMEOUT budget is exhausted. Matches TOOL_HEARTBEAT_TIMEOUT's
# existing value/precedent (provision_tool_activity, below) for the identical
# shape of risk: deprovision_activity's cancellation checkpoint only heartbeats
# *between* per-provisioner teardown calls, so any single call slower than this
# timeout is misread as a stalled worker and retried (DEFAULT_RETRY_POLICY) —
# the same accepted tradeoff provision_tool_activity already makes.
DEPROVISION_HEARTBEAT_TIMEOUT = timedelta(minutes=2)

DEFAULT_RETRY_POLICY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=30),
    maximum_interval=timedelta(minutes=2),
    backoff_coefficient=2.0,
    # A StaleFencingTokenError (either shared/agent_lock.py's or
    # shared/fencing.py's — Temporal's retry policy matches by type NAME, so
    # both are covered by this one entry) means the caller's lease was
    # reclaimed while it was paused — retrying would just be rejected again.
    # Governs setup/credentials/record_account_provisioning/compensate/
    # deliver/deprovision/release_agent_lock; inert for the activities on
    # this shared policy that can never raise it (list_manifest_tools,
    # audit, documentation, mark_job_failed).
    non_retryable_error_types=["StaleFencingTokenError"],
)

# Bounds how long a workflow keeps retrying a busy per-agent_id lock
# (shared/agent_lock.py) before giving up. Unbounded attempts — the ceiling
# is schedule_to_close_timeout, not a retry count — so a busy lock is polled
# with backoff until it frees or this budget is exhausted.
LOCK_ACQUIRE_TIMEOUT = timedelta(seconds=LOCK_ACQUIRE_TIMEOUT_S)
LOCK_ACQUIRE_RETRY_POLICY = RetryPolicy(
    maximum_attempts=0,
    initial_interval=timedelta(seconds=5),
    maximum_interval=timedelta(minutes=1),
    backoff_coefficient=2.0,
)

TOOL_RETRY_POLICY = RetryPolicy(
    maximum_attempts=4,
    initial_interval=timedelta(seconds=15),
    maximum_interval=timedelta(minutes=2),
    backoff_coefficient=2.0,
    # ValueError: bad tool config, won't succeed on retry. StaleFencingTokenError:
    # the caller's lease was reclaimed while it was paused — retrying would
    # just be rejected again by the same check (see the DEFAULT_RETRY_POLICY
    # comment above re: matching both StaleFencingTokenError classes by name).
    non_retryable_error_types=["ValueError", "StaleFencingTokenError"],
)

# Replay-compatibility gates for the per-agent_id ownership lock
# (shared/agent_lock.py). A history recorded before this deploy has no
# acquire/renew/release commands; replaying it with code that unconditionally
# schedules those activities would report nondeterminism and strand the
# in-flight execution. ``workflow.patched`` returns False on such a replay,
# so ``_acquire_agent_lock``/``_release_agent_lock`` skip scheduling anything
# and the original (lock-free) sequence reproduces exactly; a new execution
# records the marker and always takes the locking path. Distinct ids per
# workflow type since each has its own independent history population.
# Mirrors ``code_review_agent.temporal.workflows._ARCHITECTURE_PASS_PATCH``'s
# identical rationale for an additive mid-sequence step.
# TODO: Remove these gates (and always lock unconditionally) once no
# pre-lock AgentProvisioningWorkflow/AgentDeprovisioningWorkflow histories
# remain open — confirm via ``find_open_pre_patch_executions``
# (shared/visibility_query.py) or the Temporal UI, per the drain-verification
# runbook in agent_provisioning_team/README.md ("Runbook: verifying the lock
# rollout has drained") — then deprecate each marker with
# ``workflow.deprecate_patch(...)`` for one release before deleting it.
#
# No corresponding gate is needed for the fencing-token argument added to
# every already-locked activity call below: Temporal's replay-determinism
# check matches command type/order/count against recorded history, not
# activity input payloads —
# for an activity already completed in history, replay reads the recorded
# result directly and never re-sends or re-validates its input. The two gates
# above exist specifically because pre-lock histories have *no*
# acquire/renew/release commands at all (a missing command — a count
# mismatch), not because an existing command's arguments changed. A workflow
# instance straddling this deploy runs with fencing_token=None (unenforced)
# from wherever it resumes until its next lock renewal — strictly better than
# today (zero enforcement anywhere), not a regression.
_PROVISIONING_LOCK_PATCH = "agent-provisioning-lock"
_DEPROVISIONING_LOCK_PATCH = "agent-deprovisioning-lock"

# A SEPARATE, independent gate for check_existing_environment_activity —
# deliberately NOT reusing _PROVISIONING_LOCK_PATCH. An in-flight history
# recorded after the lock existed but before this check was introduced
# already recorded "lock acquired -> setup" as its command sequence; since
# workflow.patched(_PROVISIONING_LOCK_PATCH) is already True for such a
# history, gating this newer activity behind that same marker would insert
# it into that history's replay and report nondeterminism. A fresh marker
# returns False for any history recorded before ITS OWN introduction —
# whether pre-lock or merely pre-this-check — reproducing each one's
# original sequence exactly.
_PRE_EXISTING_ENV_CHECK_PATCH = "agent-provisioning-pre-existing-check"

# Bounded so a cyclic/adversarial cause chain can never loop forever — mirrors
# shared.temporal.failure_translation.translate_workflow_failure's own bound.
_MAX_FENCING_CAUSE_DEPTH = 12


def _is_stale_fencing_token_failure(exc: BaseException) -> bool:
    """True iff ``exc``'s cause chain carries a StaleFencingTokenError marker.

    Temporal reconstructs an activity's raised exception as a generic
    ``ActivityError`` wrapping an ``ApplicationError`` tagged
    ``type=<original class name>`` — not an instance of the original class —
    so a plain ``isinstance`` check cannot detect this. A local, minimal
    walk rather than reusing
    ``shared.temporal.failure_translation.translate_workflow_failure``: that
    helper reconstructs and raises the *native* exception on a match (via
    ``native(message)``, a single positional arg), which is incompatible
    with ``StaleFencingTokenError``'s richer ``(agent_id, resource,
    provided_token, current_token)`` constructor — and unnecessary here,
    since this call site only needs a boolean, never the reconstructed
    instance.

    Preconditions:
        * ``exc`` is the exception caught from an ``await
          workflow.execute_activity(...)`` call (typically an
          ``ActivityError``).
    Postconditions:
        * Returns ``True`` iff some node in the chain (``exc`` itself, or
          reached via ``__cause__``/``__context__``, bounded and cycle-safe
          via an id-based visited set) has a ``.type`` attribute equal to
          ``StaleFencingTokenError.__name__``; ``False`` otherwise. Never
          raises.
    """
    seen: set[int] = set()
    node: BaseException | None = exc
    depth = 0
    while node is not None and id(node) not in seen and depth < _MAX_FENCING_CAUSE_DEPTH:
        seen.add(id(node))
        depth += 1
        if getattr(node, "type", None) == StaleFencingTokenError.__name__:
            return True
        node = node.__cause__ or node.__context__
    return False


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
            * ``succeeded`` entries also carry ``reused`` (from
              ``details.reused``) so a later compensation call can tell a
              tool this attempt idempotently reused apart from one it
              actually created.
        """
        tool_results_dump = list(ap.get("tool_results") or [])
        prior_names = {r.get("tool_name") for r in tool_results_dump if r.get("tool_name")}
        current_names = set(tool_names)
        succeeded: list[dict] = [
            {
                "tool_name": r.get("tool_name"),
                "provisioner_key": r.get("provisioner_key"),
                "reused": bool((r.get("details") or {}).get("reused", False)),
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
        fencing_token: int | None,
    ) -> tuple[list[dict], list[dict], list[str]]:
        """Fan out per-tool provision activities, or restore a prior phase dump.

        Preconditions:
            * ``tool_specs`` are ``{name, provisioner, config}`` dicts in order.
            * ``credentials_by_tool`` is keyed by tool name.
            * ``fencing_token`` is this run's current lease token on
              ``agent_id`` (``None`` on a pre-lock replay), threaded into
              every ``provision_tool_activity`` call.
        Postconditions:
            * Returns ``(tool_results_dump, succeeded, failures)``.
            * ``succeeded`` entries carry ``tool_name`` + ``provisioner_key`` +
              ``reused`` (from the provisioner's ``details.reused`` — set when
              it found and idempotently reused an existing account rather
              than creating one, so a later compensation call knows not to
              roll it back).
            * Every fanned-out activity call presents the SAME ``fencing_token``
              (captured once before the fan-out starts) — the stores this token
              is checked against accept any caller presenting a token ``>=``
              their current high-water mark, so N concurrent writers sharing
              one still-valid token are all accepted.
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
                    fencing_token,
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
                        "reused": bool((res.get("details") or {}).get("reused", False)),
                    }
                )
                tool_results_dump.append(res)
            else:
                err = (res.get("error") if isinstance(res, dict) else None) or "unknown"
                failures.append(f"{name}: {err}")
                tool_results_dump.append(
                    res
                    if isinstance(res, dict)
                    else {"tool_name": name, "success": False, "error": err}
                )
        return tool_results_dump, succeeded, failures

    async def _acquire_agent_lock(self, job_id: str, agent_id: str) -> int | None:
        """Claim exclusive ownership of ``agent_id`` for this workflow run.

        Preconditions:
            * ``job_id`` / ``agent_id`` are non-empty.
        Postconditions:
            * Returns ``None`` and schedules nothing when replaying a history
              recorded before the lock existed
              (``workflow.patched(_PROVISIONING_LOCK_PATCH)`` is ``False``),
              reproducing that history's original (lock-free) command sequence
              exactly — such a replay never actually held the lock, so callers
              must not treat a returned value (even ``None``) as proof of
              exclusive ownership; check
              ``workflow.patched(_PROVISIONING_LOCK_PATCH)`` for that.
              Otherwise blocks (with backoff, via
              ``LOCK_ACQUIRE_RETRY_POLICY``) until ``agent_id`` is free or
              ``LOCK_ACQUIRE_TIMEOUT`` is exhausted (in which case the
              activity's exception propagates), and returns the fencing
              token ``acquire_agent_lock_activity`` reports for this
              acquisition — the caller must use this (or the value of a
              later renewal, whichever is more recent) on every subsequent
              mutating activity call and on ``_release_agent_lock``.
        """
        if not workflow.patched(_PROVISIONING_LOCK_PATCH):
            return None
        return await workflow.execute_activity(
            _activities.acquire_agent_lock_activity,
            args=[job_id, agent_id],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=LOCK_ACQUIRE_TIMEOUT,
            retry_policy=LOCK_ACQUIRE_RETRY_POLICY,
        )

    async def _check_existing_environment(self, agent_id: str, job_id: str) -> bool:
        """Report whether ``agent_id`` already had a running environment
        before this run touched anything.

        Preconditions:
            * ``agent_id`` is non-empty; this workflow already holds
              ``agent_id``'s lock (called right after acquiring it, before
              setup runs, so nothing else can register an environment in the
              gap between this check and setup starting).
            * ``job_id`` is this workflow run's own job id — passed through so
              the activity can recognize a container THIS run's own earlier
              attempt labeled (e.g. a resumed job reusing ``job_id``) rather
              than one that predates this run. Adding this argument to an
              already-scheduled call does not change the command sequence a
              replay must match — no new ``workflow.patched(...)`` gate is
              needed for it (unlike whether the call happens at all, which
              ``_PRE_EXISTING_ENV_CHECK_PATCH`` below still governs).
        Postconditions:
            * Gated by its own ``_PRE_EXISTING_ENV_CHECK_PATCH`` marker — NOT
              ``_PROVISIONING_LOCK_PATCH`` (see that marker's own comment for
              why reusing the lock's marker would misfire this activity into
              an in-flight post-lock, pre-this-check history's replay).
              Returns ``True`` without scheduling anything when unpatched,
              reproducing that history's original command sequence; this is
              moot for compensation safety on a pre-lock replay specifically,
              since ``lock_acquired`` is already ``False`` there, which alone
              disables compensation regardless of this value.
            * Otherwise returns ``check_existing_environment_activity``'s
              result.
        """
        if not workflow.patched(_PRE_EXISTING_ENV_CHECK_PATCH):
            return True
        return await workflow.execute_activity(
            _activities.check_existing_environment_activity,
            args=[agent_id, job_id],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=PHASE_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )

    async def _renew_agent_lock(self, job_id: str, agent_id: str) -> int | None:
        """Refresh this workflow's lease on ``agent_id`` at an activity boundary.

        Preconditions:
            * ``job_id`` / ``agent_id`` are non-empty; this workflow already
              holds ``agent_id``'s lock (acquired via ``_acquire_agent_lock``
              earlier in this same run).
        Postconditions:
            * ``agent_id``'s lock record's lease is extended ``LOCK_TTL_S``
              seconds from now — ``acquire()`` renews rather than raising for
              the current owner. ``run()`` calls this between every single
              ``workflow.execute_activity`` call, not just at phase
              boundaries, so no gap between renewals ever exceeds one
              activity's own worst-case duration (the tool fan-out's
              ``TOOL_RETRY_POLICY`` ceiling is the largest — see
              ``LOCK_TTL_S``'s floor, sized to exceed exactly that). A
              legitimately slow (but still active) multi-hour run therefore
              never loses its own lock to ``AGENT_PROVISIONING_LOCK_TTL_S``
              expiry.
            * Returns the (possibly unchanged) fencing token — same semantics
              as ``_acquire_agent_lock``. A live, still-valid renewal returns
              the SAME token as before (the store does not mint a new one for
              a renewal in good standing); a renewal that happens to land
              just after expiry is, by ``AgentLockStore.acquire``'s own
              contract, indistinguishable from a genuine reclaim and DOES
              mint a new one — callers must always use this method's return
              value, not a value captured earlier in the run.
        """
        return await self._acquire_agent_lock(job_id, agent_id)

    async def _release_agent_lock(
        self, job_id: str, agent_id: str, fencing_token: int | None
    ) -> None:
        """Release this workflow's ownership of ``agent_id`` (best-effort).

        Preconditions:
            * ``job_id`` / ``agent_id`` are non-empty.
        Postconditions:
            * A replay of a pre-lock history schedules nothing (same
              ``workflow.patched(_PROVISIONING_LOCK_PATCH)`` gate as
              ``_acquire_agent_lock`` — nothing was acquired on that history,
              so there is nothing to release either).
            * Logs and swallows any exception rather than raising, so a
              release failure can never mask whatever exception (if any)
              is already propagating out of ``run()``.
        """
        if not workflow.patched(_PROVISIONING_LOCK_PATCH):
            return
        try:
            await workflow.execute_activity(
                _activities.release_agent_lock_activity,
                args=[job_id, agent_id, fencing_token],
                task_queue=TASK_QUEUE,
                schedule_to_close_timeout=PHASE_TIMEOUT,
                retry_policy=DEFAULT_RETRY_POLICY,
            )
        except Exception as release_exc:
            workflow.logger.error(
                "release_agent_lock_activity failed for job=%s agent=%s: %s",
                job_id,
                agent_id,
                release_exc,
            )

    async def _compensate_failed_tools(
        self,
        agent_id: str,
        succeeded: list[dict],
        job_id: str,
        tear_down_environment: bool = True,
        fencing_token: int | None = None,
    ) -> None:
        """Roll back tools that succeeded when the account-provisioning phase fails.

        Preconditions:
            * ``succeeded`` entries are ``{tool_name, provisioner_key, reused}``
              dicts (``reused`` marks an idempotently-reused, not freshly
              created, account).
            * ``job_id`` is non-empty (used to clear completed-phase checkpoints).
            * ``tear_down_environment`` is ``False`` when ``agent_id``'s Docker
              environment predates this run (``pre_existing_environment``) and
              must be preserved — ``succeeded`` still gets rolled back either
              way (except for ``reused`` entries, which are never this run's
              own creation regardless of ``tear_down_environment``).
            * ``fencing_token`` is this run's current lease token on
              ``agent_id`` (``None`` on a pre-lock replay or when compensating
              before a lock was ever acquired).
        Postconditions:
            * Invokes ``compensate_activity`` once for the partial success set.
            * When ``tear_down_environment`` is ``True`` (no environment
              predates this run — see ``pre_existing_environment``), every
              entry's ``reused`` is forced ``False`` before sending: a tool's
              own ``reused=True`` there cannot mean "predates this run" (there
              was nothing at ``agent_id`` for it to predate) — it can only be
              an artifact of Temporal retrying THIS run's own
              ``provision_tool_activity`` after its response was lost (the
              retry's idempotent create then reads back the first attempt's
              own successful write as "existing"). Left uncorrected, treating
              that as pre-existing would skip rolling it back and leak it.
              When ``tear_down_environment`` is ``False``, a genuine
              pre-existing environment means ``reused`` entries really can
              predate this run (e.g. a re-run against an already-delivered
              agent), so it is passed through unmodified there — the residual
              ambiguity between "predates this run" and "this run's own
              retry" in that case needs a stronger ownership signal than a
              same-attempt idempotency read can provide, tracked in #1489.
        """
        if tear_down_environment:
            succeeded = [{**s, "reused": False} for s in succeeded]
        await workflow.execute_activity(
            _activities.compensate_activity,
            args=[agent_id, succeeded, job_id, tear_down_environment, fencing_token],
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
        self,
        job_id: str,
        agent_id: str,
        tool_results_dump: list[dict],
        fencing_token: int | None,
    ) -> None:
        """Checkpoint successful tool results so later-phase failures can resume."""
        await workflow.execute_activity(
            _activities.record_account_provisioning_activity,
            args=[job_id, tool_results_dump, agent_id, fencing_token],
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
        fencing_token: int | None,
    ) -> dict[str, Any] | None:
        """Run or restore setup; return the environment dump (or ``None``)."""
        setup_prior = prior.get("setup") if "setup" in skip else None
        setup_result = await workflow.execute_activity(
            _activities.setup_activity,
            args=[job_id, agent_id, manifest_path, setup_prior, fencing_token],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=PHASE_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )
        return setup_result.get("environment") if setup_result else None

    async def _execute_credentials_phase(
        self,
        job_id: str,
        agent_id: str,
        manifest_path: str,
        skip: set[str],
        prior: dict[str, Any],
        tool_specs: list[dict[str, Any]] | None,
        fencing_token: int | None,
    ) -> dict[str, dict[str, Any]]:
        """Run or restore credential generation; return credentials keyed by tool."""
        creds_prior = (
            prior.get("credential_generation") if "credential_generation" in skip else None
        )
        creds_result = await workflow.execute_activity(
            _activities.credentials_activity,
            args=[job_id, agent_id, manifest_path, creds_prior, tool_specs, fencing_token],
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
        """Run or restore access audit.

        Read-only of the three fencing-relevant stores (no ``fencing_token``
        parameter) — ``run_access_audit``/``audit_single_tool`` only read
        already-provisioned state.
        """
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
        """Run or restore documentation generation.

        Read-only of the three fencing-relevant stores (no ``fencing_token``
        parameter) — ``run_documentation`` only reads already-provisioned
        state and writes onboarding docs to the workspace filesystem, not to
        ``EnvironmentStore``/``CredentialStore``/``ProvisionerStateStore``.
        """
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
        fencing_token: int | None,
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
                fencing_token,
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
              docs, deliver) while this run holds ``agent_id``'s lock:
              ``mark_job_failed_activity`` records terminal failure before the
              exception propagates, and ``compensate_activity`` runs (tool
              failures compensate the succeeded set; any other failure
              compensates an empty set) to roll back tool-level side effects
              this run created, unconditionally including a setup-phase
              failure. A setup failure ALSO triggers ``run_setup``'s own local
              best-effort rollback first (scoped to resources that attempt
              created) — the workflow-level ``compensate_activity`` is a
              second, independently retried backstop for when that local
              rollback itself fails (e.g. a transient ``docker rm`` error),
              not a replacement for it. Calling it is safe (including before
              setup ever runs, e.g. a manifest-load failure) because
              ``compensate_activity`` is fully idempotent — a no-op wherever
              nothing was created — and this run holds ``agent_id``'s
              exclusive lock throughout, so there is no other job's resources
              it could tear down instead.
              WHAT it tears down is separately gated by
              ``pre_existing_environment`` (checked right after acquiring the
              lock): holding the lock only excludes a *concurrent* workflow —
              it says nothing about whether THIS run is the one that created
              ``agent_id``'s Docker environment specifically (e.g. setup's
              already-running fast path reuses one and creates nothing). When
              ``pre_existing_environment`` is ``True``, ``compensate_activity``
              still rolls back any tool results this run itself produced
              (``succeeded_tools``/``succeeded``, e.g. a newly added tool on an
              already-delivered agent), but is told (via
              ``tear_down_environment=False``) to leave the Docker env,
              credential store, and environment record alone, since those
              predate this run.
              Compensation is skipped entirely — logging which reason applied
              — when this run never acquired the lock at all (exhausted
              retries against a live holder) or acquired it but later lost it
              (a renewal failure might indicate a replacement job now owns
              ``agent_id``); a pre-existing environment no longer disables
              compensation outright, only the environment-teardown portion of it.
            * After a successful tool fan-out (not a restored skip),
              ``account_provisioning`` is written to ``completed_phases`` /
              ``phase_results`` before later phases run.
        Invariants:
            * One Temporal workflow id per ``job_id`` (starter uses a stable
              prefix); resume/restart mint a new run with skip/prior args rather
              than relying on history drain.
            * At most one workflow (provision or deprovision) actively
              processes a given ``agent_id`` at a time: this run holds
              ``agent_id``'s ownership lock (``shared/agent_lock.py``) for its
              entire duration — acquired before setup, renewed between every
              scheduled activity so a legitimately slow run never loses its
              own lease to ``AGENT_PROVISIONING_LOCK_TTL_S`` expiry, released
              in a ``finally`` regardless of outcome — so every agent_id-keyed
              teardown call this run makes (``compensate_activity``,
              ``cleanup_setup``) is race-free against any other job. Gated by
              ``workflow.patched(_PROVISIONING_LOCK_PATCH)`` so a history
              recorded before the lock existed replays its original
              (lock-free) sequence.
            * The fencing token returned by the initial acquire (and updated
              by every subsequent renewal) is threaded into every mutating
              activity call, so a resumed-but-stale run's writes are rejected
              at the point of mutation even if this run's own renewal loop
              never observes the theft directly (see ``_is_stale_fencing_token_failure``
              and the ``lock_lost`` gating below).
        """
        assert job_id, "job_id must be non-empty"
        assert agent_id, "agent_id must be non-empty"
        assert manifest_path, "manifest_path must be non-empty"

        skip = set(skip_phases or [])
        prior = prior_results or {}
        terminal_failure_recorded = False
        tools_phase_compensated = False
        account_provisioning_done = False
        succeeded_tools: list[dict] = []
        lock_acquired = False
        lock_lost = False
        fencing_token: int | None = None
        # Conservative default: until _check_existing_environment proves
        # otherwise, assume agent_id might already have a live environment
        # this run didn't create, so a failure before that check completes
        # can't trigger destructive compensation.
        pre_existing_environment = True

        async def _renew_or_mark_lost() -> None:
            # A renewal failure (AgentLockBusyError from a genuine steal, or
            # any other error we can't disambiguate from one — see
            # _renew_agent_lock) means we can no longer prove we still own
            # agent_id's resources. Record that before re-raising so the
            # except block below never runs unfenced compensation against
            # resources a replacement job may now own.
            nonlocal lock_lost, fencing_token
            try:
                token = await self._renew_agent_lock(job_id, agent_id)
                if token is not None:
                    fencing_token = token
            except Exception:
                lock_lost = True
                raise

        try:
            # lock_acquired is only True when acquire actually held the lock —
            # a pre-lock replay's no-op return, or an acquire that raises
            # (exhausted retries against a live holder), both mean this run
            # never held agent_id's lock at all, and the except block below
            # must treat that exactly like losing it: compensating without
            # ever having held exclusive ownership could tear down whatever
            # job currently does hold the lock (or, for a pre-lock replay,
            # whatever job is running lock-free against the same agent_id).
            # Recomputed via workflow.patched(...) directly (rather than
            # trusting fencing_token's truthiness) because a stub/back-compat
            # activity result of None must not be mistaken for "never
            # acquired" — the fencing token and lock-acquisition are two
            # independent facts about the same call.
            fencing_token = await self._acquire_agent_lock(job_id, agent_id)
            lock_acquired = workflow.patched(_PROVISIONING_LOCK_PATCH)

            # Gated on the SAME marker _check_existing_environment checks
            # internally: a history recorded before this check existed
            # scheduled no activity and no renewal at this point (its
            # original sequence went straight from "lock acquired" to
            # "setup"), so both must be skipped together on replay, or the
            # renewal call alone would still insert a new, unrecorded command
            # into that history and report nondeterminism.
            if workflow.patched(_PRE_EXISTING_ENV_CHECK_PATCH):
                try:
                    # Best-effort: this check exists only to make compensation
                    # ownership-safe, not as a pipeline gate — an infra hiccup
                    # here must not abort provisioning outright. Falls back to
                    # the conservative pre_existing_environment=True default
                    # set above, which alone is enough to keep compensation
                    # disabled.
                    pre_existing_environment = await self._check_existing_environment(
                        agent_id, job_id
                    )
                except Exception:
                    pass
                await _renew_or_mark_lost()

            environment_dump = await self._execute_setup_phase(
                job_id, agent_id, manifest_path, skip, prior, fencing_token
            )
            if environment_dump is not None and environment_dump.get("reused") is False:
                # Setup's own confirmed outcome is stronger evidence than the
                # pre-check: a container run_setup just created cannot also
                # predate this run, so nothing at agent_id could have either
                # — this corrects a pre-check that was itself conservative or
                # inconclusive (e.g. an unreadable registry, or a stale
                # record whose backing container turned out to be gone, so
                # run_setup created a fresh one in its place). Only trusted
                # in this direction: reused=True is never used to flip
                # pre_existing_environment to True, since it can also be
                # this same run's own retry of setup_activity reading back
                # its own earlier (response-lost) success as "existing" —
                # the same ambiguity documented on _compensate_failed_tools
                # for tool-level reuse, tracked in #1489.
                pre_existing_environment = False
            await _renew_or_mark_lost()

            # Freeze manifest tools once for credential + provision phases so a
            # mid-run file edit cannot change the tool set under us.
            tool_specs = await workflow.execute_activity(
                _activities.list_manifest_tools_activity,
                args=[manifest_path],
                task_queue=TASK_QUEUE,
                schedule_to_close_timeout=PHASE_TIMEOUT,
                retry_policy=DEFAULT_RETRY_POLICY,
            )
            await _renew_or_mark_lost()

            credentials_by_tool = await self._execute_credentials_phase(
                job_id, agent_id, manifest_path, skip, prior, tool_specs, fencing_token
            )
            # Renew immediately before the fan-out phase (and again right
            # after it, below) so that phase's own worst-case duration — a
            # stuck tool retrying up to TOOL_RETRY_POLICY's ceiling, this
            # workflow's single largest un-renewed gap — is isolated rather
            # than compounded with any other activity's timeout.
            await _renew_or_mark_lost()

            tool_results_dump, succeeded, failures = await self._run_tool_provisioning_phase(
                job_id,
                agent_id,
                tool_specs,
                credentials_by_tool,
                skip,
                prior,
                fencing_token,
            )
            succeeded_tools = list(succeeded)
            credentials_by_tool = self._merge_enriched_credentials(
                credentials_by_tool, tool_results_dump
            )
            # Renew immediately after the fan-out — its own worst-case
            # duration (TOOL_RETRY_POLICY's ceiling) is this workflow's
            # single largest un-renewed gap; keep the checkpoint below out
            # of that same gap rather than compounding it.
            await _renew_or_mark_lost()

            if failures:
                await self._compensate_failed_tools(
                    agent_id,
                    succeeded,
                    job_id,
                    tear_down_environment=not pre_existing_environment,
                    fencing_token=fencing_token,
                )
                tools_phase_compensated = True
                err = f"Tool provisioning failed for agent {agent_id}: {'; '.join(failures)}"
                await self._mark_job_failed(job_id, err)
                terminal_failure_recorded = True
                raise RuntimeError(err)

            if "account_provisioning" not in skip:
                await self._record_account_provisioning(
                    job_id, agent_id, tool_results_dump, fencing_token
                )
            account_provisioning_done = True
            await _renew_or_mark_lost()

            audit_dump = await self._execute_audit_phase(
                job_id, agent_id, manifest_path, tool_results_dump, skip, prior
            )
            await _renew_or_mark_lost()

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
            await _renew_or_mark_lost()

            await self._execute_deliver_phase(
                job_id,
                agent_id,
                environment_dump,
                credentials_by_tool,
                tool_results_dump,
                audit_dump,
                onboarding_dump,
                fencing_token,
            )
        except Exception as exc:
            # Compensation runs unconditionally (compensate([]) when nothing
            # succeeded yet, including before setup ever ran) rather than being
            # gated on how far the pipeline got: `compensate_activity` is fully
            # idempotent — a no-op wherever nothing was created — and this run
            # holds agent_id's exclusive lock for its entire duration, so there
            # is no concurrent owner it could tear down instead. For a setup
            # failure specifically this is a deliberate SECOND cleanup attempt:
            # `run_setup` already ran its own local best-effort rollback (see
            # its postconditions) before this exception ever reached here; this
            # workflow-level call is retried independently by
            # `compensate_activity`'s own retry policy, so a transient failure
            # in the local rollback (e.g. a flaky `docker rm`) still gets torn
            # down instead of being left as an untracked orphan.
            # Nested try/except: compensation / terminal writes must not mask
            # the original failure if Temporal activity retries are exhausted.
            # lock_lost / stale_token_failure both gate this: either means we
            # can no longer prove we still own agent_id's resources (a
            # replacement job may already own them — via a renewal that
            # itself failed, or via a mutating activity's own preflight
            # fencing check rejecting a token that went stale between
            # renewals), so compensating here — keyed on agent_id alone, like
            # every teardown path — would recreate the exact cross-job
            # teardown race this lock exists to prevent.
            stale_token_failure = _is_stale_fencing_token_failure(exc)
            # lock_acquired / lock_lost gate whether to call compensate AT ALL:
            # if acquire itself failed, this run never held the lock at all; if
            # a renewal failed, it no longer can prove it still does (a
            # replacement job may already own agent_id's resources) —
            # compensating here, keyed on agent_id alone like every teardown
            # path, would recreate the exact cross-job teardown race this lock
            # exists to prevent.
            # BUT lock_acquired is also False — with no exception raised — for
            # a pre-lock-deploy replay (_acquire_agent_lock's own no-op-return
            # branch), which is NOT the same situation: such a history was
            # recorded before the lock existed at all, back when this except
            # block's only gate was progress flags (no lock concept to check),
            # so it may already contain a recorded compensate_activity command
            # that a lock_acquired-only guard would now omit — a command
            # dropped from the replayed sequence is genuine nondeterminism.
            # Re-checking the SAME patch marker _acquire_agent_lock already
            # consulted internally (safe/idempotent to check again) tells the
            # two apart: unpatched means this is that pre-lock replay, so fall
            # back to the pre-lock guard shape (progress flags only) to
            # reproduce its original sequence; patched means lock_acquired's
            # value is meaningful (True=held it, False=acquire itself failed).
            # pre_existing_environment gates WHAT compensate tears down, not
            # whether it runs: holding the lock only rules out a CONCURRENT
            # workflow — it says nothing about whether THIS run is the one
            # that created agent_id's environment specifically. succeeded_tools
            # (e.g. a newly added tool on an already-delivered agent) still
            # gets rolled back regardless of pre_existing_environment — except
            # for any entry marked reused, which by definition predates this
            # run's own tool fan-out and is excluded from rollback by
            # compensate() itself, independent of tear_down_environment. Only
            # the Docker env / credential store / environment record — which
            # predate this run when pre_existing_environment is True — are
            # excluded from teardown (see compensate_activity's
            # tear_down_environment parameter).
            lock_safe_to_compensate = not workflow.patched(_PROVISIONING_LOCK_PATCH) or (
                lock_acquired and not lock_lost
            )
            if (
                lock_safe_to_compensate
                and not stale_token_failure
                and not account_provisioning_done
                and not tools_phase_compensated
            ):
                try:
                    await self._compensate_failed_tools(
                        agent_id,
                        succeeded_tools,
                        job_id,
                        tear_down_environment=not pre_existing_environment,
                        fencing_token=fencing_token,
                    )
                except Exception as comp_exc:
                    workflow.logger.error(
                        "Compensation failed after provisioning error for job=%s: %s (original=%s)",
                        job_id,
                        comp_exc,
                        exc,
                    )
            elif not account_provisioning_done and not tools_phase_compensated:
                # lock_safe_to_compensate / stale_token_failure are the only
                # remaining reasons to land here now: pre_existing_environment
                # no longer disables compensation outright (it's threaded into
                # the call as tear_down_environment instead), so the first
                # branch's condition can only have failed on the lock (a
                # genuine post-lock acquire failure or a lost renewal — never
                # a pre-lock replay, which always takes the first branch) or
                # on a stale-fencing-token rejection surfaced by a mutating
                # activity even though this run's own renewals all succeeded.
                if stale_token_failure:
                    reason = "a stale-fencing-token rejection from a mutating activity"
                else:
                    reason = (
                        f"this run {'never acquired' if not lock_acquired else 'lost'} "
                        "the agent_id lock"
                    )
                workflow.logger.error(
                    "Skipped unfenced compensation for job=%s agent=%s: %s "
                    "(a different job may now own its resources): %s",
                    job_id,
                    agent_id,
                    reason,
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
        finally:
            await self._release_agent_lock(job_id, agent_id, fencing_token)


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
        * Holds ``agent_id``'s ownership lock (``shared/agent_lock.py``) for
          the run's entire duration — acquired before ``deprovision_activity``,
          released in a ``finally`` regardless of outcome — so this teardown
          can never interleave with a concurrent ``AgentProvisioningWorkflow``
          run (or another deprovision) for the same ``agent_id``. Gated by
          ``workflow.patched(_DEPROVISIONING_LOCK_PATCH)`` so a history
          recorded before the lock existed replays its original sequence.
        * No renewal loop (a single bounded activity between acquire and
          release), so — unlike ``AgentProvisioningWorkflow`` — there is no
          gap where the fencing token captured at acquire time could go
          stale mid-run; the one token captured up front is used for both
          the deprovision call and the release.
        * ``deprovision_activity`` is started (not executed) so a run past
          ``DEPROVISION_SOFT_TIMEOUT`` can request its cancellation and await
          that cancellation's acknowledgement — consuming the cooperative
          cancellation checkpoints ``deprovision_activity`` heartbeats between
          — before the ``finally`` below releases the lock, so release is
          never reached while that activity's worker thread may still be
          mutating this ``agent_id``'s resources. ``DEPROVISION_HEARTBEAT_TIMEOUT``
          bounds how long a genuinely unresponsive (crashed) worker can go
          undetected; a second, explicit ``DEPROVISION_CANCEL_GRACE`` wait
          bounds how long the workflow waits for that worker to acknowledge a
          requested cancellation before giving up and releasing the lock
          anyway — so no combination of a slow, erroring, or dead worker can
          leave the lock held indefinitely.
    """

    @workflow.run
    async def run(self, agent_id: str, force: bool = False) -> dict[str, Any]:
        """Execute deprovision and return the ``DeprovisionResponse`` dump.

        Preconditions:
            * ``agent_id`` is non-empty.
        Postconditions:
            * Returns the ``DeprovisionResponse.model_dump()`` produced by
              ``deprovision_activity``.
            * A replay of a history recorded before the lock existed
              (``workflow.patched(_DEPROVISIONING_LOCK_PATCH)`` is ``False``)
              schedules neither the acquire nor the release activity,
              reproducing that history's original (lock-free) command
              sequence exactly.
            * Raises ``TimeoutError`` if ``deprovision_activity`` is still
              running past ``DEPROVISION_SOFT_TIMEOUT`` — only after its
              cancellation has been requested and either acknowledged, or
              given up on past ``DEPROVISION_CANCEL_GRACE`` because the
              worker is presumed unresponsive (see ``_await_deprovision``).
        """
        assert agent_id, "agent_id must be non-empty"
        # Deprovision workflow ids are randomized per-call (repeated/concurrent
        # deprovisions of the same agent must never collide on Temporal's own
        # workflow-id uniqueness), so workflow_id doubles as this run's unique
        # lock-owner token — stable across replay (WorkflowInfo fields are
        # fixed from workflow start).
        owner = workflow.info().workflow_id
        locked = workflow.patched(_DEPROVISIONING_LOCK_PATCH)
        fencing_token: int | None = None
        try:
            if locked:
                # Inside the try (not before it): Temporal activities are
                # at-least-once — an acquire's side effect can persist
                # server-side even if its completion is never observed here
                # (e.g. exhausted LOCK_ACQUIRE_TIMEOUT after a lost ack). The
                # finally below must always get a chance to release, or that
                # successful acquire orphans the lock until LOCK_TTL_S.
                fencing_token = await workflow.execute_activity(
                    _activities.acquire_agent_lock_activity,
                    args=[owner, agent_id],
                    task_queue=TASK_QUEUE,
                    schedule_to_close_timeout=LOCK_ACQUIRE_TIMEOUT,
                    retry_policy=LOCK_ACQUIRE_RETRY_POLICY,
                )
            handle = workflow.start_activity(
                _activities.deprovision_activity,
                args=[agent_id, force, fencing_token],
                task_queue=TASK_QUEUE,
                schedule_to_close_timeout=PHASE_TIMEOUT,
                heartbeat_timeout=DEPROVISION_HEARTBEAT_TIMEOUT,
                retry_policy=DEFAULT_RETRY_POLICY,
            )
            return await self._await_deprovision(agent_id, handle)
        finally:
            if locked:
                try:
                    await workflow.execute_activity(
                        _activities.release_agent_lock_activity,
                        args=[owner, agent_id, fencing_token],
                        task_queue=TASK_QUEUE,
                        schedule_to_close_timeout=PHASE_TIMEOUT,
                        retry_policy=DEFAULT_RETRY_POLICY,
                    )
                except Exception as release_exc:
                    workflow.logger.error(
                        "release_agent_lock_activity failed for owner=%s agent=%s: %s",
                        owner,
                        agent_id,
                        release_exc,
                    )

    async def _await_deprovision(
        self, agent_id: str, handle: "workflow.ActivityHandle[dict[str, Any]]"
    ) -> dict[str, Any]:
        """Await ``deprovision_activity``, gating past-deadline release on a confirmed stop.

        Preconditions:
            * ``handle`` is the still-pending ``ActivityHandle`` this run just
              started for ``deprovision_activity``, scheduled with
              ``schedule_to_close_timeout=PHASE_TIMEOUT``.
        Postconditions:
            * If ``handle`` resolves (success or failure) within
              ``DEPROVISION_SOFT_TIMEOUT``, returns its result / propagates its
              exception unchanged — the caller's ``finally`` runs immediately
              after, exactly as before this change.
            * Past ``DEPROVISION_SOFT_TIMEOUT``, requests ``handle``'s
              cancellation and races it against a second, explicit
              ``DEPROVISION_CANCEL_GRACE`` timer rather than waiting on it
              unboundedly:
                - If ``handle`` resolves within that grace window — success,
                  ``DeprovisionCancelledError``, an SDK-level cancellation, or
                  any other activity error — the specific outcome is logged
                  distinctly (see ``_log_cancel_outcome``) and treated as
                  "confirmed stopped": safe for the caller's ``finally`` to
                  release the lock.
                - If ``handle`` is still unresolved once the grace window
                  itself elapses (e.g. a dead worker that never even
                  acknowledges cancellation), that is logged as an error and
                  the workflow gives up waiting and proceeds anyway, rather
                  than hang ``run()`` indefinitely — ``DEPROVISION_HEARTBEAT_TIMEOUT``
                  already gives Temporal's own liveness detection this whole
                  window to independently fail the activity.
            * Always raises ``TimeoutError`` once past ``DEPROVISION_SOFT_TIMEOUT``,
              with an outcome-specific suffix — so the caller's ``finally``
              always runs, and never while ``deprovision_activity`` might
              still be mutating this ``agent_id``'s resources.
        """
        timer = asyncio.ensure_future(workflow.sleep(DEPROVISION_SOFT_TIMEOUT))
        done, _pending = await asyncio.wait([handle, timer], return_when=asyncio.FIRST_COMPLETED)
        if handle in done:
            timer.cancel()
            return handle.result()

        handle.cancel()
        ack_timer = asyncio.ensure_future(workflow.sleep(DEPROVISION_CANCEL_GRACE))
        ack_done, _ack_pending = await asyncio.wait(
            [handle, ack_timer], return_when=asyncio.FIRST_COMPLETED
        )
        if handle in ack_done:
            ack_timer.cancel()
            outcome = self._log_cancel_outcome(agent_id, handle)
        else:
            outcome = (
                "cancellation not acknowledged within DEPROVISION_CANCEL_GRACE "
                f"({DEPROVISION_CANCEL_GRACE}); worker presumed unresponsive"
            )
            workflow.logger.error(
                "deprovision_activity for agent_id=%s did not acknowledge its "
                "requested cancellation within DEPROVISION_CANCEL_GRACE (%s); "
                "releasing agent_id=%s's lock anyway because the worker is "
                "presumed unable to make further progress",
                agent_id,
                DEPROVISION_CANCEL_GRACE,
                agent_id,
            )

        raise TimeoutError(
            f"deprovision_activity for agent_id={agent_id} exceeded "
            f"DEPROVISION_SOFT_TIMEOUT ({DEPROVISION_SOFT_TIMEOUT}); {outcome}"
        )

    def _log_cancel_outcome(
        self, agent_id: str, handle: "workflow.ActivityHandle[dict[str, Any]]"
    ) -> str:
        """Log how ``deprovision_activity`` resolved after acknowledging cancellation.

        Preconditions:
            * ``handle`` is already done (a member of the caller's ``ack_done`` set).
        Postconditions:
            * Logs exactly one of four distinct outcomes — never silently
              swallowed — and returns a short tag folded into the caller's
              ``TimeoutError`` message. All four are "confirmed stopped" and
              equally safe for the caller's ``finally`` to release the lock
              over; only the log level/message differs:
                - ``DeprovisionCancelledError`` (matched by exception type
                  name so this also matches a Temporal-wrapped
                  ``ApplicationError`` in production) — warning.
                - an SDK-level ``asyncio.CancelledError`` — warning.
                - any other activity error surfacing during/after
                  cancellation — error.
                - no exception at all (the activity completed successfully
                  despite the cancellation request, e.g. its last checkpoint
                  passed just before cancellation was observed) — warning.
        """
        try:
            handle.result()
        except asyncio.CancelledError as exc:
            workflow.logger.warning(
                "deprovision_activity for agent_id=%s acknowledged its requested "
                "cancellation (SDK-level) after DEPROVISION_SOFT_TIMEOUT: %s",
                agent_id,
                exc,
            )
            return "cancellation acknowledged"
        except BaseException as exc:  # noqa: BLE001 - classified below; every branch is still a confirmed stop
            exc_type = type(exc).__name__
            if (
                exc_type == "DeprovisionCancelledError"
                or getattr(exc, "type", None) == "DeprovisionCancelledError"
            ):
                workflow.logger.warning(
                    "deprovision_activity for agent_id=%s acknowledged its requested "
                    "cancellation (DeprovisionCancelledError, mid-teardown) after "
                    "DEPROVISION_SOFT_TIMEOUT: %s",
                    agent_id,
                    exc,
                )
                return "cancellation acknowledged (DeprovisionCancelledError)"
            workflow.logger.error(
                "deprovision_activity for agent_id=%s errored during/after its "
                "requested cancellation (treated as a confirmed stop; releasing "
                "agent_id=%s's lock): %s",
                agent_id,
                agent_id,
                exc,
            )
            return f"activity errored during/after cancellation ({exc_type}): {exc}"
        else:
            workflow.logger.warning(
                "deprovision_activity for agent_id=%s completed successfully "
                "despite a requested cancellation (its last checkpoint likely "
                "passed just before cancellation was observed); treating as a "
                "normal confirmed stop",
                agent_id,
            )
            return "activity completed successfully despite requested cancellation"
