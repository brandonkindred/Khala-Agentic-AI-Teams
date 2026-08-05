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
        ],
        "fencing_token": 3           # highest fencing token accepted so far;
                                      # absent/None on rows never written with one
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

from .fencing import check_fencing_token
from .path_safety import safe_path_component

DEFAULT_STATE_DIR = (
    Path(os.environ.get("AGENT_CACHE", ".agent_cache"))
    / "agent_provisioning_team"
    / "provisioner_state"
)

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
            raise ValueError(f"CompensationRecord payload is not JSON-serializable: {e}") from e

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
    ``{"details": {...}, "compensations": [...], "fencing_token": int}``
    (``fencing_token`` is optional/absent on rows written before fencing
    tokens existed, or never fenced). We detect legacy rows by the absence
    of a ``"details"`` key and rewrite on read so every downstream caller
    sees the nested shape.
    """
    if raw is None:
        return {"details": {}, "compensations": [], "fencing_token": None}
    if "details" in raw and isinstance(raw["details"], dict):
        comps = raw.get("compensations") or []
        token = raw.get("fencing_token")
        return {
            "details": raw["details"],
            "compensations": list(comps),
            "fencing_token": token if isinstance(token, int) else None,
        }
    # Legacy flat row — treat the whole thing as details.
    return {"details": dict(raw), "compensations": [], "fencing_token": None}


class ProvisionerStateStore:
    """JSON-backed key/value store for provisioner idempotency.

    The store is intentionally minimal — one file per provisioner. Writes
    are atomic via tempfile-rename so a crash mid-write can't corrupt the
    file. A single process-wide lock guards concurrent updates inside one
    Python process; cross-process safety is provided by the atomic rename
    plus per-key versioning under the hood (load → mutate → write).
    """

    def __init__(self, provisioner_name: str, storage_dir: Optional[Path] = None) -> None:
        """Bind the store to one provisioner's JSON file.

        Raises ``ValueError`` if ``provisioner_name`` is not a safe filename
        component. The guard's containment role is on ``self.path`` — the
        record's final location, ``storage_dir / f"{provisioner_name}.json"``.
        ``_save`` also uses the validated name as a tempfile *prefix*, but that
        write targets ``storage_dir`` explicitly via ``mkstemp(dir=...)``, so the
        prefix cannot change the output directory; validating the name simply
        keeps that prefix a well-formed filename fragment too.
        """
        self.provisioner_name = safe_path_component(provisioner_name, kind="provisioner_name")
        self.storage_dir = storage_dir or DEFAULT_STATE_DIR
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.storage_dir / f"{self.provisioner_name}.json"

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
        """Return the flat details dict for ``agent_id`` (backwards-compatible).

        Read-only and intentionally lock-free: ``_save`` commits with an atomic
        ``os.replace``, so every ``_load`` observes a whole committed snapshot
        (never a torn write) without taking ``_PROCESS_LOCK`` — the lock only
        serialises the read-modify-write mutators. A concurrent write may make
        this return the pre- or post-write snapshot, which is acceptable for the
        idempotency lookups this store backs.

        An empty stored ``details`` dict is treated as absent and returned as
        ``None`` (indistinguishable from a missing key). This "empty is absent"
        rule is intentional and shared with ``list_agents`` and
        ``get_or_create``.
        """
        row = self._load().get(agent_id)
        if row is None:
            return None
        return dict(row["details"]) if row["details"] else None

    def put(
        self, agent_id: str, value: Dict[str, Any], *, fencing_token: Optional[int] = None
    ) -> None:
        """Persist details for ``agent_id``; preserves any existing compensations.

        When ``fencing_token`` is given and lower than this agent's
        already-recorded fencing token, raises
        :class:`~agent_team_studio.agent_provisioning_team.shared.fencing.StaleFencingTokenError`
        and leaves the row untouched.
        """
        with self._locked() as data:
            existing = data.get(agent_id) or {"details": {}, "compensations": []}
            if fencing_token is not None:
                prior_token = existing.get("fencing_token")
                check_fencing_token(
                    agent_id=agent_id,
                    resource=f"provisioner_state:{self.provisioner_name}",
                    provided_token=fencing_token,
                    current_token=prior_token if isinstance(prior_token, int) else None,
                )
                existing["fencing_token"] = fencing_token
            existing["details"] = dict(value)
            data[agent_id] = existing

    def delete(self, agent_id: str, *, fencing_token: Optional[int] = None) -> bool:
        """Clear ``agent_id``'s details and compensations (row itself is kept).

        When ``fencing_token`` is given and lower than this agent's
        already-recorded fencing token, raises
        :class:`~agent_team_studio.agent_provisioning_team.shared.fencing.StaleFencingTokenError`
        and deletes nothing.

        Postconditions:
            * The row is never removed outright — only ``details``/
              ``compensations`` are cleared, and ``fencing_token`` (when
              given) becomes the row's new high-water mark. Removing the row
              entirely would reset a later caller's bootstrap comparison
              (``current_token=None``) to always-accept, letting a stale
              caller's post-teardown write silently resurrect state a newer
              owner already tore down. ``get()``/``list_agents()`` already
              treat an empty ``details`` dict as absent, so this preserves
              their existing "gone" contract for every caller that doesn't
              care about the fencing high-water mark.
            * Returns ``True`` iff ``agent_id`` had non-empty ``details``
              before this call — i.e. there was a live entry to clear, not
              merely a row (a tombstone left by an earlier ``delete`` has an
              empty ``details``, so a repeat call against it returns
              ``False``, same idempotent-delete contract as before this row
              stopped being removed outright).
        """
        with self._locked() as data:
            if agent_id not in data:
                return False
            row = data[agent_id]
            if fencing_token is not None:
                prior_token = row.get("fencing_token")
                check_fencing_token(
                    agent_id=agent_id,
                    resource=f"provisioner_state:{self.provisioner_name}",
                    provided_token=fencing_token,
                    current_token=prior_token if isinstance(prior_token, int) else None,
                )
            had_content = bool(row.get("details"))
            data[agent_id] = {
                "details": {},
                "compensations": [],
                "fencing_token": fencing_token
                if fencing_token is not None
                else row.get("fencing_token"),
            }
            return had_content

    def check_fencing_token(self, agent_id: str, fencing_token: int) -> None:
        """Reject a stale token before any real infrastructure mutation runs.

        Read-check only — does not persist. Provisioners call this as the
        first statement in ``provision``/``deprovision`` (before touching
        Docker/Postgres/Redis/git), so a stale caller is rejected *before*
        performing a real side effect, not merely before its own bookkeeping
        write. The caller's own follow-up ``put``/``delete``/
        ``add_compensation`` call (passed the same ``fencing_token``)
        performs the actual persisted high-water-mark bump.

        Raises :class:`~agent_team_studio.agent_provisioning_team.shared.fencing.StaleFencingTokenError`
        when ``fencing_token`` is lower than this agent's already-recorded
        fencing token; otherwise returns ``None``.

        Intentionally lock-free (mirrors :meth:`get`): it only *reads* the
        high-water mark, so it must not enter the read-modify-write path.
        Using ``self._locked()`` here would re-serialise + fsync + atomically
        rename the entire store on every preflight — and this runs as the
        first statement of every provision/deprovision — purely to compare
        one integer. ``_load`` observes a whole committed snapshot via the
        mutators' atomic ``os.replace``, which is all this comparison needs.
        """
        row = self._load().get(agent_id) or {"details": {}, "compensations": []}
        prior_token = row.get("fencing_token")
        check_fencing_token(
            agent_id=agent_id,
            resource=f"provisioner_state:{self.provisioner_name}",
            provided_token=fencing_token,
            current_token=prior_token if isinstance(prior_token, int) else None,
        )

    def list_agents(self) -> Dict[str, Dict[str, Any]]:
        """Return every agent's flat details dict (legacy shape preserved).

        Lock-free read (see :meth:`get`); agents whose stored ``details`` is
        empty are omitted, matching ``get``'s "empty is absent" rule.
        """
        return {aid: dict(row["details"]) for aid, row in self._load().items() if row["details"]}

    def get_or_create(
        self,
        agent_id: str,
        creator: Callable[[], Dict[str, Any]],
        *,
        fencing_token: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Return existing details for agent, or run ``creator`` and store them.

        ``creator`` is invoked at most once per (provisioner, agent_id) and
        is the place where the actual side-effecting resource creation
        happens. If ``creator`` raises, nothing is persisted. Any existing
        compensation records for the agent are preserved across this call.

        When ``fencing_token`` is given and lower than this agent's
        already-recorded fencing token, raises
        :class:`~agent_team_studio.agent_provisioning_team.shared.fencing.StaleFencingTokenError`
        before invoking ``creator``.
        """
        with self._locked() as data:
            existing = data.get(agent_id)
            if fencing_token is not None:
                prior_token = existing.get("fencing_token") if existing else None
                check_fencing_token(
                    agent_id=agent_id,
                    resource=f"provisioner_state:{self.provisioner_name}",
                    provided_token=fencing_token,
                    current_token=prior_token if isinstance(prior_token, int) else None,
                )
            if existing is not None and existing["details"]:
                return dict(existing["details"])
            value = creator()
            row = existing or {"details": {}, "compensations": []}
            row["details"] = dict(value)
            if fencing_token is not None:
                row["fencing_token"] = fencing_token
            data[agent_id] = row
            return value

    # ---- Compensation records ----
    def add_compensation(
        self, agent_id: str, record: CompensationRecord, *, fencing_token: Optional[int] = None
    ) -> None:
        """Append a compensation record for ``agent_id`` (write-through).

        When ``fencing_token`` is given and lower than this agent's
        already-recorded fencing token, raises
        :class:`~agent_team_studio.agent_provisioning_team.shared.fencing.StaleFencingTokenError`
        and appends nothing.
        """
        with self._locked() as data:
            row = data.get(agent_id) or {"details": {}, "compensations": []}
            if fencing_token is not None:
                prior_token = row.get("fencing_token")
                check_fencing_token(
                    agent_id=agent_id,
                    resource=f"provisioner_state:{self.provisioner_name}",
                    provided_token=fencing_token,
                    current_token=prior_token if isinstance(prior_token, int) else None,
                )
                row["fencing_token"] = fencing_token
            comps: List[Dict[str, Any]] = list(row.get("compensations") or [])
            comps.append(record.to_json())
            row["compensations"] = comps
            data[agent_id] = row

    def list_compensations(self, agent_id: str) -> List[CompensationRecord]:
        """Return the compensation records for ``agent_id`` in registration order.

        Lock-free read (see :meth:`get`).
        """
        row = self._load().get(agent_id)
        if row is None:
            return []
        return [CompensationRecord.from_json(c) for c in row.get("compensations") or []]

    def clear_compensations(self, agent_id: str, *, fencing_token: Optional[int] = None) -> None:
        """Remove all compensation records for ``agent_id``; keep details intact.

        When ``fencing_token`` is given and lower than this agent's
        already-recorded fencing token, raises
        :class:`~agent_team_studio.agent_provisioning_team.shared.fencing.StaleFencingTokenError`
        and leaves the compensation list untouched.
        """
        with self._locked() as data:
            row = data.get(agent_id)
            if row is None:
                return
            if fencing_token is not None:
                prior_token = row.get("fencing_token")
                check_fencing_token(
                    agent_id=agent_id,
                    resource=f"provisioner_state:{self.provisioner_name}",
                    provided_token=fencing_token,
                    current_token=prior_token if isinstance(prior_token, int) else None,
                )
                row["fencing_token"] = fencing_token
            row["compensations"] = []
            data[agent_id] = row
