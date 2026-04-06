"""
In-memory artifact registry with query support.

For production, this should be backed by Postgres. The in-memory store
provides the API surface and can be used for development and testing.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

from .models import ArtifactManifest

logger = logging.getLogger(__name__)


class ArtifactRegistry:
    """Thread-safe in-memory artifact registry.

    Stores artifact manifests and provides query methods for lineage tracing.
    """

    def __init__(self) -> None:
        self._artifacts: Dict[str, ArtifactManifest] = {}  # artifact_id -> manifest
        self._by_job: Dict[str, List[str]] = {}  # job_id -> [artifact_id, ...]
        self._by_team: Dict[str, List[str]] = {}  # team -> [artifact_id, ...]
        self._lock = threading.Lock()

    def register(self, manifest: ArtifactManifest) -> ArtifactManifest:
        """Register an artifact manifest. Returns the manifest with ID assigned."""
        with self._lock:
            self._artifacts[manifest.artifact_id] = manifest
            self._by_job.setdefault(manifest.job_id, []).append(manifest.artifact_id)
            self._by_team.setdefault(manifest.team, []).append(manifest.artifact_id)
        logger.info(
            "Registered artifact: %s (type=%s, team=%s, job=%s)",
            manifest.artifact_id,
            manifest.artifact_type,
            manifest.team,
            manifest.job_id,
        )
        return manifest

    def get(self, artifact_id: str) -> Optional[ArtifactManifest]:
        """Get an artifact manifest by ID."""
        with self._lock:
            return self._artifacts.get(artifact_id)

    def get_by_job(self, job_id: str) -> List[Dict[str, Any]]:
        """Get all artifacts for a job."""
        with self._lock:
            ids = self._by_job.get(job_id, [])
            return [self._artifacts[aid].to_dict() for aid in ids if aid in self._artifacts]

    def get_by_team(self, team: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Get recent artifacts for a team."""
        with self._lock:
            ids = self._by_team.get(team, [])
            return [self._artifacts[aid].to_dict() for aid in ids[-limit:] if aid in self._artifacts]

    def get_lineage(self, artifact_id: str) -> List[Dict[str, Any]]:
        """Trace the full lineage of an artifact (all ancestors)."""
        visited = set()
        lineage = []

        def _trace(aid: str) -> None:
            if aid in visited:
                return
            visited.add(aid)
            manifest = self._artifacts.get(aid)
            if not manifest:
                return
            lineage.append(manifest.to_dict())
            for parent_id in manifest.parent_artifacts:
                _trace(parent_id)

        with self._lock:
            _trace(artifact_id)
        return lineage

    def search(
        self,
        *,
        artifact_type: Optional[str] = None,
        team: Optional[str] = None,
        agent_key: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Search artifacts by type, team, or agent."""
        with self._lock:
            results = list(self._artifacts.values())
        if artifact_type:
            results = [a for a in results if a.artifact_type == artifact_type]
        if team:
            results = [a for a in results if a.team == team]
        if agent_key:
            results = [a for a in results if a.agent_key == agent_key]
        return [a.to_dict() for a in results[-limit:]]


# Module-level singleton
_registry: Optional[ArtifactRegistry] = None
_registry_lock = threading.Lock()


def get_registry() -> ArtifactRegistry:
    """Return the global artifact registry singleton."""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = ArtifactRegistry()
    return _registry
