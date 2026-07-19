"""
Account provisioning phase: Create accounts in each tool.

This is phase 3 of the provisioning workflow.
"""

from typing import Callable, Dict, List, Optional

from ..models import (
    AccountProvisioningResult,
    DeprovisionCancelledError,
    GeneratedCredentials,
    ToolProvisionResult,
)
from ..shared.environment_store import EnvironmentStore
from ..shared.fencing import StaleFencingTokenError
from ..shared.tool_agent_registry import build_default_tool_agents
from ..shared.tool_manifest import ToolManifest
from ..tool_agents.base import ToolProvisionerInterface


def run_account_provisioning(
    agent_id: str,
    manifest: ToolManifest,
    credentials: Dict[str, GeneratedCredentials],
    provisioners: Optional[Dict[str, ToolProvisionerInterface]] = None,
    environment_store: Optional[EnvironmentStore] = None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    fencing_token: Optional[int] = None,
) -> AccountProvisioningResult:
    """
    Execute the account provisioning phase.

    Creates accounts/resources in each tool defined in the manifest.
    Every tool is provisioned with full access; the per-tier permission
    ladder was removed because Khala is a personal-use project (#456).

    Args:
        agent_id: Unique identifier for the agent
        manifest: Loaded tool manifest
        credentials: Pre-generated credentials per tool
        provisioners: Dict of provisioner instances (keyed by provisioner name)
        environment_store: Store for tracking tool provisioning
        progress_callback: Callback(done, total, tool_name) for progress updates
        fencing_token: Caller's fencing token (see ``shared.fencing``);
            ``None`` skips enforcement.

    Returns:
        AccountProvisioningResult with per-tool results
    """
    provs = provisioners or build_default_tool_agents()
    env_store = environment_store or EnvironmentStore()

    tools = manifest.tools
    total = len(tools)
    tool_results: List[ToolProvisionResult] = []
    completed = 0

    for idx, tool in enumerate(tools):
        tool_name = tool.name
        provisioner_name = tool.provisioner

        if progress_callback:
            progress_callback(idx, total, tool_name)

        provisioner = provs.get(provisioner_name)
        if provisioner is None:
            tool_results.append(
                ToolProvisionResult(
                    tool_name=tool_name,
                    success=False,
                    error=f"Unknown provisioner: {provisioner_name}",
                    provisioner_key=provisioner_name,
                )
            )
            continue

        tool_creds = credentials.get(tool_name)
        if tool_creds is None:
            tool_results.append(
                ToolProvisionResult(
                    tool_name=tool_name,
                    success=False,
                    error=f"No credentials generated for {tool_name}",
                    provisioner_key=provisioner_name,
                )
            )
            continue

        try:
            result = provisioner.provision(
                agent_id=agent_id,
                config=tool.config,
                credentials=tool_creds,
                fencing_token=fencing_token,
            )

            # Stamp the registry key so compensate() can look the provisioner
            # back up by key rather than by the fragile class attribute
            # `tool_name` (see #293). Also force tool_name to the manifest's
            # alias (mirrors provision_tool_activity, the Temporal path's
            # equivalent stamp): provisioners return their own class-level
            # tool_name (e.g. "postgresql"), which can differ from the
            # manifest name credentials were generated/stored under (e.g.
            # "pg") — compensate()'s credential purge looks the entry up by
            # this field, so leaving the provisioner's own name here would
            # silently miss the credential actually stored for this tool.
            result.provisioner_key = provisioner_name
            result.tool_name = tool_name
            tool_results.append(result)

            if result.success:
                env_store.add_tool(agent_id, tool_name, fencing_token=fencing_token)
                completed += 1

        except StaleFencingTokenError:
            # Propagate rather than convert to an ordinary failed-tool
            # result: a stale fencing token is a caller/ownership error, not
            # an infrastructure failure, and must not be silently swallowed.
            raise
        except Exception as e:
            tool_results.append(
                ToolProvisionResult(
                    tool_name=tool_name,
                    success=False,
                    error=str(e),
                    provisioner_key=provisioner_name,
                )
            )

    if progress_callback:
        progress_callback(total, total, "complete")

    all_success = all(r.success for r in tool_results)

    return AccountProvisioningResult(
        success=all_success,
        tool_results=tool_results,
        tools_completed=completed,
        tools_total=total,
        error=None if all_success else "One or more tools failed to provision",
    )


def deprovision_tools(
    agent_id: str,
    provisioner_keys: Optional[List[str]] = None,
    provisioners: Optional[Dict[str, ToolProvisionerInterface]] = None,
    checkpoint: Optional[Callable[[], bool]] = None,
    *,
    fencing_token: Optional[int] = None,
) -> Dict[str, bool]:
    """
    Deprovision an agent's tools by running each provisioner's teardown.

    This function operates at *provisioner* granularity, not tool granularity:
    a manifest can map many tools onto a single provisioner (many-to-one), and
    this function receives no manifest, so it has no way to attribute results to
    individual tool names. Both the filter and the returned keys are therefore
    provisioner registry keys (e.g. ``"docker_provisioner"``), never tool names.

    Args:
        agent_id: Agent identifier.
        provisioner_keys: Restrict teardown to these provisioner registry keys
            (the keys returned by ``build_default_tool_agents``). ``None`` means
            every provisioner in ``provisioners``.
        provisioners: Provisioner instances keyed by registry key. Defaults to
            ``build_default_tool_agents()``.
        checkpoint: Optional callable polled before each provisioner's teardown
            call. When it returns ``True``, teardown stops before that call —
            no further provisioners are torn down. ``None`` disables checking.
        fencing_token: Caller's fencing token (see ``shared.fencing``);
            ``None`` skips enforcement.

    Preconditions:
        * ``agent_id`` is non-empty.
        * ``provisioner_keys``, when given, holds provisioner registry keys (not
          tool names). Keys not present in ``provisioners`` are silently ignored.
        * ``provisioners`` maps each registry key to a ``ToolProvisionerInterface``.

    Postconditions:
        * Returns a ``dict`` keyed by **provisioner registry key** (NOT tool
          name), with a ``bool`` value per key equal to that provisioner's
          ``deprovision`` success; a provisioner that raises maps to ``False``.
        * Contains exactly one entry for every provisioner in ``provisioners``
          that passes the ``provisioner_keys`` filter and was torn down before
          ``checkpoint`` (if any) signalled cancellation, and no other keys.

    Invariants:
        * Best-effort teardown: never raises on a single provisioner failure —
          including a stale-fencing-token rejection from one provisioner — so
          one failing/rejected provisioner cannot block the rest. Each
          provisioner tracks its own fencing high-water mark independently
          (no cross-provisioner coordination), so a stale token for one
          resource does not imply the others have moved on too; aborting the
          whole loop on the first rejection would leave the others' real
          infrastructure never even attempted.

    Raises:
        DeprovisionCancelledError: ``checkpoint`` returned ``True`` before a
            provisioner's teardown call. Carries the results gathered so far.
    """
    assert agent_id, "agent_id must be non-empty"

    provs = provisioners or build_default_tool_agents()
    results: Dict[str, bool] = {}

    for key, provisioner in provs.items():
        if provisioner_keys is not None and key not in provisioner_keys:
            continue
        if checkpoint is not None and checkpoint():
            raise DeprovisionCancelledError(agent_id, results)
        try:
            result = provisioner.deprovision(agent_id, fencing_token=fencing_token)
            results[key] = result.success
        except Exception:
            results[key] = False

    return results
