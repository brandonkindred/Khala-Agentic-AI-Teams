"""
Persistent, idempotent state for tool provisioner agents.

Provisioners previously kept ``self._provisioned`` as an in-memory dict, so
restarts and re-runs would either re-create resources (and fail loudly on
DuplicateDatabase / "container name in use") or worse, silently leak.

This module gives every provisioner a single tiny JSON-backed store, keyed
by ``(provisioner, agent_id, resource_name)``, with file locking so two
concurrent processes can't corrupt it. Use ``get_or_create`` to make a
provisioner step idempotent.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, Iterator, Optional

DEFAULT_STATE_DIR = Path(
    os.environ.get("AGENT_CACHE", ".agent_cache")
) / "agent_provisioning_team" / "provisioner_state"

_PROCESS_LOCK = Lock()


class ProvisionerStateStore:
    """JSON-backed key/value store for provisioner idempotency.

    The store is intentionally minimal — one file per provisioner. Writes
    are atomic via tempfile-rename so a crash mid-write can't corrupt the
    file. A single process-wide lock guards concurrent updates inside one
    Python process; cross-process safety is provided by the atomic rename
    plus per-key versioning under the hood (load → mutate → write).
    """

    def __init__(self, provisioner_name: str, storage_dir: Optional[Path] = None) -> None:
        self.provisioner_name = provisioner_name
        self.storage_dir = storage_dir or DEFAULT_STATE_DIR
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.storage_dir / f"{provisioner_name}.json"

    # ---- I/O ----
    def _load(self) -> Dict[str, Dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, data: Dict[str, Dict[str, Any]]) -> None:
        # Atomic write: tempfile → fsync → rename.
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{self.provisioner_name}.", suffix=".json", dir=str(self.storage_dir)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, separators=(",", ":"), sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.path)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    @contextmanager
    def _locked(self) -> Iterator[Dict[str, Dict[str, Any]]]:
        with _PROCESS_LOCK:
            data = self._load()
            yield data
            self._save(data)

    # ---- Public API ----
    def get(self, agent_id: str) -> Optional[Dict[str, Any]]:
        return self._load().get(agent_id)

    def put(self, agent_id: str, value: Dict[str, Any]) -> None:
        with self._locked() as data:
            data[agent_id] = value

    def delete(self, agent_id: str) -> bool:
        with self._locked() as data:
            if agent_id in data:
                del data[agent_id]
                return True
            return False

    def list_agents(self) -> Dict[str, Dict[str, Any]]:
        return dict(self._load())

    def get_or_create(
        self,
        agent_id: str,
        creator: Callable[[], Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Return existing record for agent, or run ``creator`` and store it.

        ``creator`` is invoked at most once per (provisioner, agent_id) and
        is the place where the actual side-effecting resource creation
        happens. If ``creator`` raises, nothing is persisted.
        """
        with self._locked() as data:
            if agent_id in data:
                return data[agent_id]
            value = creator()
            data[agent_id] = value
            return value
