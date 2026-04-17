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

    def all(self) -> list[AgentManifest]:
        return list(self._by_id.values())

    def get(self, agent_id: str) -> AgentManifest | None:
        return self._by_id.get(agent_id)

    def search(
        self,
        *,
        team: str | None = None,
        tag: str | None = None,
        q: str | None = None,
    ) -> list[AgentSummary]:
        needle = q.strip().lower() if q else None
        results: list[AgentSummary] = []
        for m in self._by_id.values():
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
        for m in self._by_id.values():
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
            has_input_schema=bool(m.inputs and m.inputs.schema_ref),
            has_output_schema=bool(m.outputs and m.outputs.schema_ref),
            has_invoke=m.invoke is not None,
            has_sandbox=m.sandbox is not None,
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
            # Walk up from this file looking for the path until we hit the repo root.
            here = Path(__file__).resolve()
            candidates = [p / anatomy_ref for p in [here.parents[i] for i in range(2, 6)]]
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
