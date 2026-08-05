"""
Active environment registry for tracking provisioned Docker containers.

Maintains mapping of agent IDs to their container information.
"""

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .fencing import check_fencing_token
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

    Attributes:
        agent_id: Identifier of the agent this environment belongs to.
        container_id: Docker container ID backing the environment.
        container_name: Docker container name backing the environment.
        ssh_host: Host used to reach the environment over SSH.
        ssh_port: Port used to reach the environment over SSH.
        workspace_path: Path to the agent's workspace inside the container.
        status: Current lifecycle status (e.g. ``running``, ``ready``).
        tools_provisioned: Names of tools provisioned into the environment.
        created_at: ISO-8601 timestamp of when the record was first created.
        updated_at: ISO-8601 timestamp of the most recent status/field update;
            defaults to ``created_at`` when not explicitly supplied.

    Invariants:
        * ``tools_provisioned`` is always a ``list`` (never ``None``) —
          construction coerces a ``None`` argument to ``[]``.
        * ``created_at`` is always an ISO-8601 timestamp string — construction
          defaults it to the current UTC time when not supplied.
        * ``updated_at`` is always an ISO-8601 timestamp string — construction
          defaults it to ``created_at`` when not supplied.
        * ``agent_id``, ``container_id``, and ``container_name`` are always
          non-empty ``str`` values, and ``ssh_port`` is always an ``int`` in
          ``1-65535`` — construction is the sole enforcement point (including
          via ``from_dict``, which delegates to ``__init__``), so no
          constructed instance can violate these.
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
        updated_at: Optional[str] = None,
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
            * ``updated_at`` is ``created_at`` when not supplied, else the
              given value.
            * Raises ``ValueError`` when ``agent_id``, ``container_id``, or
              ``container_name`` is not a non-empty ``str``, or when
              ``ssh_port`` is not an ``int`` in ``1-65535``.
        """
        for field_name, value in (
            ("agent_id", agent_id),
            ("container_id", container_id),
            ("container_name", container_name),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{field_name} must be a non-empty string, got {value!r}")
        if not isinstance(ssh_port, int) or not (1 <= ssh_port <= 65535):
            raise ValueError(f"ssh_port must be a valid port number (1-65535), got {ssh_port!r}")
        self.agent_id = agent_id
        self.container_id = container_id
        self.container_name = container_name
        self.ssh_host = ssh_host
        self.ssh_port = ssh_port
        self.workspace_path = workspace_path
        self.status = status
        self.tools_provisioned = tools_provisioned or []
        self.created_at = (
            created_at if created_at is not None else datetime.now(timezone.utc).isoformat()
        )
        self.updated_at = updated_at if updated_at is not None else self.created_at

    def to_dict(self) -> Dict[str, Any]:
        """Serialize this record to a plain dict for JSON persistence.

        Preconditions:
            * None beyond a constructed instance.
        Postconditions:
            * Returns a ``Dict[str, Any]`` with exactly the keys
              ``agent_id``, ``container_id``, ``container_name``,
              ``ssh_host``, ``ssh_port``, ``workspace_path``, ``status``,
              ``tools_provisioned``, ``created_at``, ``updated_at``,
              mirroring instance state.
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
            "updated_at": self.updated_at,
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
              ``tools_provisioned``, ``created_at``, ``updated_at``) when
              absent from ``data``.
            * Delegates to ``__init__``, so also raises ``ValueError`` when
              ``agent_id``, ``container_id``, or ``container_name`` is not a
              non-empty ``str``, or ``ssh_port`` is not a valid port number.
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
            updated_at=data.get("updated_at"),
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
            * Malformed JSON, invalid UTF-8 bytes, or incomplete records are
              skipped (treated as absent).
        """
        required = ("agent_id", "container_id", "container_name")
        for path in self._env_file_candidates(agent_id):
            # Path.exists() itself can raise OSError (e.g. EACCES on a parent
            # directory), so it lives inside the handler — otherwise `get`'s
            # "never raises" postcondition would be false. read_text() can also
            # raise UnicodeDecodeError on invalid UTF-8 bytes, which is neither
            # OSError nor JSONDecodeError.
            try:
                if not path.exists():
                    continue
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                continue
            if not isinstance(data, dict) or any(k not in data for k in required):
                continue
            return data, path
        return None, None

    def _read_raw_fencing_token(self, agent_id: str) -> Optional[int]:
        """Read a lingering fencing token from a tombstone :meth:`remove` left behind.

        Preconditions:
            * ``agent_id`` is non-empty.
        Postconditions:
            * Bypasses ``_read_env_data``'s required-fields check — a
              tombstone deliberately omits ``container_id``/
              ``container_name`` so ordinary readers (``get``/``list_all``/
              ``exists``) treat it as absent, but the fencing high-water
              mark it carries must still be found here by :meth:`register`'s
              own prior-token lookup, or a stale caller's write would read
              ``current_token=None`` (bootstrap) and be wrongly accepted.
            * Returns the first existing candidate's ``fencing_token`` when
              it is an ``int``, else ``None``. Never raises.
        """
        for path in self._env_file_candidates(agent_id):
            try:
                if not path.exists():
                    continue
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                continue
            token = data.get("fencing_token") if isinstance(data, dict) else None
            return token if isinstance(token, int) else None
        return None

    def _write_env_data(
        self, agent_id: str, data: Dict[str, Any], source: Optional[Path] = None
    ) -> None:
        """Persist environment JSON to the primary store, dropping a legacy copy.

        Postconditions:
            * The write is atomic (tempfile → fsync → ``os.replace``, matching
              ``provisioner_state._save``): on any failure the primary file holds
              its previous content — never a truncated or partial record — so a
              raising write leaves the prior registration intact.
            * The temp file's suffix is deliberately not ``.json``:
              ``list_all``'s ``glob("*.json")`` (unlike shell globbing) matches
              dotfiles too, so a fully-written temp file left behind by a crash
              between ``fsync`` and ``os.replace`` would otherwise be scanned as
              a phantom environment record indistinguishable from a real one.
        """
        primary = self._env_file(agent_id)
        primary.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{primary.stem}.", suffix=".tmp", dir=str(primary.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(data, indent=2))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, primary)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        if source is not None and source != primary and source.exists():
            source.unlink()

    def register(self, env_info: EnvironmentInfo, *, fencing_token: Optional[int] = None) -> None:
        """Register (or overwrite) an environment record.

        Preconditions:
            * ``env_info`` is not ``None``.
            * ``env_info.agent_id`` is non-empty.
        Postconditions:
            * ``env_info`` is serialized to the primary store, replacing any
              prior record for the same ``agent_id``.
            * The replacement is atomic: if the write raises, the prior record
              (or the absence of one) is preserved unchanged — a failed register
              never leaves a truncated or partial record behind.
            * Returns ``None``.
            * When ``fencing_token`` is given and a prior record for this
              ``agent_id`` already recorded a higher fencing token, raises
              :class:`~agent_team_studio.agent_provisioning_team.shared.fencing.StaleFencingTokenError`
              and leaves the record untouched. Otherwise the given token (or,
              when ``fencing_token`` is ``None``, whichever token — if any —
              the prior record already carried) becomes the new record's
              recorded token. The prior-token lookup also finds a fencing
              tombstone :meth:`remove` left behind (no valid record exists,
              but the high-water mark survives) — this is the only write
              method here that creates a record unconditionally rather than
              modifying an existing one, so it is the one place a stale
              caller could otherwise resurrect a torn-down environment by
              reading a bootstrap ``current_token=None``.
        """
        if env_info is None:
            raise ValueError("env_info must not be None")
        if not env_info.agent_id:
            raise ValueError("agent_id must not be empty")
        with _lock:
            existing_data, _src = self._read_env_data(env_info.agent_id)
            if existing_data is not None:
                prior_token = existing_data.get("fencing_token")
            else:
                prior_token = self._read_raw_fencing_token(env_info.agent_id)
            prior_token = prior_token if isinstance(prior_token, int) else None
            if fencing_token is not None:
                check_fencing_token(
                    agent_id=env_info.agent_id,
                    resource="environment_store",
                    provided_token=fencing_token,
                    current_token=prior_token,
                )
            data = env_info.to_dict()
            data["fencing_token"] = fencing_token if fencing_token is not None else prior_token
            self._write_env_data(env_info.agent_id, data)

    def readable(self, agent_id: str) -> bool:
        """Report whether ``agent_id``'s record location(s) can be examined now.

        Preconditions:
            * ``agent_id`` is non-empty.
        Postconditions:
            * Returns ``True`` iff every existing candidate path for
              ``agent_id`` (primary store, then legacy locations) is either
              genuinely absent, or present and parses as a well-formed record
              that also passes the same semantic validation ``get`` applies
              (via ``EnvironmentInfo.from_dict``); never raises.
            * A path that does not exist is not a readability failure. A path
              that exists but cannot be stat'd/read (``OSError``), whose bytes
              are not valid UTF-8 (``UnicodeDecodeError``), or whose content is
              not a well-formed record (malformed JSON, valid JSON missing a
              required key, or a required key present with an invalid value —
              e.g. an out-of-range ``ssh_port`` — that fails construction), IS a
              readability failure — such a file is evidence *something* was
              written there, which must not be conflated with confirmed
              absence.

        A listable directory is not sufficient: the specific record file can be
        individually unreadable (bad mode, transient I/O error) while sibling
        files list fine, and a legacy-location record is invisible to a
        primary-only directory listing. Nor is "the bytes could be read"
        sufficient on its own: ``get`` maps a malformed/incomplete/invalid
        record to ``None`` — correct for lookups, since callers don't care why
        a record is unusable — but a destructive rollback caller needs to know
        the difference between "confirmed nothing was ever registered here"
        and "something is here but we can't trust it" before treating
        ``get() is None`` as proof an orphan is safe to reclaim. Applying the
        exact same validation as ``get`` (rather than a shallower "required
        keys present" check) keeps the two methods from disagreeing on a
        present-but-invalid record — disagreement that previously let such a
        record be misread as confirmed absence.

        Deliberately does not pre-check with ``Path.exists()``: it stats the
        path and treats *any* ``OSError`` (not just "doesn't exist") as
        `False`, so a transient stat failure (e.g. a parent directory
        temporarily returning ``EACCES``) would be indistinguishable from
        confirmed absence and this method would wrongly report readable.
        Attempting the read directly and catching ``FileNotFoundError``
        specifically (rather than ``OSError`` broadly) preserves that
        distinction.
        """
        assert agent_id, "agent_id must be non-empty"
        with _lock:
            for path in self._env_file_candidates(agent_id):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    continue
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                    return False
                if not isinstance(data, dict):
                    return False
                try:
                    EnvironmentInfo.from_dict(data)
                except (KeyError, TypeError, ValueError):
                    return False
        return True

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

    def update_status(
        self, agent_id: str, status: str, *, fencing_token: Optional[int] = None
    ) -> bool:
        """Update the status of an environment.

        Preconditions:
            * ``agent_id`` is non-empty.
            * ``status`` is the new status string to record.
        Postconditions:
            * When the env exists, sets ``status`` and refreshes ``updated_at``,
              rewrites the record to the primary store, and returns ``True``.
            * Returns ``False`` when the env is missing or its file is corrupt
              (including a well-formed record with an invalid field value).
            * When ``fencing_token`` is given and lower than this record's
              already-recorded fencing token, raises
              :class:`~agent_team_studio.agent_provisioning_team.shared.fencing.StaleFencingTokenError`
              and leaves the record untouched.
        """
        with _lock:
            data, src = self._read_env_data(agent_id)
            if data is None:
                return False
            prior_token = data.get("fencing_token")
            prior_token = prior_token if isinstance(prior_token, int) else None
            if fencing_token is not None:
                check_fencing_token(
                    agent_id=agent_id,
                    resource="environment_store",
                    provided_token=fencing_token,
                    current_token=prior_token,
                )
            try:
                info = EnvironmentInfo.from_dict(data)
            except (KeyError, TypeError, ValueError):
                return False
            info.status = status
            info.updated_at = datetime.now(timezone.utc).isoformat()
            new_data = info.to_dict()
            new_data["fencing_token"] = fencing_token if fencing_token is not None else prior_token
            self._write_env_data(agent_id, new_data, source=src)
            return True

    def add_tool(
        self, agent_id: str, tool_name: str, *, fencing_token: Optional[int] = None
    ) -> bool:
        """Add a single tool to the environment's provisioned tools list.

        Preconditions:
            * ``agent_id`` is non-empty.
            * ``tool_name`` is the tool to record (empty is a no-op via
              ``add_tools``).
        Postconditions:
            * Delegates to ``add_tools([tool_name], fencing_token=fencing_token)``;
              when the env exists and ``tool_name`` is non-empty, ``tool_name``
              is present in ``tools_provisioned`` (an empty ``tool_name`` is a
              no-op).
            * Returns ``True`` on success, ``False`` when the env is missing or
              corrupt (per ``add_tools``).
        """
        return self.add_tools(agent_id, [tool_name], fencing_token=fencing_token)

    def add_tools(
        self, agent_id: str, tool_names: List[str], *, fencing_token: Optional[int] = None
    ) -> bool:
        """Add zero or more tools in one read/modify/write under the store lock.

        Preconditions:
            * ``agent_id`` is non-empty.
            * ``tool_names`` may be empty (no-op success when the env exists).
        Postconditions:
            * When the env file exists, every non-empty unique name in
              ``tool_names`` is present in ``tools_provisioned`` (order of first
              appearance preserved for new names) and ``updated_at`` is
              refreshed.
            * Returns ``False`` when the env is missing or the file is corrupt
              (including a well-formed record with an invalid field value).
            * When ``fencing_token`` is given and lower than this record's
              already-recorded fencing token, raises
              :class:`~agent_team_studio.agent_provisioning_team.shared.fencing.StaleFencingTokenError`
              and leaves the record untouched.
        """
        with _lock:
            data, src = self._read_env_data(agent_id)
            if data is None:
                return False
            prior_token = data.get("fencing_token")
            prior_token = prior_token if isinstance(prior_token, int) else None
            if fencing_token is not None:
                check_fencing_token(
                    agent_id=agent_id,
                    resource="environment_store",
                    provided_token=fencing_token,
                    current_token=prior_token,
                )
            try:
                info = EnvironmentInfo.from_dict(data)
            except (KeyError, TypeError, ValueError):
                return False
            tools = list(info.tools_provisioned)
            for tool_name in tool_names:
                if tool_name and tool_name not in tools:
                    tools.append(tool_name)
            info.tools_provisioned = tools
            info.updated_at = datetime.now(timezone.utc).isoformat()
            new_data = info.to_dict()
            new_data["fencing_token"] = fencing_token if fencing_token is not None else prior_token
            self._write_env_data(agent_id, new_data, source=src)
            return True

    def remove(self, agent_id: str, *, fencing_token: Optional[int] = None) -> bool:
        """Remove an environment from the registry.

        Preconditions:
            * ``agent_id`` is non-empty.
        Postconditions:
            * Deletes the primary env file and any legacy copies for ``agent_id``.
            * Returns ``True`` iff at least one file was removed; idempotent —
              returns ``False`` when no record existed.
            * When ``fencing_token`` is given and lower than this record's
              already-recorded fencing token, raises
              :class:`~agent_team_studio.agent_provisioning_team.shared.fencing.StaleFencingTokenError`
              and removes nothing.
            * When ``fencing_token`` is given, a tombstone carrying just that
              token is written to the primary path afterward — deleting every
              file outright would reset a later caller's prior-token lookup
              to ``current_token=None`` (bootstrap), letting a stale caller's
              ``register`` call silently recreate an environment a newer
              owner already tore down. The tombstone deliberately omits
              ``container_id``/``container_name``, so ``get``/``list_all``/
              ``exists`` all still correctly treat it as absent — only
              ``register``'s own prior-token lookup (via
              ``_read_raw_fencing_token``) reads it. When ``fencing_token``
              is ``None``, behavior is unchanged: no tombstone is written.
        """
        with _lock:
            if fencing_token is not None:
                data, _src = self._read_env_data(agent_id)
                if data is not None:
                    prior_token = data.get("fencing_token")
                else:
                    prior_token = self._read_raw_fencing_token(agent_id)
                check_fencing_token(
                    agent_id=agent_id,
                    resource="environment_store",
                    provided_token=fencing_token,
                    current_token=prior_token if isinstance(prior_token, int) else None,
                )
            removed = False
            for path in self._env_file_candidates(agent_id):
                if path.exists():
                    path.unlink()
                    removed = True
            if fencing_token is not None:
                self._write_env_data(agent_id, {"fencing_token": fencing_token})
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
            * Unparseable, non-dict (e.g. a JSON array), incomplete, or
              unreadable files (e.g. permissions changed or the file was
              deleted between the directory scan and the read) are skipped;
              never raises. A record whose ``agent_id`` fails
              :func:`safe_path_component` (e.g. a path-traversal string like
              ``"../../etc/passwd"`` planted in a malicious or malformed file)
              is skipped too, so every returned ``agent_id`` is safe for
              callers to use in a filename or path.
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
                        if not isinstance(data, dict):
                            continue
                        env = EnvironmentInfo.from_dict(data)
                        safe_path_component(env.agent_id, kind="agent_id")
                        if env.agent_id in seen:
                            continue
                        seen.add(env.agent_id)
                        if status is None or env.status == status:
                            environments.append(env)
                    except (json.JSONDecodeError, KeyError, ValueError, OSError):
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
