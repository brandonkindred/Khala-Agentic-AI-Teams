"""
Active environment registry for tracking provisioned Docker containers.

Maintains mapping of agent IDs to their container information.
"""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_ENVIRONMENTS_DIR = Path(".agent_cache/provisioning_environments")

_lock = threading.Lock()


class EnvironmentInfo:
    """Information about a provisioned environment."""

    def __init__(
        self,
        agent_id: str,
        container_id: str,
        container_name: str,
        ssh_host: str = "localhost",
        ssh_port: int = 22,
        workspace_path: str = "/workspace",
        status: str = "running",
        tools_provisioned: Optional[List[str]] = None,
        created_at: Optional[str] = None,
    ) -> None:
        self.agent_id = agent_id
        self.container_id = container_id
        self.container_name = container_name
        self.ssh_host = ssh_host
        self.ssh_port = ssh_port
        self.workspace_path = workspace_path
        self.status = status
        self.tools_provisioned = tools_provisioned or []
        self.created_at = created_at or datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "container_id": self.container_id,
            "container_name": self.container_name,
            "ssh_host": self.ssh_host,
            "ssh_port": self.ssh_port,
            "workspace_path": self.workspace_path,
            "status": self.status,
            "tools_provisioned": self.tools_provisioned,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EnvironmentInfo":
        return cls(
            agent_id=data["agent_id"],
            container_id=data["container_id"],
            container_name=data["container_name"],
            ssh_host=data.get("ssh_host", "localhost"),
            ssh_port=data.get("ssh_port", 22),
            workspace_path=data.get("workspace_path", "/workspace"),
            status=data.get("status", "running"),
            tools_provisioned=data.get("tools_provisioned", []),
            created_at=data.get("created_at"),
        )


class EnvironmentStore:
    """Store for tracking active agent environments."""

    def __init__(self, storage_dir: Optional[Path] = None) -> None:
        self.storage_dir = storage_dir or DEFAULT_ENVIRONMENTS_DIR
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def _env_file(self, agent_id: str) -> Path:
        """Get the environment file path for an agent."""
        return self.storage_dir / f"{agent_id}.json"

    def register(self, env_info: EnvironmentInfo) -> None:
        """Register a new environment."""
        with _lock:
            path = self._env_file(env_info.agent_id)
            path.write_text(
                json.dumps(env_info.to_dict(), indent=2),
                encoding="utf-8",
            )

    def get(self, agent_id: str) -> Optional[EnvironmentInfo]:
        """Get environment info for an agent."""
        with _lock:
            path = self._env_file(agent_id)
            if not path.exists():
                return None
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return EnvironmentInfo.from_dict(data)
            except (json.JSONDecodeError, KeyError):
                return None

    def update_status(self, agent_id: str, status: str) -> bool:
        """Update the status of an environment."""
        with _lock:
            path = self._env_file(agent_id)
            if not path.exists():
                return False
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                data["status"] = status
                data["updated_at"] = datetime.now(timezone.utc).isoformat()
                path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                return True
            except (json.JSONDecodeError, IOError):
                return False

    def add_tool(self, agent_id: str, tool_name: str) -> bool:
        """Add a tool to the environment's provisioned tools list."""
        with _lock:
            path = self._env_file(agent_id)
            if not path.exists():
                return False
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                tools = data.get("tools_provisioned", [])
                if tool_name not in tools:
                    tools.append(tool_name)
                data["tools_provisioned"] = tools
                path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                return True
            except (json.JSONDecodeError, IOError):
                return False

    def remove(self, agent_id: str) -> bool:
        """Remove an environment from the registry."""
        with _lock:
            path = self._env_file(agent_id)
            if path.exists():
                path.unlink()
                return True
            return False

    def list_all(self, status: Optional[str] = None) -> List[EnvironmentInfo]:
        """List all registered environments, optionally filtered by status."""
        environments: List[EnvironmentInfo] = []

        with _lock:
            for env_file in self.storage_dir.glob("*.json"):
                try:
                    data = json.loads(env_file.read_text(encoding="utf-8"))
                    env = EnvironmentInfo.from_dict(data)
                    if status is None or env.status == status:
                        environments.append(env)
                except (json.JSONDecodeError, KeyError):
                    continue

        environments.sort(key=lambda e: e.created_at or "", reverse=True)
        return environments

    def exists(self, agent_id: str) -> bool:
        """Check if an environment exists for an agent."""
        return self._env_file(agent_id).exists()
