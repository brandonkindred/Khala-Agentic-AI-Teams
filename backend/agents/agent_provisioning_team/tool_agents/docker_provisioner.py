"""
Docker container provisioner tool agent.

Handles container lifecycle: create, start, stop, remove.
"""

import subprocess
from typing import Any, Dict, List, Optional, Tuple

from ..models import (
    AccessTier,
    AccessVerification,
    DeprovisionResult,
    GeneratedCredentials,
    ToolProvisionResult,
)
from ..shared.access_policy import get_permissions
from .base import BaseToolProvisioner


class DockerProvisionerTool(BaseToolProvisioner):
    """Tool agent for Docker container provisioning."""

    tool_name = "docker"

    def __init__(self, workspace_base: str = "/workspace") -> None:
        self.workspace_base = workspace_base
        self._containers: Dict[str, Dict[str, Any]] = {}

    def provision(
        self,
        agent_id: str,
        config: Dict[str, Any],
        credentials: GeneratedCredentials,
        access_tier: AccessTier,
    ) -> ToolProvisionResult:
        """Create and start a Docker container for the agent."""
        try:
            container_name = f"agent-{agent_id}"
            base_image = config.get("base_image", "python:3.11-slim")
            workspace_path = config.get("workspace_path", f"{self.workspace_base}/{agent_id}")
            ssh_port = config.get("ssh_port", self._allocate_port(agent_id))
            
            build_cmd = [
                "docker", "run", "-d",
                "--name", container_name,
                "--hostname", container_name,
                "-v", f"{workspace_path}:/workspace",
                "-w", "/workspace",
                "--restart", "unless-stopped",
            ]
            
            env_vars = config.get("environment", {})
            for key, value in env_vars.items():
                build_cmd.extend(["-e", f"{key}={value}"])
            
            if config.get("expose_ssh", False):
                build_cmd.extend(["-p", f"{ssh_port}:22"])
            
            build_cmd.append(base_image)
            
            init_cmd = config.get("init_command", "tail -f /dev/null")
            build_cmd.extend(["sh", "-c", init_cmd])
            
            result = subprocess.run(
                build_cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
            
            if result.returncode != 0:
                return self._make_error_result(f"Docker run failed: {result.stderr}")
            
            container_id = result.stdout.strip()[:12]
            
            self._containers[agent_id] = {
                "container_id": container_id,
                "container_name": container_name,
                "ssh_port": ssh_port,
                "workspace_path": workspace_path,
                "status": "running",
            }
            
            permissions = get_permissions("docker", access_tier)
            
            credentials.extra["container_id"] = container_id
            credentials.extra["container_name"] = container_name
            credentials.extra["workspace_path"] = workspace_path
            
            return self._make_success_result(
                credentials=credentials,
                permissions=permissions,
                details={
                    "container_id": container_id,
                    "container_name": container_name,
                    "ssh_port": ssh_port,
                    "workspace_path": workspace_path,
                },
            )

        except subprocess.TimeoutExpired:
            return self._make_error_result("Docker container creation timed out")
        except Exception as e:
            return self._make_error_result(f"Docker provisioning error: {str(e)}")

    def verify_access(
        self,
        agent_id: str,
        expected_tier: AccessTier,
    ) -> AccessVerification:
        """Verify Docker container access."""
        container_info = self._containers.get(agent_id)
        
        if not container_info:
            return self._make_verification(
                passed=False,
                expected_tier=expected_tier,
                actual_permissions=[],
                errors=[f"No container found for agent {agent_id}"],
            )
        
        try:
            result = subprocess.run(
                ["docker", "inspect", container_info["container_name"]],
                capture_output=True,
                text=True,
                timeout=30,
            )
            
            if result.returncode != 0:
                return self._make_verification(
                    passed=False,
                    expected_tier=expected_tier,
                    actual_permissions=[],
                    errors=["Container not accessible"],
                )
            
            actual_permissions = get_permissions("docker", expected_tier)
            
            return self._make_verification(
                passed=True,
                expected_tier=expected_tier,
                actual_permissions=actual_permissions,
            )

        except Exception as e:
            return self._make_verification(
                passed=False,
                expected_tier=expected_tier,
                actual_permissions=[],
                errors=[str(e)],
            )

    def deprovision(self, agent_id: str) -> DeprovisionResult:
        """Stop and remove the Docker container."""
        container_info = self._containers.get(agent_id)
        
        if not container_info:
            return DeprovisionResult(
                tool_name=self.tool_name,
                success=True,
                details={"message": "No container to remove"},
            )
        
        try:
            container_name = container_info["container_name"]
            
            subprocess.run(
                ["docker", "stop", container_name],
                capture_output=True,
                timeout=60,
            )
            
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True,
                timeout=30,
            )
            
            del self._containers[agent_id]
            
            return DeprovisionResult(
                tool_name=self.tool_name,
                success=True,
                details={"container_removed": container_name},
            )

        except Exception as e:
            return DeprovisionResult(
                tool_name=self.tool_name,
                success=False,
                error=str(e),
            )

    def _allocate_port(self, agent_id: str) -> int:
        """Allocate an SSH port for the container."""
        base_port = 22000
        offset = abs(hash(agent_id)) % 1000
        return base_port + offset

    def get_container_info(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Get container information for an agent."""
        return self._containers.get(agent_id)

    def exec_in_container(
        self,
        agent_id: str,
        command: List[str],
        timeout: int = 60,
    ) -> Tuple[int, str, str]:
        """Execute a command inside the container.
        
        Returns:
            Tuple of (exit_code, stdout, stderr)
        """
        container_info = self._containers.get(agent_id)
        if not container_info:
            return 1, "", f"No container for agent {agent_id}"
        
        try:
            result = subprocess.run(
                ["docker", "exec", container_info["container_name"]] + command,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return 1, "", "Command timed out"
        except Exception as e:
            return 1, "", str(e)
