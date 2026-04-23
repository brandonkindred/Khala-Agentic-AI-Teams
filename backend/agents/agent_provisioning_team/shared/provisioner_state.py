"""
Persistent, idempotent state for tool provisioner agents.

Provisioners previously kept ``self._provisioned`` as an in-memory dict, so
restarts and re-runs would either re-create resources (and fail loudly on
DuplicateDatabase / "container name in use") or worse, silently leak.

This module gives every provisioner a single tiny JSON-backed store, keyed
by ``(provisioner, agent_id, resource_name)``, with file locking so two
concurrent processes can't corrupt it. Use ``get_or_create`` to make a
provisioner step idempotent.

On-disk schema (legacy flat rows are migrated transparently on load):

    {
      "agent-uuid": {
        "details": {...},            # what `put(agent_id, details)` stores
        "compensations": [           # LIFO rollback records from run_idempotent
          {"kind": "...", "payload": {...}, "created_at": 1.0}
        ]
      }
    }
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, Iterator, List, Optional

DEFAULT_STATE_DIR = Path(
    os.environ.get("AGENT_CACHE", ".agent_cache")
) / "agent_provisioning_team" / "provisioner_state"

_PROCESS_LOCK = Lock()


@dataclass(frozen=True)
class CompensationRecord:
    """Serializable per-step rollback record.

    Provisioners register these from inside ``create(...)`` as each
    side effect lands, so that a later failure (including a full process
    crash) can replay the rollback in LIFO order. ``payload`` must be
    JSON-serializable; this is enforced at construction time.
    """

    kind: str
    payload: Dict[str, Any]
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        # Fail fast on non-serializable payloads (lambdas, objects) — we
        # want the error at registration time, not at recovery time when
        # the original stack frame is long gone.
        try:
            json.dumps(self.payload)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"CompensationRecord payload is not JSON-serializable: {e}"
            ) from e

    def to_json(self) -> Dict[str, Any]:
        return {"kind": self.kind, "payload": self.payload, "created_at": self.created_at}

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "CompensationRecord":
        return cls(
            kind=data["kind"],
            payload=dict(data.get("payload") or {}),
            created_at=float(data.get("created_at") or time.time()),
        )


def _as_row(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Normalize an on-disk entry into the nested schema.

    Legacy rows were flat (``{<details>}``); new rows are
    ``{"details": {...}, "compensations": [...]}``. We detect legacy rows
    by the absence of a ``"details"`` key and rewrite on read so every
    downstream caller sees the nested shape.
    """
    if raw is None:
        return {"details": {}, "compensations": []}
    if "details" in raw and isinstance(raw["details"], dict):
        comps = raw.get("compensations") or []
        return {"details": raw["details"], "compensations": list(comps)}
    # Legacy flat row — treat the whole thing as details.
    return {"details": dict(raw), "compensations": []}


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
            raw = json.loads(self.path.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError):
            return {}
        # Migrate legacy flat rows on read so every in-memory view is nested.
        return {agent_id: _as_row(row) for agent_id, row in raw.items()}

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
        """Return the flat details dict for ``agent_id`` (backwards-compatible)."""
        row = self._load().get(agent_id)
        if row is None:
            return None
        return dict(row["details"]) if row["details"] else None

    def put(self, agent_id: str, value: Dict[str, Any]) -> None:
        """Persist details for ``agent_id``; preserves any existing compensations."""
        with self._locked() as data:
            existing = data.get(agent_id) or {"details": {}, "compensations": []}
            existing["details"] = dict(value)
            data[agent_id] = existing

    def delete(self, agent_id: str) -> bool:
        with self._locked() as data:
            if agent_id in data:
                del data[agent_id]
                return True
            return False

    def list_agents(self) -> Dict[str, Dict[str, Any]]:
        """Return every agent's flat details dict (legacy shape preserved)."""
        return {aid: dict(row["details"]) for aid, row in self._load().items() if row["details"]}

    def get_or_create(
        self,
        agent_id: str,
        creator: Callable[[], Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Return existing details for agent, or run ``creator`` and store them.

        ``creator`` is invoked at most once per (provisioner, agent_id) and
        is the place where the actual side-effecting resource creation
        happens. If ``creator`` raises, nothing is persisted. Any existing
        compensation records for the agent are preserved across this call.
        """
        with self._locked() as data:
            existing = data.get(agent_id)
            if existing is not None and existing["details"]:
                return dict(existing["details"])
            value = creator()
            row = existing or {"details": {}, "compensations": []}
            row["details"] = dict(value)
            data[agent_id] = row
            return value

    # ---- Compensation records ----
    def add_compensation(self, agent_id: str, record: CompensationRecord) -> None:
        """Append a compensation record for ``agent_id`` (write-through)."""
        with self._locked() as data:
            row = data.get(agent_id) or {"details": {}, "compensations": []}
            comps: List[Dict[str, Any]] = list(row.get("compensations") or [])
            comps.append(record.to_json())
            row["compensations"] = comps
            data[agent_id] = row

    def list_compensations(self, agent_id: str) -> List[CompensationRecord]:
        """Return the compensation records for ``agent_id`` in registration order."""
        row = self._load().get(agent_id)
        if row is None:
            return []
        return [CompensationRecord.from_json(c) for c in row.get("compensations") or []]

    def clear_compensations(self, agent_id: str) -> None:
        """Remove all compensation records for ``agent_id``; keep details intact."""
        with self._locked() as data:
            row = data.get(agent_id)
            if row is None:
                return
            row["compensations"] = []
            data[agent_id] = row
