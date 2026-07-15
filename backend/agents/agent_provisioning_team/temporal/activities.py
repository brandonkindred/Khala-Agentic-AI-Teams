"""Temporal activities for the Agent Provisioning team.

Per-phase activities used by ``AgentProvisioningWorkflow``. The per-tool
provision step is its own activity (``provision_tool_activity``) so a workflow
can fan out across tools in parallel with independent retry/heartbeat policies.
Each activity takes ``job_id`` as its first argument and writes phase/progress
updates back to ``job_store`` directly so ``GET /provision/status/{job_id}``
shows live progress without any signal plumbing.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

from temporalio import activity

from agent_provisioning_team.shared import job_store as _js

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

    ``ProvisioningOrchestrator()`` is intentionally constructed per call. Its
    ``__init__`` only wires local ``CredentialStore`` / ``EnvironmentStore``
    (mkdir + optional Fernet key file) and builds in-process provisioner
    objects — no network or DB pool warmup — so a process-global cache is
    unnecessary for activity use.
    """
    from agent_provisioning_team.orchestrator import ProvisioningOrchestrator
    from agent_provisioning_team.shared.tool_manifest import load_manifest

    orch = ProvisioningOrchestrator()
    manifest = load_manifest(manifest_path)
    return orch, manifest


def _best_effort_job_store(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Best-effort job_store call. Store hiccups must never fail the activity."""
    # Pass the real callable (not a name string) so renames stay searchable.
    # Incorrect args for a valid callable are still caught — progress writes
    # must not abort the activity and leave Temporal retries opaque.
    try:
        fn(*args, **kwargs)
    except Exception:
        logger.exception(
            "job_store.%s failed: args=%s kwargs=%s",
            getattr(fn, "__name__", repr(fn)),
            args,
            kwargs,
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
def list_manifest_tools_activity(manifest_path: str) -> List[str]:
    """Return ordered tool names from the agent manifest (workflow-safe I/O).

    Temporal workflows must not read files directly. This activity loads the
    manifest outside the deterministic workflow sandbox.

    Preconditions:
        * ``manifest_path`` is non-empty and readable by ``load_manifest``.
    Postconditions:
        * Returns tool names in manifest order.
    """
    assert manifest_path, "manifest_path must be non-empty"
    from agent_provisioning_team.shared.tool_manifest import load_manifest

    manifest = load_manifest(manifest_path)
    return [t.name for t in manifest.tools]


@activity.defn(name="agent_provisioning_setup")
def setup_activity(
    job_id: str,
    agent_id: str,
    manifest_path: str,
    prior_setup: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run (or restore) the Docker/environment setup phase.

    Preconditions:
        * ``job_id`` / ``agent_id`` / ``manifest_path`` are non-empty.
        * When ``prior_setup`` is set, it is a serialized setup phase snapshot
          acceptable to ``restore_setup``.
    Postconditions:
        * Returns ``{"success": True, "environment": <dump|None>}``.
        * Writes setup progress (or restore status) into ``job_store``.
        * Raises ``RuntimeError`` when a fresh setup fails.
    """
    from agent_provisioning_team.phases.setup import run_setup
    from agent_provisioning_team.shared.phase_state import restore_setup

    _best_effort_job_store(_js.mark_job_running, job_id)

    if prior_setup is not None:
        snap = restore_setup(prior_setup)
        _record_phase_restored(job_id, "setup", 15)
        return {
            "success": snap.success,
            "environment": snap.environment.model_dump() if snap.environment else None,
        }

    _best_effort_job_store(_js.update_job,
        job_id,
        current_phase="setup",
        progress=5,
        status_text="Creating Docker environment...",
    )
    orch, manifest = _load_ctx(manifest_path)
    activity.heartbeat("setup")
    result = run_setup(
        agent_id=agent_id,
        manifest=manifest,
        environment_store=orch.environment_store,
        docker_provisioner=orch.tool_agents.get("docker_provisioner"),
    )
    if not result.success:
        raise RuntimeError(f"setup failed: {result.error}")

    payload = {
        "success": True,
        "environment": result.environment.model_dump() if result.environment else None,
    }
    _best_effort_job_store(_js.add_completed_phase, job_id, "setup", payload)
    _best_effort_job_store(_js.update_job, job_id, progress=15, status_text="Setup complete")
    return payload


@activity.defn(name="agent_provisioning_credentials")
def credentials_activity(
    job_id: str,
    agent_id: str,
    manifest_path: str,
    prior_credentials: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Generate (or restore) per-tool credentials for the agent.

    Preconditions:
        * ``job_id`` / ``agent_id`` / ``manifest_path`` are non-empty.
        * When ``prior_credentials`` is set, it is a credential-phase snapshot
          acceptable to ``restore_credentials``.
    Postconditions:
        * Returns ``{"success": True, "credentials": {tool_name: dump, ...}}``.
        * Raises ``RuntimeError`` when credential generation fails.
    """
    from agent_provisioning_team.phases.credential_generation import run_credential_generation
    from agent_provisioning_team.shared.phase_state import restore_credentials

    if prior_credentials is not None:
        snap = restore_credentials(prior_credentials)
        _record_phase_restored(job_id, "credential_generation", 30)
        return {
            "success": snap.success,
            "credentials": {k: v.model_dump() for k, v in snap.credentials.items()},
        }

    _best_effort_job_store(_js.update_job,
        job_id,
        current_phase="credential_generation",
        progress=20,
        status_text="Generating credentials...",
    )
    orch, manifest = _load_ctx(manifest_path)
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
    _best_effort_job_store(_js.add_completed_phase, job_id, "credential_generation", payload)
    _best_effort_job_store(_js.update_job, job_id, progress=30, status_text="Credentials generated")
    return payload


@activity.defn(name="agent_provisioning_provision_tool")
def provision_tool_activity(
    job_id: str,
    agent_id: str,
    tool_name: str,
    manifest_path: str,
    credentials_dump: Dict[str, Any],
    tool_index: int = 0,
    tools_total: int = 0,
) -> Dict[str, Any]:
    """Provision a single tool — one activity per tool so fan-out is natural.

    Preconditions:
        * ``tool_name`` appears in the loaded manifest and maps to a known
          provisioner registry key.
        * ``credentials_dump`` is a serializable ``GeneratedCredentials`` dump
          for this tool.
    Postconditions:
        * Returns ``ToolProvisionResult.model_dump()`` from the provisioner
          with ``provisioner_key`` set to the manifest registry key (needed by
          ``compensate()`` — built-in provisioners leave it ``None``).
        * Does **not** write ``EnvironmentStore`` — parallel fan-out can run in
          different worker processes, so tool lists are recorded once after the
          gather in ``record_account_provisioning_activity``.
        * Raises ``RuntimeError`` when the tool or provisioner is unknown.
        * Updates ``job_store`` with the current tool / phase progress.
          Does not write ``tools_completed`` — parallel fan-out indexes are not
          completion counts and would race/regress under ``asyncio.gather``.
    Notes:
        * ``tool_index`` is reserved for logging / ordered progress aggregators;
          it is intentionally unused while fan-out progress stays tool-name based.
    """
    from agent_provisioning_team.models import GeneratedCredentials
    from agent_provisioning_team.shared.tool_agent_registry import build_default_tool_agents
    from agent_provisioning_team.shared.tool_manifest import load_manifest

    _ = tool_index
    _best_effort_job_store(
        _js.update_job,
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

    activity.heartbeat(f"provisioning {tool_name}")
    result = provisioner.provision(
        agent_id=agent_id,
        config=tool.config,
        credentials=creds,
    )
    # Mirror run_account_provisioning: stamp the registry key so compensate()
    # can look the provisioner back up (built-ins leave provisioner_key=None).
    # Also force tool_name to the manifest entry name — provisioners may return
    # their own stem (e.g. generic_provisioner → "generic") which would break
    # resume tool-set matching and EnvironmentStore recording.
    result.provisioner_key = tool.provisioner
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
    from agent_provisioning_team.models import ToolProvisionResult
    from agent_provisioning_team.phases.access_audit import run_access_audit
    from agent_provisioning_team.shared.phase_state import restore_access_audit
    from agent_provisioning_team.shared.tool_agent_registry import build_default_tool_agents
    from agent_provisioning_team.shared.tool_manifest import load_manifest

    if prior_audit is not None:
        result = restore_access_audit(prior_audit)
        _record_phase_restored(job_id, "access_audit", 75)
        return result.model_dump()

    _best_effort_job_store(_js.update_job,
        job_id,
        current_phase="access_audit",
        progress=70,
        status_text="Auditing access permissions...",
    )
    manifest = load_manifest(manifest_path)
    tool_results = [ToolProvisionResult.model_validate(t) for t in tool_results_dump]
    activity.heartbeat("access_audit")
    result = run_access_audit(
        agent_id=agent_id,
        tool_results=tool_results,
        manifest=manifest,
        provisioners=build_default_tool_agents(),
    )
    payload = result.model_dump()
    _best_effort_job_store(_js.add_completed_phase, job_id, "access_audit", payload)
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
    from agent_provisioning_team.models import GeneratedCredentials, ToolProvisionResult
    from agent_provisioning_team.phases.documentation import run_documentation
    from agent_provisioning_team.shared.phase_state import restore_documentation
    from agent_provisioning_team.shared.tool_manifest import load_manifest

    if prior_documentation is not None:
        snap = restore_documentation(prior_documentation)
        _record_phase_restored(job_id, "documentation", 90)
        return {
            "success": snap.success,
            "onboarding": snap.onboarding.model_dump() if snap.onboarding else None,
        }

    _best_effort_job_store(_js.update_job,
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
    _best_effort_job_store(_js.add_completed_phase, job_id, "documentation", payload)
    _best_effort_job_store(_js.update_job, job_id, progress=92, status_text="Documentation complete")
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
) -> Dict[str, Any]:
    """Finalize provisioning and mark the job completed or failed.

    Preconditions:
        * Upstream phase dumps (environment / credentials / tools / audit /
          onboarding) are None or valid model dumps for the deliver phase.
    Postconditions:
        * Returns ``{"success": <bool>, "error": <str|None>}``.
        * Marks the job completed (redacted result) or failed in ``job_store``.
    """
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

    _best_effort_job_store(_js.update_job,
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
        _best_effort_job_store(_js.mark_job_completed, job_id, result=redacted.model_dump())
    else:
        _best_effort_job_store(_js.mark_job_failed, job_id, error=final.error or "Provisioning failed")

    return {"success": final.success, "error": final.error}


@activity.defn(name="agent_provisioning_record_account_provisioning")
def record_account_provisioning_activity(
    job_id: str,
    tool_results_dump: List[Dict[str, Any]],
    agent_id: str = "",
) -> Dict[str, Any]:
    """Persist a successful account-provisioning checkpoint for ``/resume``.

    Preconditions:
        * ``job_id`` is non-empty.
        * ``tool_results_dump`` is the serializable per-tool result list.
        * ``agent_id`` is non-empty when environment tool recording is required.
    Postconditions:
        * ``completed_phases`` includes ``account_provisioning`` and
          ``phase_results`` carries ``{"success": True, "tool_results": ...}``.
        * Job progress reports ``tools_completed`` / ``tools_total`` from the
          finished result list so status polls no longer show ``0/N``.
        * When ``agent_id`` is set, successful tool names are written once via
          ``EnvironmentStore.add_tools`` (safe after parallel fan-out).
    """
    assert job_id, "job_id must be non-empty"
    results = list(tool_results_dump)
    tools_total = len(results)
    tools_completed = sum(1 for r in results if isinstance(r, dict) and r.get("success"))
    payload = {"success": True, "tool_results": results}
    _best_effort_job_store(_js.add_completed_phase, job_id, "account_provisioning", payload)
    _best_effort_job_store(
        _js.update_job,
        job_id,
        progress=60,
        status_text="Account provisioning complete",
        current_tool=None,
        tools_completed=tools_completed,
        tools_total=tools_total,
    )
    if agent_id:
        from agent_provisioning_team.shared.environment_store import EnvironmentStore

        names = [
            r.get("tool_name")
            for r in results
            if isinstance(r, dict) and r.get("success") and r.get("tool_name")
        ]
        EnvironmentStore().add_tools(agent_id, [n for n in names if isinstance(n, str)])
    return payload


@activity.defn(name="agent_provisioning_compensate")
def compensate_activity(
    agent_id: str,
    succeeded_tools: List[Dict[str, Any]],
) -> None:
    """Roll back a partially-provisioned agent (best effort).

    Preconditions:
        * ``agent_id`` identifies the agent whose tools should be rolled back.
        * ``succeeded_tools`` entries are dicts with ``tool_name`` and
          ``provisioner_key`` (registry key, e.g. ``"postgres_provisioner"``).
          The orchestrator looks provisioners up by that registry key.
    Postconditions:
        * Invokes ``ProvisioningOrchestrator.compensate`` once. Failures inside
          compensation are absorbed by the orchestrator (best effort).
    """
    from agent_provisioning_team.orchestrator import ProvisioningOrchestrator

    orch = ProvisioningOrchestrator()
    shims = [
        SimpleNamespace(
            tool_name=t.get("tool_name", ""),
            provisioner_key=t.get("provisioner_key"),
            success=True,
        )
        for t in succeeded_tools
    ]
    orch.compensate(agent_id, shims)


@activity.defn(name="agent_provisioning_mark_job_failed")
def mark_job_failed_activity(job_id: str, error: str) -> None:
    """Record a terminal failure for a provisioning job in ``job_store``.

    Used when the workflow aborts before ``deliver_activity`` (e.g. after tool
    compensation) so ``GET /provision/status/{job_id}`` does not stay ``running``.

    Preconditions:
        * ``job_id`` is non-empty.
        * ``error`` is a non-empty human-readable failure reason.
    Postconditions:
        * Best-effort ``mark_job_failed`` write via ``job_store`` (never raises
          from a store hiccup — uses ``_best_effort_job_store``).
    """
    assert job_id, "job_id must be non-empty"
    assert error, "error must be non-empty"
    _best_effort_job_store(_js.mark_job_failed, job_id, error=error)


# ---------------------------------------------------------------------------
# Deprovision — single activity wrapping the orchestrator's teardown
# ---------------------------------------------------------------------------


@activity.defn(name="agent_provisioning_deprovision")
def deprovision_activity(agent_id: str, force: bool = False) -> Dict[str, Any]:
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
    Postconditions:
        * Returns ``DeprovisionResponse.model_dump()`` — a JSON-serializable dict
          with ``agent_id``/``success``/``details``/``error``. Cleanup is
          best-effort: ``success`` is ``True`` when no tool errored or ``force``
          was set. The activity does not raise on partial-cleanup failure (the
          response carries the error), so Temporal does not retry a run that was
          intentionally reported as a soft failure.
    """
    from agent_provisioning_team.orchestrator import ProvisioningOrchestrator

    assert agent_id, "agent_id must be non-empty"
    activity.heartbeat("deprovision")
    response = ProvisioningOrchestrator().deprovision(agent_id, force=force)
    return response.model_dump()
