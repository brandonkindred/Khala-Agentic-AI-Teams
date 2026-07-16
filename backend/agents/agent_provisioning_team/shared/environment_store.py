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

from .path_safety import candidate_paths, safe_path_component


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
    """Information about a provisioned environment.

    Invariants:
        * ``tools_provisioned`` is always a ``list`` (never ``None``) —
          construction coerces a ``None`` argument to ``[]``.
        * ``created_at`` is always an ISO-8601 timestamp string — construction
          defaults it to the current UTC time when not supplied.
    """

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
        """Construct an environment record.

        Preconditions:
            * ``agent_id``, ``container_id``, ``container_name`` are non-empty
              strings.
            * ``ssh_port`` is a valid port number.
        Postconditions:
            * All fields are set from the corresponding arguments.
            * ``tools_provisioned`` is ``[]`` when ``None`` is passed, else the
              given list.
            * ``created_at`` is the current UTC time in ISO format when not
              supplied, else the given value.
        """
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
        """Serialize this record to a plain dict for JSON persistence.

        Preconditions:
            * None beyond a constructed instance.
        Postconditions:
            * Returns a ``Dict[str, Any]`` with exactly the keys
              ``agent_id``, ``container_id``, ``container_name``,
              ``ssh_host``, ``ssh_port``, ``workspace_path``, ``status``,
              ``tools_provisioned``, ``created_at``, mirroring instance state.
        """
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
        """Reconstruct a record from its serialized dict form.

        Preconditions:
            * ``data`` is a ``Dict[str, Any]`` containing at least the
              required keys ``agent_id``, ``container_id``,
              ``container_name`` (raises ``KeyError`` otherwise).
        Postconditions:
            * Returns a new ``EnvironmentInfo`` populated from ``data``,
              applying the same defaults as ``__init__`` for optional fields
              (``ssh_host``, ``ssh_port``, ``workspace_path``, ``status``,
              ``tools_provisioned``, ``created_at``) when absent from
              ``data``.
        """
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
    """Store for tracking active agent environments.

    Invariants:
        * Each agent's environment is persisted as a single JSON file named
          ``{agent_id}.json`` in the primary ``storage_dir``; reads prefer that
          primary file over any legacy copy (from before the ``AGENT_CACHE`` move).
        * Every public operation serializes on the module-level ``_lock`` so
          concurrent callers never interleave a read/modify/write.
        * Writes always land in the primary ``storage_dir``. A legacy copy is
          pruned (migrated) only by the read-modify-write updates
          ``update_status`` and ``add_tool``/``add_tools`` — which pass the source
          path through to the writer — and by ``remove``. The read-only methods
          ``get``, ``list_all``, and ``exists`` may return data from a legacy
          location without migrating it; ``register`` overwrites the primary
          record but does not prune a pre-existing legacy copy.
    """

    def __init__(self, storage_dir: Optional[Path] = None) -> None:
        self.storage_dir = (
            Path(storage_dir) if storage_dir is not None else default_environments_dir()
        )
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def _env_file(self, agent_id: str) -> Path:
        """Return the environment file path for ``agent_id`` in the primary store.

        Raises ``ValueError`` (via :func:`safe_path_component`) if ``agent_id`` is
        not a safe filename component. This is the store's single validation
        chokepoint: the write path calls it directly (``_write_env_data``) and the
        read/remove path reaches it through ``_env_file_candidates``, so every
        code path that turns ``agent_id`` into a path is guarded here exactly
        once. The returned path is always strictly inside ``storage_dir``.
        """
        return self.storage_dir / f"{safe_path_component(agent_id, kind='agent_id')}.json"

    def _env_file_candidates(self, agent_id: str) -> List[Path]:
        """Primary path first, then legacy locations from before the AGENT_CACHE move.

        The primary path comes from the guarded :meth:`_env_file`; each legacy
        candidate reuses that validated filename via :func:`candidate_paths`, so
        the traversal guard runs once (in :meth:`_env_file`) and every candidate
        is derived from it.
        """
        return candidate_paths(self._env_file(agent_id), legacy_environments_dirs())

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

    def _write_env_data(
        self, agent_id: str, data: Dict[str, Any], source: Optional[Path] = None
    ) -> None:
        """Persist environment JSON to the primary store, dropping a legacy copy."""
        primary = self._env_file(agent_id)
        primary.parent.mkdir(parents=True, exist_ok=True)
        primary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        if source is not None and source != primary and source.exists():
            source.unlink()

    def register(self, env_info: EnvironmentInfo) -> None:
        """Register (or overwrite) an environment record.

        Preconditions:
            * ``env_info`` is not ``None``.
            * ``env_info.agent_id`` is non-empty.
        Postconditions:
            * ``env_info`` is serialized to the primary store, replacing any
              prior record for the same ``agent_id``.
            * Returns ``None``.
        """
        if env_info is None:
            raise ValueError("env_info must not be None")
        with _lock:
            self._write_env_data(env_info.agent_id, env_info.to_dict())

    def get(self, agent_id: str) -> Optional[EnvironmentInfo]:
        """Get environment info for an agent.

        Preconditions:
            * ``agent_id`` is non-empty.
        Postconditions:
            * Returns the ``EnvironmentInfo`` reconstructed from the primary or a
              legacy record when one exists and parses, else ``None``.
            * Malformed or partial records are treated as absent; never raises.
        """
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
        """Update the status of an environment.

        Preconditions:
            * ``agent_id`` is non-empty.
            * ``status`` is the new status string to record.
        Postconditions:
            * When the env exists, sets ``status`` and refreshes ``updated_at``,
              rewrites the record to the primary store, and returns ``True``.
            * Returns ``False`` when the env is missing or its file is corrupt.
        """
        with _lock:
            data, src = self._read_env_data(agent_id)
            if data is None:
                return False
            data["status"] = status
            data["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._write_env_data(agent_id, data, source=src)
            return True

    def add_tool(self, agent_id: str, tool_name: str) -> bool:
        """Add a single tool to the environment's provisioned tools list.

        Preconditions:
            * ``agent_id`` is non-empty.
            * ``tool_name`` is the tool to record (empty is a no-op via
              ``add_tools``).
        Postconditions:
            * Delegates to ``add_tools([tool_name])``; when the env exists and
              ``tool_name`` is non-empty, ``tool_name`` is present in
              ``tools_provisioned`` (an empty ``tool_name`` is a no-op).
            * Returns ``True`` on success, ``False`` when the env is missing or
              corrupt (per ``add_tools``).
        """
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
        """Remove an environment from the registry.

        Preconditions:
            * ``agent_id`` is non-empty.
        Postconditions:
            * Deletes the primary env file and any legacy copies for ``agent_id``.
            * Returns ``True`` iff at least one file was removed; idempotent —
              returns ``False`` when no record existed.
        """
        with _lock:
            removed = False
            for path in self._env_file_candidates(agent_id):
                if path.exists():
                    path.unlink()
                    removed = True
            return removed

    def list_all(self, status: Optional[str] = None) -> List[EnvironmentInfo]:
        """List all registered environments, optionally filtered by status.

        Preconditions:
            * ``status`` is ``None`` (no filter) or a status string to match.
        Postconditions:
            * Returns the ``EnvironmentInfo`` records found across the primary and
              legacy directories, deduplicated by ``agent_id`` (not filename stem,
              so a legacy file whose name differs from its ``agent_id`` cannot
              produce a duplicate entry); the primary ``storage_dir`` is scanned
              first, so its record wins. Results are filtered to ``status`` when
              given and sorted by ``created_at`` descending.
            * Unparseable or incomplete files are skipped; never raises. A record
              whose ``agent_id`` fails :func:`safe_path_component` (e.g. a
              path-traversal string like ``"../../etc/passwd"`` planted in a
              malicious or malformed file) is skipped too, so every returned
              ``agent_id`` is safe for callers to use in a filename or path.
        """
        environments: List[EnvironmentInfo] = []
        seen: set[str] = set()

        with _lock:
            for directory in [self.storage_dir, *legacy_environments_dirs()]:
                if not directory.exists():
                    continue
                for env_file in directory.glob("*.json"):
                    try:
                        data = json.loads(env_file.read_text(encoding="utf-8"))
                        env = EnvironmentInfo.from_dict(data)
                        safe_path_component(env.agent_id, kind="agent_id")
                        if env.agent_id in seen:
                            continue
                        seen.add(env.agent_id)
                        if status is None or env.status == status:
                            environments.append(env)
                    except (json.JSONDecodeError, KeyError, ValueError):
                        continue

        environments.sort(key=lambda e: e.created_at or "", reverse=True)
        return environments

    def exists(self, agent_id: str) -> bool:
        """Check if a valid environment record exists for an agent.

        Preconditions:
            * ``agent_id`` is non-empty.
        Postconditions:
            * Returns ``True`` iff a valid record is readable from the primary or
              a legacy path; never raises.
        """
        with _lock:
            data, _src = self._read_env_data(agent_id)
            return data is not None
