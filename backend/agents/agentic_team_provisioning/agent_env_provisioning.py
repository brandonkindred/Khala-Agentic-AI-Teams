"""
Bridge from agentic team process steps to the Agent Provisioning team.

Each (team, process, step, named agent) maps to a stable ``agent_id`` passed to
``agent_provisioning_team.ProvisioningOrchestrator.run_workflow`` so individual
step agents receive sandboxed environments (see manifests).

Disable with env ``AGENTIC_TEAM_AGENT_PROVISIONING_ENABLED=false``.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentic_team_provisioning.assistant.store import AgenticTeamStore
    from agentic_team_provisioning.models import ProcessDefinition

logger = logging.getLogger(__name__)

_ENABLED = os.getenv("AGENTIC_TEAM_AGENT_PROVISIONING_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
_MANIFEST = os.getenv("AGENTIC_TEAM_AGENT_PROVISIONING_MANIFEST", "minimal.yaml")


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
    def _run() -> None:
        try:
            from agent_provisioning_team.models import AccessTier
            from agent_provisioning_team.orchestrator import ProvisioningOrchestrator

            orch = ProvisioningOrchestrator()
            result = orch.run_workflow(
                agent_id=provisioning_agent_id,
                manifest_path=_MANIFEST,
                access_tier=AccessTier.STANDARD,
                job_updater=None,
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

    threading.Thread(target=_run, daemon=True, name=f"prov-{provisioning_agent_id[:24]}").start()


def is_agent_env_provisioning_enabled() -> bool:
    return _ENABLED
