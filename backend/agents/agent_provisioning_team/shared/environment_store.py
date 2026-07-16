"""
Active environment registry for tracking provisioned Docker containers.

Maintains mapping of agent IDs to their container information.
"""

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def default_environments_dir() -> Path:
    """Resolve the durable on-disk environment registry directory.

    Preconditions:
        * None.
    Postconditions:
        * Returns ``${AGENT_CACHE:-.agent_cache}/agent_provisioning/environments``
          as a ``Path`` (directory need not exist yet).
    """
    root = Path(os.environ.get("AGENT_CACHE", ".agent_cache"))
    return root / "agent_provisioning" / "environments"


DEFAULT_ENVIRONMENTS_DIR = default_environments_dir()


def legacy_environments_dirs() -> List[Path]:
    """Return pre-cutover environment directories for read/migrate fallback.

    Preconditions:
        * None.
    Postconditions:
        * Includes the historical default ``.agent_cache/provisioning_environments``
          and the same relative layout under ``AGENT_CACHE`` when set.
    """
    root = Path(os.environ.get("AGENT_CACHE", ".agent_cache"))
    return [
        Path(".agent_cache") / "provisioning_environments",
        root / "provisioning_environments",
    ]

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
        self.storage_dir = Path(storage_dir) if storage_dir is not None else default_environments_dir()
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def _env_file(self, agent_id: str) -> Path:
        """Get the environment file path for an agent in the primary store."""
        return self.storage_dir / f"{agent_id}.json"

    def _env_file_candidates(self, agent_id: str) -> List[Path]:
        """Primary path first, then legacy locations from before the AGENT_CACHE move."""
        return [self._env_file(agent_id)] + [
            legacy / f"{agent_id}.json" for legacy in legacy_environments_dirs()
        ]

    def _read_env_data(self, agent_id: str) -> tuple[Optional[Dict[str, Any]], Optional[Path]]:
        """Load environment JSON from the primary or a legacy path.

        Preconditions:
            * ``agent_id`` is non-empty.
        Postconditions:
            * Returns ``(data, path)`` when a candidate file parses as a dict with
              the required ``agent_id`` / ``container_id`` / ``container_name`` keys.
            * Malformed JSON or incomplete records are skipped (treated as absent).
        """
        required = ("agent_id", "container_id", "container_name")
        for path in self._env_file_candidates(agent_id):
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(data, dict) or any(k not in data for k in required):
                continue
            return data, path
        return None, None

    def _write_env_data(self, agent_id: str, data: Dict[str, Any], source: Optional[Path] = None) -> None:
        """Persist environment JSON to the primary store, dropping a legacy copy."""
        primary = self._env_file(agent_id)
        primary.parent.mkdir(parents=True, exist_ok=True)
        primary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        if source is not None and source != primary and source.exists():
            source.unlink()

    def register(self, env_info: EnvironmentInfo) -> None:
        """Register a new environment."""
        with _lock:
            self._write_env_data(env_info.agent_id, env_info.to_dict())

    def get(self, agent_id: str) -> Optional[EnvironmentInfo]:
        """Get environment info for an agent."""
        with _lock:
            data, _src = self._read_env_data(agent_id)
            if data is None:
                return None
            try:
                return EnvironmentInfo.from_dict(data)
            except (KeyError, TypeError, ValueError):
                # Malformed / partial legacy records are treated as absent.
                return None

    def update_status(self, agent_id: str, status: str) -> bool:
        """Update the status of an environment."""
        with _lock:
            data, src = self._read_env_data(agent_id)
            if data is None:
                return False
            data["status"] = status
            data["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._write_env_data(agent_id, data, source=src)
            return True

    def add_tool(self, agent_id: str, tool_name: str) -> bool:
        """Add a tool to the environment's provisioned tools list."""
        return self.add_tools(agent_id, [tool_name])

    def add_tools(self, agent_id: str, tool_names: List[str]) -> bool:
        """Add zero or more tools in one read/modify/write under the store lock.

        Preconditions:
            * ``agent_id`` is non-empty.
            * ``tool_names`` may be empty (no-op success when the env exists).
        Postconditions:
            * When the env file exists, every non-empty unique name in
              ``tool_names`` is present in ``tools_provisioned`` (order of first
              appearance preserved for new names).
            * Returns ``False`` when the env is missing or the file is corrupt.
        """
        with _lock:
            data, src = self._read_env_data(agent_id)
            if data is None:
                return False
            tools = list(data.get("tools_provisioned", []))
            for tool_name in tool_names:
                if tool_name and tool_name not in tools:
                    tools.append(tool_name)
            data["tools_provisioned"] = tools
            self._write_env_data(agent_id, data, source=src)
            return True

    def remove(self, agent_id: str) -> bool:
        """Remove an environment from the registry."""
        with _lock:
            removed = False
            for path in self._env_file_candidates(agent_id):
                if path.exists():
                    path.unlink()
                    removed = True
            return removed

    def list_all(self, status: Optional[str] = None) -> List[EnvironmentInfo]:
        """List all registered environments, optionally filtered by status."""
        environments: List[EnvironmentInfo] = []
        seen: set[str] = set()

        with _lock:
            for directory in [self.storage_dir, *legacy_environments_dirs()]:
                if not directory.exists():
                    continue
                for env_file in directory.glob("*.json"):
                    if env_file.stem in seen:
                        continue
                    try:
                        data = json.loads(env_file.read_text(encoding="utf-8"))
                        env = EnvironmentInfo.from_dict(data)
                        seen.add(env.agent_id)
                        if status is None or env.status == status:
                            environments.append(env)
                    except (json.JSONDecodeError, KeyError):
                        continue

        environments.sort(key=lambda e: e.created_at or "", reverse=True)
        return environments

    def exists(self, agent_id: str) -> bool:
        """Check if a valid environment record exists for an agent."""
        with _lock:
            data, _src = self._read_env_data(agent_id)
            return data is not None
