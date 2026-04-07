"""Temporal activities for the Agent Provisioning team.

Two activity surfaces are exposed:

* ``run_provisioning_activity`` — v1, single activity per workflow. Kept for
  backwards compatibility with ``AgentProvisioningWorkflow``.

* The ``*_activity_v2`` family — fine-grained, per-phase activities used by
  ``AgentProvisioningWorkflowV2``. The per-tool provision step is its own
  activity (``provision_tool_activity``) so a workflow can fan out across
  tools in parallel with independent retry/heartbeat policies.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from temporalio import activity

from agent_provisioning_team.models import AccessTier

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# v1 — single-shot activity (back-compat)
# ---------------------------------------------------------------------------


@activity.defn(name="run_agent_provisioning")
def run_provisioning_activity(
    job_id: str,
    agent_id: str,
    manifest_path: str,
    access_tier_str: str,
) -> None:
    """Run the provisioning workflow. Converts access_tier_str to AccessTier."""
    try:
        from agent_provisioning_team.api.main import _run_provisioning_background

        access_tier = AccessTier(access_tier_str)
        _run_provisioning_background(job_id, agent_id, manifest_path, access_tier)
    except Exception:
        logger.exception("Agent Provisioning activity failed for job %s", job_id)
        raise


# ---------------------------------------------------------------------------
# v2 — per-phase, fan-out friendly activities
# ---------------------------------------------------------------------------


def _load_ctx(manifest_path: str, access_tier_str: str):
    """Lazy import to keep the worker's import graph minimal."""
    from agent_provisioning_team.orchestrator import ProvisioningOrchestrator
    from agent_provisioning_team.shared.tool_manifest import load_manifest

    orch = ProvisioningOrchestrator()
    manifest = load_manifest(manifest_path)
    access_tier = AccessTier(access_tier_str)
    return orch, manifest, access_tier


@activity.defn(name="agent_provisioning_setup")
def setup_activity_v2(
    agent_id: str,
    manifest_path: str,
    access_tier_str: str,
) -> Dict[str, Any]:
    from agent_provisioning_team.phases.setup import run_setup

    orch, manifest, access_tier = _load_ctx(manifest_path, access_tier_str)
    activity.heartbeat("setup")
    result = run_setup(
        agent_id=agent_id,
        manifest=manifest,
        access_tier=access_tier,
        environment_store=orch.environment_store,
        docker_provisioner=orch.tool_agents.get("docker_provisioner"),
    )
    if not result.success:
        raise RuntimeError(f"setup failed: {result.error}")
    # Return a plain dict so it's JSON-serializable across the activity boundary.
    return {
        "success": True,
        "environment": result.environment.model_dump() if result.environment else None,
    }


@activity.defn(name="agent_provisioning_credentials")
def credentials_activity_v2(
    agent_id: str,
    manifest_path: str,
) -> Dict[str, Any]:
    from agent_provisioning_team.orchestrator import ProvisioningOrchestrator
    from agent_provisioning_team.phases.credential_generation import run_credential_generation
    from agent_provisioning_team.shared.tool_manifest import load_manifest

    orch = ProvisioningOrchestrator()
    manifest = load_manifest(manifest_path)
    activity.heartbeat("credentials")
    result = run_credential_generation(
        agent_id=agent_id,
        manifest=manifest,
        credential_store=orch.credential_store,
    )
    if not result.success:
        raise RuntimeError(f"credential generation failed: {result.error}")
    return {
        "success": True,
        "credentials": {k: v.model_dump() for k, v in result.credentials.items()},
    }


@activity.defn(name="agent_provisioning_provision_tool")
def provision_tool_activity(
    agent_id: str,
    tool_name: str,
    manifest_path: str,
    access_tier_str: str,
    credentials_dump: Dict[str, Any],
) -> Dict[str, Any]:
    """Provision a single tool — one activity per tool so fan-out is natural."""
    from agent_provisioning_team.models import GeneratedCredentials
    from agent_provisioning_team.phases.account_provisioning import _map_access_level_to_tier
    from agent_provisioning_team.shared.tool_agent_registry import build_default_tool_agents
    from agent_provisioning_team.shared.tool_manifest import load_manifest

    manifest = load_manifest(manifest_path)
    tool = manifest.get_tool(tool_name)
    if tool is None:
        raise RuntimeError(f"tool {tool_name} not in manifest")

    provisioners = build_default_tool_agents()
    provisioner = provisioners.get(tool.provisioner)
    if provisioner is None:
        raise RuntimeError(f"unknown provisioner {tool.provisioner}")

    creds = GeneratedCredentials.model_validate(credentials_dump)
    tier = _map_access_level_to_tier(tool.access_level, AccessTier(access_tier_str))

    activity.heartbeat(f"provisioning {tool_name}")
    result = provisioner.provision(
        agent_id=agent_id,
        config=tool.config,
        credentials=creds,
        access_tier=tier,
    )
    return result.model_dump()


@activity.defn(name="agent_provisioning_compensate")
def compensate_activity_v2(
    agent_id: str,
    succeeded_tools: List[str],
) -> None:
    """Roll back a partially-provisioned agent (best effort)."""
    from agent_provisioning_team.orchestrator import ProvisioningOrchestrator

    orch = ProvisioningOrchestrator()
    # Reuse the orchestrator's compensation path by synthesizing minimal
    # tool_results with success=True for the ones that completed.
    class _R:  # noqa: D401 — local shim
        def __init__(self, name: str) -> None:
            self.tool_name = name
            self.success = True

    orch._compensate(agent_id, [_R(t) for t in succeeded_tools])
