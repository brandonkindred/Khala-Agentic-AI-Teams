"""
Account provisioning phase: Create accounts in each tool.

This is phase 3 of the provisioning workflow.
"""

from typing import Callable, Dict, List, Optional

from ..models import (
    AccessTier,
    AccountProvisioningResult,
    GeneratedCredentials,
    ToolProvisionResult,
)
from ..shared.environment_store import EnvironmentStore
from ..shared.tool_manifest import ToolManifest
from ..tool_agents.base import ToolProvisionerInterface
from ..tool_agents.docker_provisioner import DockerProvisionerTool
from ..tool_agents.generic_provisioner import GenericProvisionerTool
from ..tool_agents.git_provisioner import GitProvisionerTool
from ..tool_agents.postgres_provisioner import PostgresProvisionerTool
from ..tool_agents.redis_provisioner import RedisProvisionerTool


def _build_provisioners() -> Dict[str, ToolProvisionerInterface]:
    """Build the default set of tool provisioners."""
    return {
        "docker_provisioner": DockerProvisionerTool(),
        "postgres_provisioner": PostgresProvisionerTool(),
        "git_provisioner": GitProvisionerTool(),
        "redis_provisioner": RedisProvisionerTool(),
        "generic_provisioner": GenericProvisionerTool(),
    }


def run_account_provisioning(
    agent_id: str,
    manifest: ToolManifest,
    credentials: Dict[str, GeneratedCredentials],
    access_tier: AccessTier,
    provisioners: Optional[Dict[str, ToolProvisionerInterface]] = None,
    environment_store: Optional[EnvironmentStore] = None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> AccountProvisioningResult:
    """
    Execute the account provisioning phase.
    
    Creates accounts/resources in each tool defined in the manifest.
    
    Args:
        agent_id: Unique identifier for the agent
        manifest: Loaded tool manifest
        credentials: Pre-generated credentials per tool
        access_tier: Requested access tier
        provisioners: Dict of provisioner instances (keyed by provisioner name)
        environment_store: Store for tracking tool provisioning
        progress_callback: Callback(done, total, tool_name) for progress updates
    
    Returns:
        AccountProvisioningResult with per-tool results
    """
    provs = provisioners or _build_provisioners()
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
            tool_results.append(ToolProvisionResult(
                tool_name=tool_name,
                success=False,
                error=f"Unknown provisioner: {provisioner_name}",
            ))
            continue
        
        tool_creds = credentials.get(tool_name)
        if tool_creds is None:
            tool_results.append(ToolProvisionResult(
                tool_name=tool_name,
                success=False,
                error=f"No credentials generated for {tool_name}",
            ))
            continue
        
        tool_access_level = tool.access_level
        tool_tier = _map_access_level_to_tier(tool_access_level, access_tier)
        
        try:
            result = provisioner.provision(
                agent_id=agent_id,
                config=tool.config,
                credentials=tool_creds,
                access_tier=tool_tier,
            )
            
            tool_results.append(result)
            
            if result.success:
                env_store.add_tool(agent_id, tool_name)
                completed += 1
        
        except Exception as e:
            tool_results.append(ToolProvisionResult(
                tool_name=tool_name,
                success=False,
                error=str(e),
            ))
    
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


def _map_access_level_to_tier(
    tool_access_level: str,
    default_tier: AccessTier,
) -> AccessTier:
    """Map a tool's access_level string to an AccessTier enum."""
    level_map = {
        "read_only": AccessTier.MINIMAL,
        "minimal": AccessTier.MINIMAL,
        "read_write": AccessTier.STANDARD,
        "standard": AccessTier.STANDARD,
        "contributor": AccessTier.STANDARD,
        "admin": AccessTier.ELEVATED,
        "elevated": AccessTier.ELEVATED,
        "full": AccessTier.FULL,
    }
    return level_map.get(tool_access_level.lower(), default_tier)


def deprovision_tools(
    agent_id: str,
    tool_names: Optional[List[str]] = None,
    provisioners: Optional[Dict[str, ToolProvisionerInterface]] = None,
) -> Dict[str, bool]:
    """
    Deprovision tools for an agent.
    
    Args:
        agent_id: Agent identifier
        tool_names: Specific tools to deprovision (None = all)
        provisioners: Provisioner instances
    
    Returns:
        Dict of tool_name -> success status
    """
    provs = provisioners or _build_provisioners()
    results: Dict[str, bool] = {}
    
    for name, provisioner in provs.items():
        if tool_names is None or name in tool_names:
            try:
                result = provisioner.deprovision(agent_id)
                results[name] = result.success
            except Exception:
                results[name] = False
    
    return results
