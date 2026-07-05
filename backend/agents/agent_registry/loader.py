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
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import yaml
from pydantic import ValidationError

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

    Falls back to title-casing the team key if the import fails (e.g. when the
    registry is used from a test harness that does not have unified_api on the
    path).
    """
    try:
        from unified_api.config import TEAM_CONFIGS  # type: ignore

        return {key: cfg.name for key, cfg in TEAM_CONFIGS.items()}
    except Exception:  # pragma: no cover — defensive
        logger.debug(
            "Could not import unified_api.config.TEAM_CONFIGS; using key-derived names",
            exc_info=True,
        )
        return {}


class AgentRegistry:
    """In-memory registry of agent manifests loaded from disk."""

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

    def _merged_manifests(self) -> list[AgentManifest]:
        """All manifests visible to this worker: cross-worker dynamic + local static.

        Overlays the dynamic Postgres rows with the static disk manifests (static
        wins on id collision). Degrades to the local ``_by_id`` view on any store
        error, so the catalog never breaks when Postgres is down.
        """
        store = self._dynamic_store()
        if store is None:
            return list(self._by_id.values())
        try:
            merged: dict[str, AgentManifest] = {m.id: m for m in store.all()}
        except Exception:
            logger.warning("dynamic store all() failed; serving local registry", exc_info=True)
            return list(self._by_id.values())
        for static_id in self._static_ids:
            local = self._by_id.get(static_id)
            if local is not None:
                merged[static_id] = local
        return list(merged.values())

    def all(self) -> list[AgentManifest]:
        return self._merged_manifests()

    def manifests_with_id_prefix(self, prefix: str) -> list[AgentManifest]:
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
        """
        local = [m for m in self._by_id.values() if m.id.startswith(prefix)]
        store = self._dynamic_store()
        if store is None:
            return local
        try:
            dynamic = store.manifests_with_prefix(prefix)
        except Exception:
            logger.warning(
                "dynamic store prefix scan failed for %r; serving local", prefix, exc_info=True
            )
            return local
        merged = {m.id: m for m in dynamic}
        # Static (disk) ids win; other local entries are already represented by the
        # authoritative dynamic rows.
        for m in local:
            if m.id in self._static_ids:
                merged[m.id] = m
        return list(merged.values())

    def get(self, agent_id: str) -> AgentManifest | None:
        # Disk/static ids resolve locally with zero Postgres cost — the built-in
        # agent invoke hot path.
        if agent_id in self._static_ids:
            return self._by_id.get(agent_id)
        store = self._dynamic_store()
        if store is None:
            return self._by_id.get(agent_id)
        try:
            # Postgres is authoritative for dynamic ids: a ``None`` here means the
            # manifest was deleted/never-persisted cross-worker, so we do NOT fall
            # back to a possibly-stale local copy. We degrade to local only on a
            # store *error*.
            return store.get(agent_id)
        except Exception:
            logger.warning(
                "dynamic store get failed for %s; using local registry", agent_id, exc_info=True
            )
            return self._by_id.get(agent_id)

    def register(self, manifest: AgentManifest, source_path: Path | None = None) -> None:
        """Install a manifest into the live registry (for dynamically generated agents).

        Disk discovery (:meth:`load`) is the norm; teams that *generate* agents at
        runtime register them here so the Agent Console catalog and the invoke
        route (``get_registry().get(id)``) resolve them without a YAML file.

        Preconditions:
            * ``manifest.id`` is non-empty.
        Postconditions:
            * ``get(manifest.id)`` returns ``manifest`` (re-registering the same id
              overwrites the prior entry). Always updates this process's in-memory
              view; when a dynamic store is active and ``manifest.id`` is not a
              static/disk id, it is also persisted to Postgres so other workers and
              the per-invoke sandbox resolve it. The write-through is best-effort —
              a Postgres error is logged, never raised (callers like the generated
              roster path hold a lock and swallow registry errors).
        """
        assert manifest.id, "register: manifest.id must be non-empty"
        self._by_id[manifest.id] = manifest
        if source_path is not None:
            self._source_paths[manifest.id] = source_path
        if manifest.id not in self._static_ids:
            store = self._dynamic_store()
            if store is not None:
                try:
                    store.upsert(manifest)
                except Exception:
                    logger.warning(
                        "dynamic store upsert failed for %s; registered locally only",
                        manifest.id,
                        exc_info=True,
                    )

    def unregister(self, agent_id: str) -> bool:
        """Remove a (dynamically registered) manifest from the live registry.

        Used by generators that replace a team's roster to drop entries for
        removed/renamed agents.

        Postconditions:
            * ``get(agent_id)`` returns ``None`` afterwards. Removes the entry from
              this process's in-memory view and, when a dynamic store is active for
              a non-static id, from Postgres too (best-effort). Returns ``True`` when
              a local entry was present and removed, ``False`` otherwise — the
              cross-worker Postgres delete is issued regardless (it's a no-op when
              the row is absent).
        """
        self._source_paths.pop(agent_id, None)
        removed = self._by_id.pop(agent_id, None) is not None
        if agent_id not in self._static_ids:
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
            has_input_schema=bool(m.inputs and (m.inputs.schema_ref or m.inputs.inline_schema)),
            has_output_schema=bool(m.outputs and (m.outputs.schema_ref or m.outputs.inline_schema)),
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
