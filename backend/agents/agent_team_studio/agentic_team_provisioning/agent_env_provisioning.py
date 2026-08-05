"""
Bridge from agentic team process steps to the Agent Provisioning team.

Each (team, process, step, named agent) maps to a stable ``agent_id`` passed to
``agent_team_studio.agent_provisioning_team.ProvisioningOrchestrator.run_workflow`` so individual
step agents receive sandboxed environments (see manifests).

This path calls ``ProvisioningOrchestrator`` directly from a background thread,
bypassing Temporal entirely (unlike the HTTP ``/provision``/``/environments``
routes, which always go through ``AgentProvisioningWorkflow`` — see that
workflow's module docstring). It therefore takes the same
``agent_team_studio.agent_provisioning_team.shared.agent_lock.AgentLockStore`` ownership lock the
Temporal workflow takes, keyed by the same ``provisioning_agent_id``, so this
thread and a concurrent Temporal-driven run for the same agent id can never
interleave and corrupt each other's Docker/credential state.

Disable with env ``AGENTIC_TEAM_AGENT_PROVISIONING_ENABLED=false``.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_team_studio.agentic_team_provisioning.assistant.store import AgenticTeamStore
    from agent_team_studio.agentic_team_provisioning.models import ProcessDefinition

logger = logging.getLogger(__name__)

_ENABLED = os.getenv("AGENTIC_TEAM_AGENT_PROVISIONING_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
_MANIFEST = os.getenv("AGENTIC_TEAM_AGENT_PROVISIONING_MANIFEST", "minimal.yaml")

# Backoff for the blocking lock-acquire retry loop below. This background
# thread has no Temporal retry policy to lean on, so it mirrors
# AgentProvisioningWorkflow's own acquire semantics (bounded overall wait,
# capped exponential backoff) with a plain sleep loop instead.
_LOCK_RETRY_INITIAL_S = 5.0
_LOCK_RETRY_MAX_S = 60.0


def _slug(s: str, max_len: int = 40) -> str:
    t = re.sub(r"[^a-zA-Z0-9]+", "-", (s or "").strip().lower()).strip("-")
    return (t[:max_len] if t else "agent").rstrip("-")


def make_provisioning_agent_id(
    team_id: str,
    process_id: str,
    step_id: str,
    agent_name: str,
) -> str:
    """Stable id for agent_provisioning_team (alphanumeric + hyphens, bounded length)."""
    tid = re.sub(r"[^a-zA-Z0-9]", "", team_id)[:12]
    pid = re.sub(r"[^a-zA-Z0-9]", "", process_id)[:10]
    sid = _slug(step_id, 28)
    an = _slug(agent_name, 36)
    raw = f"at-{tid}-{pid}-{sid}-{an}"
    return raw[:120]


def schedule_provision_step_agents(
    team_id: str,
    process: ProcessDefinition,
    store: AgenticTeamStore,
) -> None:
    """For each step agent in ``process``, start Agent Provisioning workflow (background)."""
    if not _ENABLED:
        logger.debug("Agent env provisioning disabled (AGENTIC_TEAM_AGENT_PROVISIONING_ENABLED)")
        return

    for step in process.steps:
        for sa in step.agents:
            stable_key = f"{process.process_id}:{step.step_id}:{sa.agent_name}"
            prov_id = make_provisioning_agent_id(
                team_id, process.process_id, step.step_id, sa.agent_name
            )
            should_run = store.try_begin_agent_env_provision(
                team_id=team_id,
                stable_key=stable_key,
                process_id=process.process_id,
                step_id=step.step_id,
                agent_name=sa.agent_name,
                provisioning_agent_id=prov_id,
            )
            if not should_run:
                continue

            _spawn_provision_thread(
                team_id=team_id,
                stable_key=stable_key,
                provisioning_agent_id=prov_id,
                store=store,
            )


def _spawn_provision_thread(
    *,
    team_id: str,
    stable_key: str,
    provisioning_agent_id: str,
    store: AgenticTeamStore,
) -> None:
    threading.Thread(
        target=_provision_one,
        kwargs=dict(
            team_id=team_id,
            stable_key=stable_key,
            provisioning_agent_id=provisioning_agent_id,
            store=store,
        ),
        daemon=True,
        name=f"prov-{provisioning_agent_id[:24]}",
    ).start()


def _acquire_lock_blocking(lock_store, agent_id: str, owner: str, timeout_s: float) -> int:
    """Retry ``lock_store.acquire`` with capped backoff until ``timeout_s`` elapses.

    Preconditions:
        * ``timeout_s`` is positive.
    Postconditions:
        * Returns the fencing token ``owner`` now holds for ``agent_id`` (see
          ``AgentLockStore.acquire``) once acquired.
        * Raises the last ``AgentLockBusyError`` once ``timeout_s`` elapses
          without acquiring it.
    """
    from agent_team_studio.agent_provisioning_team.shared.agent_lock import AgentLockBusyError

    assert timeout_s > 0, "timeout_s must be positive"
    deadline = time.monotonic() + timeout_s
    delay = _LOCK_RETRY_INITIAL_S
    while True:
        try:
            return lock_store.acquire(agent_id, owner)
        except AgentLockBusyError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, _LOCK_RETRY_MAX_S)


def _provision_one(
    *,
    team_id: str,
    stable_key: str,
    provisioning_agent_id: str,
    store: AgenticTeamStore,
) -> None:
    """Acquire ``provisioning_agent_id``'s ownership lock, provision, then release.

    Runs on its own background thread (see ``_spawn_provision_thread``); the
    caller has already claimed ``stable_key`` via
    ``store.try_begin_agent_env_provision``, so this always ends by marking
    that row finished (success or failure) — never leaving it stuck ``running``.

    Preconditions:
        * ``store.try_begin_agent_env_provision`` returned ``True`` for
          ``(team_id, stable_key)`` — this call owns marking it finished.
    Postconditions:
        * ``store.mark_agent_env_provision_finished`` is called exactly once
          for ``(team_id, stable_key)``.
        * This function always attempts ``lock_store.release(...)`` for
          ``provisioning_agent_id`` under this run's owner token before
          returning; on the common path that succeeds and the record is no
          longer held by this run. A release failure (e.g. an unreadable
          lock record) is logged and swallowed rather than re-raised —
          matching this package's convention that cleanup/release paths
          never mask the original provisioning result — so in that rare
          case the record can remain held under this run's owner token
          until its lease naturally expires.
    """
    from agent_team_studio.agent_provisioning_team.orchestrator import ProvisioningOrchestrator
    from agent_team_studio.agent_provisioning_team.shared.agent_lock import (
        AgentLockBusyError,
        AgentLockStore,
    )
    from agent_team_studio.agent_provisioning_team.temporal.constants import (
        LOCK_ACQUIRE_TIMEOUT_S,
        LOCK_TTL_S,
    )

    owner = f"agentic-team-provision-{uuid.uuid4().hex}"
    lock_store = AgentLockStore(ttl_seconds=LOCK_TTL_S)
    try:
        fencing_token = _acquire_lock_blocking(
            lock_store, provisioning_agent_id, owner, LOCK_ACQUIRE_TIMEOUT_S
        )
    except AgentLockBusyError as e:
        logger.error(
            "Agent lock busy for team=%s key=%s agent_id=%s: %s",
            team_id,
            stable_key,
            provisioning_agent_id,
            e,
        )
        store.mark_agent_env_provision_finished(
            team_id, stable_key, success=False, error_message=str(e)
        )
        return

    try:
        orch = ProvisioningOrchestrator()
        result = orch.run_workflow(
            agent_id=provisioning_agent_id,
            manifest_path=_MANIFEST,
            job_updater=None,
            fencing_token=fencing_token,
        )
        if result.success:
            store.mark_agent_env_provision_finished(
                team_id, stable_key, success=True, error_message=None
            )
        else:
            store.mark_agent_env_provision_finished(
                team_id,
                stable_key,
                success=False,
                error_message=result.error or "Provisioning failed",
            )
    except Exception as e:
        logger.exception(
            "Agent provisioning failed for team=%s key=%s agent_id=%s",
            team_id,
            stable_key,
            provisioning_agent_id,
        )
        store.mark_agent_env_provision_finished(
            team_id, stable_key, success=False, error_message=str(e)
        )
    finally:
        try:
            lock_store.release(provisioning_agent_id, owner, fencing_token=fencing_token)
        except Exception:
            logger.exception(
                "Failed to release agent lock for agent_id=%s owner=%s",
                provisioning_agent_id,
                owner,
            )


def is_agent_env_provisioning_enabled() -> bool:
    return _ENABLED
