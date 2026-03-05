"""
Setup phase: Create Docker container for the agent.

This is phase 1 of the provisioning workflow.
"""

from typing import Any, Callable, Dict, Optional

from ..models import (
    AccessTier,
    EnvironmentInfo,
    GeneratedCredentials,
    SetupResult,
)
from ..shared.environment_store import EnvironmentStore
from ..shared.tool_manifest import ToolManifest
from ..tool_agents.docker_provisioner import DockerProvisionerTool


def run_setup(
    agent_id: str,
    manifest: ToolManifest,
    access_tier: AccessTier,
    environment_store: Optional[EnvironmentStore] = None,
    docker_provisioner: Optional[DockerProvisionerTool] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> SetupResult:
    """
    Execute the setup phase: create Docker container for the agent.
    
    Args:
        agent_id: Unique identifier for the agent
        manifest: Loaded tool manifest
        access_tier: Requested access tier
        environment_store: Store for tracking environments
        docker_provisioner: Docker provisioner instance
        progress_callback: Optional callback for progress updates
    
    Returns:
        SetupResult with environment info
    """
    env_store = environment_store or EnvironmentStore()
    docker = docker_provisioner or DockerProvisionerTool()
    
    if progress_callback:
        progress_callback("Checking for existing environment...")
    
    existing = env_store.get(agent_id)
    if existing and existing.status == "running":
        return SetupResult(
            success=True,
            environment=EnvironmentInfo(
                container_id=existing.container_id,
                container_name=existing.container_name,
                ssh_host=existing.ssh_host,
                ssh_port=existing.ssh_port,
                workspace_path=existing.workspace_path,
                status="running",
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
    
    credentials = GeneratedCredentials(
        tool_name="docker",
    )
    
    result = docker.provision(
        agent_id=agent_id,
        config=docker_config,
        credentials=credentials,
        access_tier=access_tier,
    )
    
    if not result.success:
        return SetupResult(
            success=False,
            error=result.error or "Docker container creation failed",
        )
    
    if progress_callback:
        progress_callback("Registering environment...")
    
    env_info = EnvironmentInfo(
        container_id=result.details.get("container_id", ""),
        container_name=result.details.get("container_name", f"agent-{agent_id}"),
        ssh_host="localhost",
        ssh_port=result.details.get("ssh_port", 22),
        workspace_path=result.details.get("workspace_path", f"/workspace/{agent_id}"),
        status="running",
    )
    
    from ..shared.environment_store import EnvironmentInfo as EnvInfoClass
    env_store.register(EnvInfoClass(
        agent_id=agent_id,
        container_id=env_info.container_id,
        container_name=env_info.container_name,
        ssh_host=env_info.ssh_host,
        ssh_port=env_info.ssh_port,
        workspace_path=env_info.workspace_path,
        status="running",
        tools_provisioned=[],
    ))
    
    return SetupResult(
        success=True,
        environment=env_info,
    )


def cleanup_setup(
    agent_id: str,
    environment_store: Optional[EnvironmentStore] = None,
    docker_provisioner: Optional[DockerProvisionerTool] = None,
) -> bool:
    """
    Clean up a failed setup by removing any partially created resources.
    
    Returns:
        True if cleanup successful
    """
    env_store = environment_store or EnvironmentStore()
    docker = docker_provisioner or DockerProvisionerTool()
    
    docker.deprovision(agent_id)
    env_store.remove(agent_id)
    
    return True
