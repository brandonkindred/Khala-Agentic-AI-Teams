"""
Manifest loader and in-memory registry for the Agent Console catalog.

Discovery rule: scan ``<agents_root>/*/agent_console/manifests/*.yaml``. Each file
yields one :class:`AgentManifest`. Duplicates on ``id`` are dropped (last one
wins with a warning). Manifests whose ``team`` is not in ``TEAM_CONFIGS`` are
kept but logged as orphans.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Sequence

import yaml
from pydantic import ValidationError

from shared.env import parse_float, parse_int

from .models import AgentDetail, AgentManifest, AgentSummary, TeamGroup

logger = logging.getLogger(__name__)


def _agents_root() -> Path:
    """Path to ``backend/agents/``. We live at ``backend/agents/agent_registry/loader.py``."""
    return Path(__file__).resolve().parent.parent


def _discover_manifest_files(root: Path) -> list[Path]:
    """Return every ``<team>/agent_console/manifests/*.yaml`` under ``root``."""
    return sorted(root.glob("*/agent_console/manifests/*.yaml"))


def _load_team_display_names() -> dict[str, str]:
    """Best-effort import of TEAM_CONFIGS so we can pretty-print team names.

    Returns an empty dict if the import fails (e.g. when the registry is used
    from a test harness that does not have unified_api on the path), or if a
    team key is simply absent from ``TEAM_CONFIGS``. Title-casing the team key
    as a display-name fallback is the caller's responsibility — see
    :meth:`AgentRegistry.teams`.
    """
    try:
        from unified_api.config import TEAM_CONFIGS  # type: ignore
    except ImportError:
        # Expected outside a full unified_api checkout (e.g. a test harness) —
        # not worth surfacing above debug.
        logger.debug(
            "Could not import unified_api.config.TEAM_CONFIGS; using key-derived names",
            exc_info=True,
        )
        return {}
    try:
        return {key: cfg.name for key, cfg in TEAM_CONFIGS.items()}
    except Exception:  # pragma: no cover — defensive
        # unified_api.config imported fine but TEAM_CONFIGS itself is malformed —
        # an unexpected configuration bug, not the routine "not on path" case, so
        # operators should see it.
        logger.warning(
            "TEAM_CONFIGS imported but could not be read; using key-derived names",
            exc_info=True,
        )
        return {}


class AgentRegistry:
    """Registry of agent manifests, merging static disk manifests with a shared
    dynamic overlay.

    Static manifests are loaded from disk once at :meth:`load` and resolved with
    zero Postgres cost (see ``_static_ids``). Manifests registered at runtime
    (Agent Studio saves, generated team agents) live in-memory and, when a
    Postgres-backed dynamic store is active, write through to it so every worker
    resolves the same dynamic id coherently; see :meth:`get`, :meth:`register`,
    and :meth:`unregister`.
    """

    # Defaults for the env-tunable properties below. Bounds how long a
    # locally-issued unregister() masks a dynamic id from get() after its
    # best-effort Postgres delete fails (see _is_tombstoned). Short enough that
    # a legitimate external re-registration of the same id (another worker)
    # becomes visible again promptly; long enough to close the window where the
    # very next get() on this worker would otherwise resurrect the stale row it
    # just tried to delete.
    _DEFAULT_TOMBSTONE_TTL_S = 5.0
    # Bounds _tombstones' size so an id that's unregistered and never revisited
    # doesn't accumulate forever over a long-lived worker's lifetime; oldest
    # entries are evicted first once the cap is exceeded (see unregister()).
    _DEFAULT_TOMBSTONE_MAX_ENTRIES = 1000

    @property
    def _TOMBSTONE_TTL_S(self) -> float:
        """Tombstone window (seconds), re-read from the environment on every access.

        Postconditions:
            * Returns ``AGENT_REGISTRY_TOMBSTONE_TTL_S`` parsed as a float,
              clamped to ``>= 0.0``; falls back to
              :attr:`_DEFAULT_TOMBSTONE_TTL_S` when unset/blank/unparseable. A
              property (not a class constant) so operators can tune it via the
              environment without a code change.
        """
        return parse_float(
            "AGENT_REGISTRY_TOMBSTONE_TTL_S", self._DEFAULT_TOMBSTONE_TTL_S, minimum=0.0
        )

    @property
    def _TOMBSTONE_MAX_ENTRIES(self) -> int:
        """Max size of ``_tombstones``, re-read from the environment on every access.

        Postconditions:
            * Returns ``AGENT_REGISTRY_TOMBSTONE_MAX_ENTRIES`` parsed as an int,
              clamped to ``>= 1``; falls back to
              :attr:`_DEFAULT_TOMBSTONE_MAX_ENTRIES` when unset/blank/unparseable.
        """
        return parse_int(
            "AGENT_REGISTRY_TOMBSTONE_MAX_ENTRIES",
            self._DEFAULT_TOMBSTONE_MAX_ENTRIES,
            minimum=1,
        )

    def __init__(
        self,
        manifests: list[AgentManifest],
        team_display_names: dict[str, str],
        source_paths: dict[str, Path] | None = None,
    ) -> None:
        self._by_id: dict[str, AgentManifest] = {m.id: m for m in manifests}
        # Ids present at construction are the **disk/static** manifests: never
        # written to the dynamic Postgres store (they're already on every worker's
        # disk and in every sandbox image) and always resolved locally, so the
        # built-in-agent invoke hot path never touches Postgres. Runtime
        # ``register()`` of a *new* id does NOT add to this set — that's a dynamic
        # manifest, and it is what gets persisted / read cross-worker.
        self._static_ids: frozenset[str] = frozenset(self._by_id)
        self._team_display_names = team_display_names
        # Map agent_id → the YAML file it was loaded from. Used to locate
        # per-agent ``samples/`` directories without assuming that the team
        # directory name matches ``manifest.team`` (e.g. ``branding_team`` vs.
        # ``branding``).
        self._source_paths: dict[str, Path] = source_paths or {}
        # agent_id -> time.monotonic() of this worker's last unregister(), oldest
        # first. See _is_tombstoned / _TOMBSTONE_TTL_S / _TOMBSTONE_MAX_ENTRIES.
        self._tombstones: "OrderedDict[str, float]" = OrderedDict()
        # Dynamic ids whose Postgres write-through in register() FAILED (or which
        # were registered while no dynamic store was active). Only these fall back
        # to the local ``_by_id`` copy on an *authoritative store miss* — see get().
        # This is what makes the local fallback read-your-writes-only: an id whose
        # write-through succeeded is trusted to the store, so a later store miss
        # means it was deleted on another worker (drop it) rather than resurrecting
        # a stale local copy forever. Entries clear on a later successful
        # register()/upsert or on unregister().
        self._unconfirmed: set[str] = set()
        # Guards every local mutation (register/unregister) and iteration
        # (_merged_manifests/manifests_with_id_prefix) of _by_id/_tombstones/
        # _source_paths. FastAPI runs sync `def` routes in a threadpool, so
        # register()/unregister() on one request can genuinely race a concurrent
        # all()/search() iterating the same dict on another thread — CPython
        # raises ``RuntimeError: dictionary changed size during iteration`` if a
        # key is added/removed mid-scan. Never held across a dynamic-store
        # Postgres call (those run outside the lock) so a slow query can't
        # serialize the whole registry.
        self._lock = threading.Lock()

    def _is_tombstoned(self, agent_id: str) -> bool:
        """Whether this worker locally unregistered ``agent_id`` within the TTL window.

        Preconditions:
            * ``agent_id`` is a string.
        Postconditions:
            * Returns ``True`` iff ``unregister(agent_id)`` ran on this worker less
              than :attr:`_TOMBSTONE_TTL_S` ago, ``False`` otherwise (including an
              expired entry). Read-only — never mutates ``_tombstones``; expiry and
              size-cap pruning happen only in :meth:`unregister`, not here.
        Invariants:
            * Safe to call without holding :attr:`_lock` — ``OrderedDict.get`` is
              atomic in CPython — but every current caller (:meth:`get`,
              :meth:`_drop_tombstoned`) already holds the lock for the surrounding
              read, so this always observes a consistent snapshot in practice.
        """
        stamped_at = self._tombstones.get(agent_id)
        return stamped_at is not None and (time.monotonic() - stamped_at) <= self._TOMBSTONE_TTL_S

    # ---------------------------------------------------------------
    # Construction
    # ---------------------------------------------------------------

    @classmethod
    def load(cls, root: Path | None = None) -> "AgentRegistry":
        root = root or _agents_root()
        team_names = _load_team_display_names()
        manifests: dict[str, AgentManifest] = {}
        source_paths: dict[str, Path] = {}

        for path in _discover_manifest_files(root):
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                logger.warning("Skipping malformed agent manifest %s: %s", path, exc)
                continue
            if not isinstance(raw, dict):
                logger.warning("Skipping non-object agent manifest %s", path)
                continue

            try:
                manifest = AgentManifest.model_validate(raw)
            except ValidationError as exc:
                logger.warning("Skipping invalid agent manifest %s: %s", path, exc)
                continue

            if manifest.id in manifests:
                logger.warning(
                    "Duplicate agent id '%s' in %s; overwriting previous entry", manifest.id, path
                )
            if team_names and manifest.team not in team_names:
                logger.warning(
                    "Agent '%s' references unknown team '%s' (not in TEAM_CONFIGS)",
                    manifest.id,
                    manifest.team,
                )
            manifests[manifest.id] = manifest
            source_paths[manifest.id] = path

        logger.info("Agent registry loaded %d manifest(s) from %s", len(manifests), root)
        return cls(list(manifests.values()), team_names, source_paths)

    # ---------------------------------------------------------------
    # Queries
    # ---------------------------------------------------------------

    def _dynamic_store(self):
        """Return the ``dynamic_store`` module iff it should back this process.

        Postconditions:
            * Returns the module when Postgres is configured and we are not inside
              a per-invoke sandbox; otherwise ``None`` (in-memory-only, as before).
              Any import failure degrades to ``None`` — a Postgres-less environment
              must never break registry resolution.
        """
        try:
            from . import dynamic_store  # noqa: PLC0415

            if dynamic_store._store_active():
                return dynamic_store
        except Exception:  # pragma: no cover - defensive: never break disk resolution
            logger.debug("dynamic manifest store unavailable", exc_info=True)
        return None

    def _drop_tombstoned(self, merged: dict[str, AgentManifest]) -> None:
        """Remove any dynamic id this worker just ``unregister()``'d, in place.

        Preconditions:
            * ``merged`` maps id -> manifest (as built by :meth:`_merged_manifests` /
              :meth:`manifests_with_id_prefix`).
        Postconditions:
            * Keeps listings consistent with :meth:`get` during the tombstone
              window: a dynamic id this worker recently unregistered is dropped
              even if the store's scan still has a stale row (its best-effort
              delete may have failed) or this worker's own local copy would
              otherwise have resurfaced it.
        """
        for agent_id in list(merged):
            if agent_id not in self._static_ids and self._is_tombstoned(agent_id):
                del merged[agent_id]

    def _merged_manifests(self) -> list[AgentManifest]:
        """All manifests visible to this worker: cross-worker dynamic + local static.

        Overlays the dynamic Postgres rows with this process's local view: a static
        (disk) id always wins on collision, and a dynamic id missing from the store
        falls back to its local copy **only when unconfirmed** — the same
        read-your-writes guarantee :meth:`get` makes, so a manifest whose
        ``register()`` write-through failed (or hasn't propagated yet) is not
        invisible in the catalog, while a *confirmed* id absent from the store (i.e.
        deleted on another worker) is correctly dropped rather than resurrected.
        Degrades to the local ``_by_id`` view entirely on any store error, so the
        catalog never breaks when Postgres is down.

        Consistency note: ``store.all()`` runs *before* the lock is taken (a slow
        Postgres scan must not serialize the whole registry). A dynamic id
        ``register()``'d on this worker in the window between that scan and the lock
        can therefore be briefly absent from this one listing — its confirmed
        write-through means it isn't in ``_unconfirmed``, so the local-fallback merge
        below skips it, and the scan predated its store row. This is an accepted
        eventual-consistency window for the *catalog listing only*: the next scan
        (≤ the dynamic store's short ``all()`` cache TTL) includes it, and a point
        :meth:`get` / invoke of that id resolves it immediately from the store. The
        window can't be closed by a re-check under the lock, because a confirmed id
        missing from the scan is genuinely ambiguous — just-registered (include) vs.
        deleted-elsewhere (exclude) — and only a per-id store read distinguishes
        them, which would defeat the point of a single bulk listing.
        """
        store = self._dynamic_store()
        if store is None:
            with self._lock:
                return list(self._by_id.values())
        try:
            merged: dict[str, AgentManifest] = {m.id: m for m in store.all()}
        except Exception:
            logger.warning("dynamic store all() failed; serving local registry", exc_info=True)
            with self._lock:
                return list(self._by_id.values())
        with self._lock:
            for agent_id, local in self._by_id.items():
                if agent_id in self._static_ids:
                    merged[agent_id] = local  # static disk id always wins
                elif agent_id not in merged and agent_id in self._unconfirmed:
                    merged[agent_id] = local  # unconfirmed local write-through only
            self._drop_tombstoned(merged)
        return list(merged.values())

    def all(self) -> list[AgentManifest]:
        return self._merged_manifests()

    def manifests_with_id_prefix(
        self, prefix: str, *, require_store: bool = False
    ) -> list[AgentManifest]:
        """Return registered manifests whose ``id`` starts with ``prefix``.

        Materializes only the matching subset in one pass, unlike :meth:`all`, which
        copies the whole registry before the caller filters — useful for a caller that
        wants just one namespace's entries (e.g. a single team's generated wrappers)
        and runs the scan while holding a lock. When a dynamic store is active the
        scan spans **all workers'** dynamic entries (not just this process's), so a
        roster-replacement cleanup drops stale generated agents everywhere.

        Preconditions: ``prefix`` is a string.
        Postconditions: returns every registered manifest ``m`` with
            ``m.id.startswith(prefix)`` (empty list when none match); read-only.
            Carries the same brief scan-then-lock eventual-consistency window as
            :meth:`_merged_manifests` (a dynamic id registered between the store
            prefix scan and the lock may be momentarily absent); see that method's
            consistency note. Default store-scan failure degrades to this process's
            local subset. When ``require_store`` is True and a dynamic store is
            active, a scan failure propagates instead — fail-closed callers (roster
            replace) must not omit another worker's stale ids.
        """
        with self._lock:
            local = [m for m in self._by_id.values() if m.id.startswith(prefix)]
        store = self._dynamic_store()
        if store is None:
            return local
        try:
            dynamic = store.manifests_with_prefix(prefix)
        except Exception:
            if require_store:
                raise
            logger.warning(
                "dynamic store prefix scan failed for %r; serving local", prefix, exc_info=True
            )
            return local
        merged = {m.id: m for m in dynamic}
        # Static (disk) ids always win; a dynamic id missing from the store's scan
        # falls back to its local copy ONLY when unconfirmed — same read-your-writes
        # guarantee as get(), so a write-through failure doesn't hide the entry from
        # this scan (used by the generated-roster stale-cleanup pass), while a
        # confirmed id deleted on another worker is dropped rather than resurrected.
        with self._lock:
            for m in local:
                if m.id in self._static_ids:
                    merged[m.id] = m
                elif m.id not in merged and m.id in self._unconfirmed:
                    merged[m.id] = m
            self._drop_tombstoned(merged)
        return list(merged.values())

    def get(self, agent_id: str) -> AgentManifest | None:
        """Resolve a manifest by id, consulting the dynamic store for non-static ids.

        Preconditions:
            * ``agent_id`` is a string.
        Postconditions:
            * Static/disk ids resolve from the in-memory map with zero Postgres
              cost (the built-in-agent invoke hot path).
            * For a dynamic id with the store active, returns the Postgres row when
              present. On an authoritative **miss** (the store has no row) the local
              ``_by_id`` copy is returned **only if the id is unconfirmed** (its
              write-through to Postgres failed / never happened — see
              :attr:`_unconfirmed`); this is the **read-your-writes for a first
              registration** guarantee, so a brand-new dynamic id whose upsert failed
              still resolves on the worker that registered it. For a **confirmed** id
              a store miss means it was ``unregister()``'d on another worker, so this
              returns ``None`` rather than resurrecting the stale local copy — cross-
              worker deletes are seen immediately, not merely on restart/roster-resync.
              Updating an existing *confirmed* id whose upsert fails still returns the
              store's older confirmed row (a present store row always wins — the
              unconfirmed local update is not assumed more correct than the last
              confirmed one). A store *error* (as opposed to a miss) degrades to the
              local copy, since the true state is unknown.
            * Symmetrically, an id this worker itself ``unregister()``'d within the
              last :attr:`_TOMBSTONE_TTL_S` never resolves here even on a stale
              Postgres hit (its best-effort delete may have failed) — see
              :meth:`_is_tombstoned`.
            * Returns ``None`` iff no static, dynamic-store, or local entry exists.
        """
        with self._lock:
            if agent_id in self._static_ids:
                return self._by_id.get(agent_id)
            if self._is_tombstoned(agent_id):
                return None
            local_fallback = self._by_id.get(agent_id)
        store = self._dynamic_store()
        if store is None:
            return local_fallback
        try:
            found = store.get(agent_id)
        except Exception:
            # Store *error* (not a miss): true state unknown → degrade to the local
            # copy rather than spuriously 404 a live agent.
            logger.warning(
                "dynamic store get failed for %s; using local registry", agent_id, exc_info=True
            )
            return local_fallback
        if found is not None:
            return found
        # Store *miss* (authoritative absence). Fall back to the local copy ONLY for
        # an unconfirmed write-through (read-your-writes: the store never accepted
        # our row). For a confirmed id, a miss means it was deleted on another
        # worker — return None instead of resurrecting the stale local copy.
        #
        # Re-read the local state under the lock rather than trusting the snapshot
        # taken before the (unlocked) store round trip: a concurrent register()/
        # unregister() during the round trip could have overwritten the manifest,
        # confirmed a previously-unconfirmed write, or tombstoned the id. Re-checking
        # here makes the read-your-writes decision reflect the latest state instead
        # of a stale ``unconfirmed``/``local_fallback`` pair. Cheap: this runs only
        # on the cold store-miss path (a store hit returned above).
        with self._lock:
            if self._is_tombstoned(agent_id):
                return None
            return self._by_id.get(agent_id) if agent_id in self._unconfirmed else None

    def register(
        self,
        manifest: AgentManifest,
        source_path: Path | None = None,
        *,
        require_persist: bool = False,
    ) -> None:
        """Install a manifest into the live registry (for dynamically generated agents).

        Disk discovery (:meth:`load`) is the norm; teams that *generate* agents at
        runtime register them here so the Agent Console catalog and the invoke
        route (``get_registry().get(id)``) resolve them without a YAML file.

        Preconditions:
            * ``manifest.id`` is non-empty.
        Postconditions:
            * On success, ``get(manifest.id)`` returns ``manifest`` (re-registering
              the same id overwrites the prior entry). Always updates this process's
              in-memory view; when a dynamic store is active and ``manifest.id`` is
              not a static/disk id, it is also persisted to Postgres so other
              workers and the per-invoke sandbox resolve it.
            * Default write-through is best-effort — a Postgres error is logged,
              never raised, and the local entry remains (``_unconfirmed``) so this
              worker still sees read-your-writes.
            * When ``require_persist`` is True and a dynamic store is active, a
              Postgres upsert failure restores the prior local state (if any) and
              re-raises so fail-closed callers (e.g. generated roster chat-save)
              can roll back their DB write. When no store is active, local-only
              registration still succeeds — there is nothing to persist.
        """
        assert manifest.id, "register: manifest.id must be non-empty"
        with self._lock:
            prior_manifest = self._by_id.get(manifest.id)
            prior_source = self._source_paths.get(manifest.id)
            prior_tombstone = self._tombstones.get(manifest.id)
            was_unconfirmed = manifest.id in self._unconfirmed
            self._by_id[manifest.id] = manifest
            if source_path is not None:
                self._source_paths[manifest.id] = source_path
            # A fresh registration supersedes any earlier local unregister() of
            # this id. Cleared under the same lock as the _by_id write above so a
            # concurrent get() never observes the torn state where the manifest is
            # already installed but the stale tombstone still masks it (or vice
            # versa) — see get()'s matching lock scope.
            self._tombstones.pop(manifest.id, None)
        if manifest.id not in self._static_ids:
            store = self._dynamic_store()
            if store is None:
                # No active store to confirm against (Postgres off / in sandbox):
                # the local copy is authoritative, so a store miss must never drop
                # it. Treat as unconfirmed. ``require_persist`` is a no-op here —
                # there is no store that other workers could miss.
                with self._lock:
                    self._unconfirmed.add(manifest.id)
                return
            try:
                store.upsert(manifest)
            except Exception:
                if require_persist:
                    # Undo the in-memory install so fail-closed callers do not leave
                    # a half-registered local entry when their transaction rolls back.
                    # Only roll back state this call still owns: the lock was released
                    # during the store upsert, so a concurrent register/unregister of
                    # the same id must not be clobbered by restoring our snapshots.
                    with self._lock:
                        if self._by_id.get(manifest.id) is manifest:
                            if prior_manifest is None:
                                self._by_id.pop(manifest.id, None)
                                self._source_paths.pop(manifest.id, None)
                                self._unconfirmed.discard(manifest.id)
                            else:
                                self._by_id[manifest.id] = prior_manifest
                                if prior_source is not None:
                                    self._source_paths[manifest.id] = prior_source
                                elif source_path is not None:
                                    self._source_paths.pop(manifest.id, None)
                                if was_unconfirmed:
                                    self._unconfirmed.add(manifest.id)
                                else:
                                    self._unconfirmed.discard(manifest.id)
                        if (
                            prior_tombstone is not None
                            and manifest.id not in self._tombstones
                            and manifest.id not in self._by_id
                        ):
                            self._tombstones[manifest.id] = prior_tombstone
                            self._tombstones.move_to_end(manifest.id)
                    raise
                logger.warning(
                    "dynamic store upsert failed for %s; registered locally only",
                    manifest.id,
                    exc_info=True,
                )
                # Write-through failed → the store has no (new) row for this id, so
                # get() must keep serving the local copy on a miss (read-your-writes).
                with self._lock:
                    self._unconfirmed.add(manifest.id)
            else:
                # Confirmed in Postgres → the store is now authoritative for this id.
                # A future store miss means an authoritative delete, not our failure.
                with self._lock:
                    self._unconfirmed.discard(manifest.id)

    def unregister(self, agent_id: str) -> bool:
        """Remove a (dynamically registered) manifest from the live registry.

        Used by generators that replace a team's roster to drop entries for
        removed/renamed agents. A no-op for a *static* (disk-loaded) id — this
        method is only for dynamically-registered manifests, and a static id must
        remain resolvable for the process's lifetime regardless of caller error.

        Postconditions:
            * ``get(agent_id)`` returns ``None`` afterwards on this worker for at
              least :attr:`_TOMBSTONE_TTL_S` (see :meth:`_is_tombstoned`) — this
              holds even if the best-effort Postgres delete below fails, so a
              transient error can never resurrect a just-removed id on the worker
              that removed it. Removes the entry from this process's in-memory view
              and, when a dynamic store is active for a non-static id, from Postgres
              too (best-effort). Returns ``True`` when a local entry was present and
              removed, ``False`` otherwise (including when ``agent_id`` names a
              static id, which this method never removes) — the cross-worker
              Postgres delete is issued regardless (it's a no-op when the row is
              absent).
        """
        with self._lock:
            if agent_id in self._static_ids:
                logger.warning("Refusing to unregister static agent id %r; ignoring", agent_id)
                return False
            self._source_paths.pop(agent_id, None)
            removed = self._by_id.pop(agent_id, None) is not None
            self._unconfirmed.discard(agent_id)
            self._tombstones[agent_id] = time.monotonic()
            self._tombstones.move_to_end(agent_id)
            while len(self._tombstones) > self._TOMBSTONE_MAX_ENTRIES:
                self._tombstones.popitem(last=False)  # evict the oldest stamp
        store = self._dynamic_store()
        if store is not None:
            try:
                store.delete(agent_id)
            except Exception:
                logger.warning(
                    "dynamic store delete failed for %s; removed locally only",
                    agent_id,
                    exc_info=True,
                )
        return removed

    def replace_dynamic_manifests(
        self,
        upserts: Sequence[AgentManifest],
        delete_ids: Sequence[str],
        *,
        conn: Any | None = None,
    ) -> None:
        """Atomically install ``upserts`` and drop ``delete_ids`` for dynamic agents.

        Generated-roster replacement uses this so the shared store and this
        process's in-memory view stay aligned with the roster DB transaction:
        either the whole replacement lands, or nothing does.

        Preconditions:
            * Every upsert has a non-empty ``id`` that is not a static/disk id.
            * No ``delete_ids`` entry names a static/disk id.
            * ``upserts`` ids and ``delete_ids`` are disjoint.
        Postconditions:
            * When a dynamic store is active and ``conn`` is ``None``, all upserts
              and deletes commit in **one** dedicated Postgres transaction before
              local memory is updated. A store failure leaves both store and local
              registry unchanged and propagates.
            * When ``conn`` is provided (chat-save), store statements join that
              open transaction so a later roster-commit failure rolls both back
              together. Local memory is still updated after the statements succeed
              on ``conn`` (process-local residual if the outer commit then fails —
              other workers never see the uncommitted store rows).
            * When no store is active, only this process's in-memory view is
              updated (local-authoritative, same as :meth:`register` with no
              store). After success, each upserted id resolves via :meth:`get`
              and each deleted id does not (subject to tombstone / store semantics).
        """
        upsert_list = list(upserts)
        delete_list = list(delete_ids)
        for manifest in upsert_list:
            if not manifest.id:
                raise ValueError("replace_dynamic_manifests: manifest.id must be non-empty")
            if manifest.id in self._static_ids:
                raise ValueError(f"replace_dynamic_manifests: refusing static id {manifest.id!r}")
        for agent_id in delete_list:
            if agent_id in self._static_ids:
                raise ValueError(
                    f"replace_dynamic_manifests: refusing to delete static id {agent_id!r}"
                )
        upsert_ids = {m.id for m in upsert_list}
        overlap = upsert_ids & set(delete_list)
        if overlap:
            raise ValueError(
                "replace_dynamic_manifests: upserts and delete_ids must be disjoint; "
                f"overlap={sorted(overlap)!r}"
            )

        store = self._dynamic_store()
        if store is not None:
            # Store first: on failure local state is untouched so fail-closed
            # callers can roll back their roster write against an unchanged catalog.
            store.replace_manifests(upsert_list, delete_list, conn=conn)

        with self._lock:
            for manifest in upsert_list:
                self._by_id[manifest.id] = manifest
                self._tombstones.pop(manifest.id, None)
                if store is None:
                    self._unconfirmed.add(manifest.id)
                else:
                    self._unconfirmed.discard(manifest.id)
            for agent_id in delete_list:
                self._source_paths.pop(agent_id, None)
                self._by_id.pop(agent_id, None)
                self._unconfirmed.discard(agent_id)
                self._tombstones[agent_id] = time.monotonic()
                self._tombstones.move_to_end(agent_id)
                while len(self._tombstones) > self._TOMBSTONE_MAX_ENTRIES:
                    self._tombstones.popitem(last=False)

    def search(
        self,
        *,
        team: str | None = None,
        tag: str | None = None,
        q: str | None = None,
    ) -> list[AgentSummary]:
        needle = q.strip().lower() if q else None
        results: list[AgentSummary] = []
        for m in self._merged_manifests():
            if team and m.team != team:
                continue
            if tag and tag not in m.tags:
                continue
            if needle and not self._matches_query(m, needle):
                continue
            results.append(self._summarize(m))
        results.sort(key=lambda s: (s.team, s.id))
        return results

    def teams(self) -> list[TeamGroup]:
        counts: dict[str, int] = {}
        tags: dict[str, set[str]] = {}
        for m in self._merged_manifests():
            counts[m.team] = counts.get(m.team, 0) + 1
            tags.setdefault(m.team, set()).update(m.tags)
        groups = [
            TeamGroup(
                team=team_key,
                display_name=self._team_display_names.get(team_key)
                or team_key.replace("_", " ").title(),
                agent_count=count,
                tags=sorted(tags.get(team_key, set())),
            )
            for team_key, count in counts.items()
        ]
        groups.sort(key=lambda g: g.display_name.lower())
        return groups

    def list_samples(self, agent_id: str) -> list[str]:
        """Return the stem names of golden samples for ``agent_id``. Empty list if none."""
        directory = self._samples_dir(agent_id)
        if directory is None or not directory.is_dir():
            return []
        return sorted(p.stem for p in directory.glob("*.json"))

    def get_sample(self, agent_id: str, name: str) -> dict[str, Any] | None:
        """Load a golden sample by stem name. ``None`` if unknown."""
        directory = self._samples_dir(agent_id)
        if directory is None:
            return None
        path = directory / f"{name}.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read sample %s for %s: %s", name, agent_id, exc)
            return None

    def _samples_dir(self, agent_id: str) -> Path | None:
        """Locate the samples dir for ``agent_id`` without assuming a team-dir naming convention.

        Derives it from the manifest's actual on-disk path:
        ``<team_dir>/agent_console/manifests/<file>.yaml``
        → ``<team_dir>/agent_console/samples/<agent_id>/``.

        Falls back to ``<agents_root>/<manifest.team>/...`` when the source
        path is unknown (e.g. tests that instantiate ``AgentRegistry`` manually).
        """
        with self._lock:
            source = self._source_paths.get(agent_id)
        if source is not None:
            team_console_dir = source.parent.parent  # manifests/ → agent_console/
            return team_console_dir / "samples" / agent_id
        manifest = self.get(agent_id)
        if manifest is None:
            return None
        return _agents_root() / manifest.team / "agent_console" / "samples" / agent_id

    def detail(self, agent_id: str, *, repo_root: Path | None = None) -> AgentDetail | None:
        manifest = self.get(agent_id)
        if manifest is None:
            return None
        anatomy = None
        if manifest.source.anatomy_ref:
            anatomy = self._read_anatomy(manifest.source.anatomy_ref, repo_root=repo_root)
        return AgentDetail(manifest=manifest, anatomy_markdown=anatomy)

    # ---------------------------------------------------------------
    # Internals
    # ---------------------------------------------------------------

    @staticmethod
    def _matches_query(m: AgentManifest, needle: str) -> bool:
        haystack = " ".join([m.id, m.name, m.summary, " ".join(m.tags), m.description or ""])
        return needle in haystack.lower()

    @staticmethod
    def _summarize(m: AgentManifest) -> AgentSummary:
        return AgentSummary(
            id=m.id,
            team=m.team,
            name=m.name,
            summary=m.summary,
            tags=list(m.tags),
            has_input_schema=bool(
                m.inputs and (m.inputs.schema_ref or m.inputs.inline_schema is not None)
            ),
            has_output_schema=bool(
                m.outputs and (m.outputs.schema_ref or m.outputs.inline_schema is not None)
            ),
            has_invoke=m.invoke is not None,
            has_sandbox=m.sandbox is not None,
            has_cognition=m.cognition is not None,
            has_knowledge_graph=bool(m.cognition and m.cognition.knowledge_graph.enabled),
        )

    @staticmethod
    def _read_anatomy(anatomy_ref: str, *, repo_root: Path | None) -> str | None:
        """Read an anatomy markdown file from disk if it exists.

        ``anatomy_ref`` is expected to be a repo-relative path
        (e.g. ``backend/agents/blogging/blog_planning_agent/ANATOMY.md``).
        """
        candidates: Iterable[Path]
        if repo_root is not None:
            candidates = [repo_root / anatomy_ref]
        else:
            # Walk every parent from this file up to the filesystem root, so we
            # survive shallow checkouts (e.g. ``/repo/backend/...``) where the
            # repo root is fewer than four levels above this module.
            here = Path(__file__).resolve()
            candidates = [parent / anatomy_ref for parent in here.parents]
        for path in candidates:
            try:
                if path.is_file():
                    return path.read_text(encoding="utf-8")
            except OSError:
                continue
        logger.debug("Anatomy file not found for ref %r", anatomy_ref)
        return None


@lru_cache(maxsize=1)
def get_registry() -> AgentRegistry:
    """Process-wide singleton. Call ``get_registry.cache_clear()`` to reload."""
    return AgentRegistry.load()
