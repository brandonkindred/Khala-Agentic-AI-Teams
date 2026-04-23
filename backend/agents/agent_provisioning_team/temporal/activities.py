"""Temporal activities for the Agent Provisioning team.

Two activity surfaces are exposed:

* ``run_provisioning_activity`` — v1, single activity per workflow. Kept for
  backwards compatibility with ``AgentProvisioningWorkflow`` so in-flight
  runs can drain during a deploy.

* The ``*_activity_v2`` family — fine-grained, per-phase activities used by
  ``AgentProvisioningWorkflowV2``. The per-tool provision step is its own
  activity (``provision_tool_activity``) so a workflow can fan out across
  tools in parallel with independent retry/heartbeat policies. Each v2
  activity takes ``job_id`` as its first argument and writes phase/progress
  updates back to ``job_store`` directly so ``GET /provision/status/{job_id}``
  shows live progress without any signal plumbing.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

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


def _update_job_safe(job_id: str, **fields: Any) -> None:
    """Best-effort progress write. Must not fail the activity if job_store hiccups."""
    try:
        from agent_provisioning_team.shared.job_store import update_job

        update_job(job_id, **fields)
    except Exception:
        logger.exception("update_job failed for job=%s fields=%s", job_id, list(fields))


def _mark_running_safe(job_id: str) -> None:
    try:
        from agent_provisioning_team.shared.job_store import mark_job_running

        mark_job_running(job_id)
    except Exception:
        logger.exception("mark_job_running failed for job=%s", job_id)


def _add_completed_phase_safe(job_id: str, phase: str, result: Dict[str, Any]) -> None:
    try:
        from agent_provisioning_team.shared.job_store import add_completed_phase

        add_completed_phase(job_id, phase, result)
    except Exception:
        logger.exception("add_completed_phase failed for job=%s phase=%s", job_id, phase)


@activity.defn(name="agent_provisioning_setup")
def setup_activity_v2(
    job_id: str,
    agent_id: str,
    manifest_path: str,
    access_tier_str: str,
    prior_setup: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    from agent_provisioning_team.phases.setup import run_setup
    from agent_provisioning_team.shared.phase_state import restore_setup

    _mark_running_safe(job_id)

    if prior_setup is not None:
        snap = restore_setup(prior_setup)
        logger.info("Skipping setup for job=%s (restored from prior_results)", job_id)
        _update_job_safe(
            job_id,
            current_phase="setup",
            progress=15,
            status_text="Restored setup from previous run",
        )
        return {
            "success": snap.success,
            "environment": snap.environment.model_dump() if snap.environment else None,
        }

    _update_job_safe(
        job_id,
        current_phase="setup",
        progress=5,
        status_text="Creating Docker environment...",
    )
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

    payload = {
        "success": True,
        "environment": result.environment.model_dump() if result.environment else None,
    }
    _add_completed_phase_safe(job_id, "setup", payload)
    _update_job_safe(job_id, progress=15, status_text="Setup complete")
    return payload


@activity.defn(name="agent_provisioning_credentials")
def credentials_activity_v2(
    job_id: str,
    agent_id: str,
    manifest_path: str,
    prior_credentials: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    from agent_provisioning_team.orchestrator import ProvisioningOrchestrator
    from agent_provisioning_team.phases.credential_generation import run_credential_generation
    from agent_provisioning_team.shared.phase_state import restore_credentials
    from agent_provisioning_team.shared.tool_manifest import load_manifest

    if prior_credentials is not None:
        snap = restore_credentials(prior_credentials)
        logger.info("Skipping credential_generation for job=%s (restored)", job_id)
        _update_job_safe(
            job_id,
            current_phase="credential_generation",
            progress=30,
            status_text="Restored credentials from previous run",
        )
        return {
            "success": snap.success,
            "credentials": {k: v.model_dump() for k, v in snap.credentials.items()},
        }

    _update_job_safe(
        job_id,
        current_phase="credential_generation",
        progress=20,
        status_text="Generating credentials...",
    )
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

    payload = {
        "success": True,
        "credentials": {k: v.model_dump() for k, v in result.credentials.items()},
    }
    _add_completed_phase_safe(job_id, "credential_generation", payload)
    _update_job_safe(job_id, progress=30, status_text="Credentials generated")
    return payload


@activity.defn(name="agent_provisioning_provision_tool")
def provision_tool_activity(
    job_id: str,
    agent_id: str,
    tool_name: str,
    manifest_path: str,
    access_tier_str: str,
    credentials_dump: Dict[str, Any],
    tools_completed_so_far: int = 0,
    tools_total: int = 0,
) -> Dict[str, Any]:
    """Provision a single tool — one activity per tool so fan-out is natural."""
    from agent_provisioning_team.models import GeneratedCredentials
    from agent_provisioning_team.phases.account_provisioning import _map_access_level_to_tier
    from agent_provisioning_team.shared.tool_agent_registry import build_default_tool_agents
    from agent_provisioning_team.shared.tool_manifest import load_manifest

    _update_job_safe(
        job_id,
        current_phase="account_provisioning",
        current_tool=tool_name,
        tools_total=tools_total,
        status_text=f"Provisioning {tool_name}...",
    )

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


@activity.defn(name="agent_provisioning_audit")
def audit_activity_v2(
    job_id: str,
    agent_id: str,
    manifest_path: str,
    access_tier_str: str,
    tool_results_dump: List[Dict[str, Any]],
    prior_audit: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    from agent_provisioning_team.models import ToolProvisionResult
    from agent_provisioning_team.phases.access_audit import run_access_audit
    from agent_provisioning_team.shared.phase_state import restore_access_audit
    from agent_provisioning_team.shared.tool_agent_registry import build_default_tool_agents
    from agent_provisioning_team.shared.tool_manifest import load_manifest

    if prior_audit is not None:
        result = restore_access_audit(prior_audit)
        logger.info("Skipping access_audit for job=%s (restored)", job_id)
        _update_job_safe(
            job_id,
            current_phase="access_audit",
            progress=75,
            status_text="Restored audit from previous run",
        )
        return result.model_dump()

    _update_job_safe(
        job_id,
        current_phase="access_audit",
        progress=70,
        status_text="Auditing access permissions...",
    )
    manifest = load_manifest(manifest_path)
    access_tier = AccessTier(access_tier_str)
    tool_results = [ToolProvisionResult.model_validate(t) for t in tool_results_dump]
    activity.heartbeat("access_audit")
    result = run_access_audit(
        agent_id=agent_id,
        tool_results=tool_results,
        access_tier=access_tier,
        manifest=manifest,
        provisioners=build_default_tool_agents(),
    )
    payload = result.model_dump()
    _add_completed_phase_safe(job_id, "access_audit", payload)
    _update_job_safe(job_id, progress=80, status_text="Access audit complete")
    return payload


@activity.defn(name="agent_provisioning_documentation")
def documentation_activity_v2(
    job_id: str,
    agent_id: str,
    manifest_path: str,
    access_tier_str: str,
    credentials_dump: Dict[str, Dict[str, Any]],
    tool_results_dump: List[Dict[str, Any]],
    workspace_path: str,
    prior_documentation: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    from agent_provisioning_team.models import GeneratedCredentials, ToolProvisionResult
    from agent_provisioning_team.phases.documentation import run_documentation
    from agent_provisioning_team.shared.phase_state import restore_documentation
    from agent_provisioning_team.shared.tool_manifest import load_manifest

    if prior_documentation is not None:
        snap = restore_documentation(prior_documentation)
        logger.info("Skipping documentation for job=%s (restored)", job_id)
        _update_job_safe(
            job_id,
            current_phase="documentation",
            progress=90,
            status_text="Restored documentation from previous run",
        )
        return {
            "success": snap.success,
            "onboarding": snap.onboarding.model_dump() if snap.onboarding else None,
        }

    _update_job_safe(
        job_id,
        current_phase="documentation",
        progress=85,
        status_text="Generating onboarding documentation...",
    )
    manifest = load_manifest(manifest_path)
    access_tier = AccessTier(access_tier_str)
    credentials = {k: GeneratedCredentials.model_validate(v) for k, v in credentials_dump.items()}
    tool_results = [ToolProvisionResult.model_validate(t) for t in tool_results_dump]
    activity.heartbeat("documentation")
    result = run_documentation(
        agent_id=agent_id,
        manifest=manifest,
        credentials=credentials,
        tool_results=tool_results,
        access_tier=access_tier,
        workspace_path=workspace_path,
    )
    payload = {
        "success": result.success,
        "onboarding": result.onboarding.model_dump() if result.onboarding else None,
    }
    _add_completed_phase_safe(job_id, "documentation", payload)
    _update_job_safe(job_id, progress=92, status_text="Documentation complete")
    return payload


@activity.defn(name="agent_provisioning_deliver")
def deliver_activity_v2(
    job_id: str,
    agent_id: str,
    environment_dump: Optional[Dict[str, Any]],
    credentials_dump: Dict[str, Dict[str, Any]],
    tool_results_dump: List[Dict[str, Any]],
    audit_dump: Optional[Dict[str, Any]],
    onboarding_dump: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    from agent_provisioning_team.models import (
        AccessAuditResult,
        EnvironmentInfo,
        GeneratedCredentials,
        OnboardingPacket,
        ToolProvisionResult,
    )
    from agent_provisioning_team.orchestrator import ProvisioningOrchestrator
    from agent_provisioning_team.phases.deliver import (
        build_final_result,
        redact_credentials_for_response,
        run_deliver,
    )
    from agent_provisioning_team.shared.job_store import mark_job_completed, mark_job_failed

    _update_job_safe(
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

    orch = ProvisioningOrchestrator()
    activity.heartbeat("deliver")
    deliver_result = run_deliver(
        agent_id=agent_id,
        environment=environment,
        credentials=credentials,
        tool_results=tool_results,
        access_audit=audit,
        onboarding=onboarding,
        environment_store=orch.environment_store,
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
        try:
            mark_job_completed(job_id, result=redacted.model_dump())
        except Exception:
            logger.exception("mark_job_completed failed for job=%s", job_id)
    else:
        try:
            mark_job_failed(job_id, error=final.error or "Provisioning failed")
        except Exception:
            logger.exception("mark_job_failed failed for job=%s", job_id)

    return {"success": final.success, "error": final.error}


@activity.defn(name="agent_provisioning_compensate")
def compensate_activity_v2(
    agent_id: str,
    succeeded_tools: List[Dict[str, Any]],
) -> None:
    """Roll back a partially-provisioned agent (best effort).

    ``succeeded_tools`` entries are dicts with ``tool_name`` and
    ``provisioner_key`` (registry key, e.g. ``"postgres_provisioner"``).
    Post-#293 the orchestrator looks provisioners back up by the registry
    key, not by a class attribute derived from ``tool_name``.
    """
    from agent_provisioning_team.orchestrator import ProvisioningOrchestrator

    orch = ProvisioningOrchestrator()

    class _R:  # noqa: D401 — local shim
        def __init__(self, tool_name: str, provisioner_key: Optional[str]) -> None:
            self.tool_name = tool_name
            self.provisioner_key = provisioner_key
            self.success = True

    orch._compensate(
        agent_id,
        [_R(t.get("tool_name", ""), t.get("provisioner_key")) for t in succeeded_tools],
    )
