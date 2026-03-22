"""
Unified Artifact Store for the Digital Accessibility Audit Team.

Provides centralized storage semantics for:
- Evidence packs (screenshots, videos, DOM snapshots, a11y trees)
- Monitoring baselines and snapshots
- Training bundles and modules
- Exported reports and backlogs

Uses S3-style references with hashing and retention policies.
"""

import hashlib
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ArtifactType(str, Enum):
    """Types of artifacts stored."""

    EVIDENCE_PACK = "evidence_pack"
    SCREENSHOT = "screenshot"
    VIDEO = "video"
    DOM_SNAPSHOT = "dom_snapshot"
    A11Y_TREE = "a11y_tree"
    SCAN_RESULT = "scan_result"
    BASELINE = "baseline"
    MONITORING_RUN = "monitoring_run"
    TRAINING_MODULE = "training_module"
    TRAINING_BUNDLE = "training_bundle"
    REPORT = "report"
    BACKLOG_EXPORT = "backlog_export"
    CONTRACT = "contract"


class RetentionPolicy(str, Enum):
    """Artifact retention policies."""

    EPHEMERAL = "ephemeral"  # Delete after 24 hours
    SHORT = "short"  # Keep for 30 days
    STANDARD = "standard"  # Keep for 1 year
    LONG = "long"  # Keep for 3 years
    PERMANENT = "permanent"  # Keep forever


RETENTION_DAYS = {
    RetentionPolicy.EPHEMERAL: 1,
    RetentionPolicy.SHORT: 30,
    RetentionPolicy.STANDARD: 365,
    RetentionPolicy.LONG: 365 * 3,
    RetentionPolicy.PERMANENT: None,
}


class ArtifactMetadata(BaseModel):
    """Metadata for stored artifacts."""

    artifact_ref: str = Field(..., description="Unique artifact reference")
    artifact_type: ArtifactType
    audit_id: str = Field(default="", description="Associated audit ID")
    content_hash: str = Field(default="", description="SHA-256 hash of content")
    size_bytes: int = Field(default=0)
    mime_type: str = Field(default="application/octet-stream")
    retention_policy: RetentionPolicy = Field(default=RetentionPolicy.STANDARD)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: Optional[datetime] = Field(default=None)
    tags: Dict[str, str] = Field(default_factory=dict)


class StorageBackend(ABC):
    """Abstract base for storage backends."""

    @abstractmethod
    async def store(
        self,
        ref: str,
        content: bytes,
        metadata: ArtifactMetadata,
    ) -> str:
        """Store content and return the storage reference."""
        pass

    @abstractmethod
    async def retrieve(self, ref: str) -> Optional[bytes]:
        """Retrieve content by reference."""
        pass

    @abstractmethod
    async def get_metadata(self, ref: str) -> Optional[ArtifactMetadata]:
        """Get metadata for an artifact."""
        pass

    @abstractmethod
    async def delete(self, ref: str) -> bool:
        """Delete an artifact."""
        pass

    @abstractmethod
    async def list_artifacts(
        self,
        audit_id: str = None,
        artifact_type: ArtifactType = None,
    ) -> List[ArtifactMetadata]:
        """List artifacts with optional filters."""
        pass


class FileSystemBackend(StorageBackend):
    """
    File system storage backend.

    Stores artifacts in a local directory with JSON metadata files.
    """

    def __init__(self, base_path: str = "/tmp/accessibility_audit_artifacts"):
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)
        self._metadata_cache: Dict[str, ArtifactMetadata] = {}

    def _get_artifact_path(self, ref: str) -> Path:
        """Get file path for an artifact."""
        return self.base_path / f"{ref}.bin"

    def _get_metadata_path(self, ref: str) -> Path:
        """Get metadata file path for an artifact."""
        return self.base_path / f"{ref}.meta.json"

    async def store(
        self,
        ref: str,
        content: bytes,
        metadata: ArtifactMetadata,
    ) -> str:
        """Store content to file system."""
        artifact_path = self._get_artifact_path(ref)
        metadata_path = self._get_metadata_path(ref)

        # Compute hash
        content_hash = hashlib.sha256(content).hexdigest()
        metadata.content_hash = content_hash
        metadata.size_bytes = len(content)

        # Calculate expiration
        if metadata.retention_policy != RetentionPolicy.PERMANENT:
            retention_days = RETENTION_DAYS.get(metadata.retention_policy, 365)
            metadata.expires_at = datetime.utcnow() + timedelta(days=retention_days)

        # Write content
        artifact_path.write_bytes(content)

        # Write metadata
        metadata_path.write_text(metadata.model_dump_json())

        # Cache metadata
        self._metadata_cache[ref] = metadata

        return ref

    async def retrieve(self, ref: str) -> Optional[bytes]:
        """Retrieve content from file system."""
        artifact_path = self._get_artifact_path(ref)

        if not artifact_path.exists():
            return None

        return artifact_path.read_bytes()

    async def get_metadata(self, ref: str) -> Optional[ArtifactMetadata]:
        """Get metadata for an artifact."""
        if ref in self._metadata_cache:
            return self._metadata_cache[ref]

        metadata_path = self._get_metadata_path(ref)

        if not metadata_path.exists():
            return None

        metadata = ArtifactMetadata.model_validate_json(metadata_path.read_text())
        self._metadata_cache[ref] = metadata
        return metadata

    async def delete(self, ref: str) -> bool:
        """Delete an artifact."""
        artifact_path = self._get_artifact_path(ref)
        metadata_path = self._get_metadata_path(ref)

        deleted = False

        if artifact_path.exists():
            artifact_path.unlink()
            deleted = True

        if metadata_path.exists():
            metadata_path.unlink()
            deleted = True

        if ref in self._metadata_cache:
            del self._metadata_cache[ref]

        return deleted

    async def list_artifacts(
        self,
        audit_id: str = None,
        artifact_type: ArtifactType = None,
    ) -> List[ArtifactMetadata]:
        """List artifacts with optional filters."""
        artifacts = []

        for meta_path in self.base_path.glob("*.meta.json"):
            try:
                metadata = ArtifactMetadata.model_validate_json(meta_path.read_text())

                if audit_id and metadata.audit_id != audit_id:
                    continue

                if artifact_type and metadata.artifact_type != artifact_type:
                    continue

                artifacts.append(metadata)
            except Exception:
                continue

        return sorted(artifacts, key=lambda a: a.created_at, reverse=True)


class ArtifactStore:
    """
    Unified artifact store for accessibility audit artifacts.

    Provides high-level API for storing and retrieving artifacts
    with automatic hashing, retention policies, and metadata.
    """

    def __init__(self, backend: StorageBackend = None):
        """
        Initialize the artifact store.

        Args:
            backend: Storage backend to use (defaults to FileSystemBackend)
        """
        self.backend = backend or FileSystemBackend()

    def _generate_ref(
        self,
        artifact_type: ArtifactType,
        audit_id: str = "",
        suffix: str = "",
    ) -> str:
        """Generate a unique artifact reference."""
        parts = [artifact_type.value]
        if audit_id:
            parts.append(audit_id)
        parts.append(uuid.uuid4().hex[:12])
        if suffix:
            parts.append(suffix)
        return "_".join(parts)

    async def store_evidence_pack(
        self,
        audit_id: str,
        finding_id: str,
        content: bytes,
        mime_type: str = "application/json",
    ) -> str:
        """Store an evidence pack."""
        ref = self._generate_ref(
            ArtifactType.EVIDENCE_PACK,
            audit_id,
            finding_id,
        )

        metadata = ArtifactMetadata(
            artifact_ref=ref,
            artifact_type=ArtifactType.EVIDENCE_PACK,
            audit_id=audit_id,
            mime_type=mime_type,
            retention_policy=RetentionPolicy.STANDARD,
            tags={"finding_id": finding_id},
        )

        return await self.backend.store(ref, content, metadata)

    async def store_screenshot(
        self,
        audit_id: str,
        content: bytes,
        description: str = "",
    ) -> str:
        """Store a screenshot."""
        ref = self._generate_ref(ArtifactType.SCREENSHOT, audit_id)

        metadata = ArtifactMetadata(
            artifact_ref=ref,
            artifact_type=ArtifactType.SCREENSHOT,
            audit_id=audit_id,
            mime_type="image/png",
            retention_policy=RetentionPolicy.STANDARD,
            tags={"description": description},
        )

        return await self.backend.store(ref, content, metadata)

    async def store_video(
        self,
        audit_id: str,
        content: bytes,
        description: str = "",
    ) -> str:
        """Store a video recording."""
        ref = self._generate_ref(ArtifactType.VIDEO, audit_id)

        metadata = ArtifactMetadata(
            artifact_ref=ref,
            artifact_type=ArtifactType.VIDEO,
            audit_id=audit_id,
            mime_type="video/webm",
            retention_policy=RetentionPolicy.SHORT,  # Videos are large
            tags={"description": description},
        )

        return await self.backend.store(ref, content, metadata)

    async def store_baseline(
        self,
        audit_id: str,
        content: bytes,
        env: str = "prod",
    ) -> str:
        """Store a monitoring baseline."""
        ref = self._generate_ref(ArtifactType.BASELINE, audit_id, env)

        metadata = ArtifactMetadata(
            artifact_ref=ref,
            artifact_type=ArtifactType.BASELINE,
            audit_id=audit_id,
            mime_type="application/json",
            retention_policy=RetentionPolicy.LONG,  # Baselines kept longer
            tags={"env": env},
        )

        return await self.backend.store(ref, content, metadata)

    async def store_training_bundle(
        self,
        audit_id: str,
        content: bytes,
    ) -> str:
        """Store a training bundle."""
        ref = self._generate_ref(ArtifactType.TRAINING_BUNDLE, audit_id)

        metadata = ArtifactMetadata(
            artifact_ref=ref,
            artifact_type=ArtifactType.TRAINING_BUNDLE,
            audit_id=audit_id,
            mime_type="application/json",
            retention_policy=RetentionPolicy.PERMANENT,  # Training content kept forever
        )

        return await self.backend.store(ref, content, metadata)

    async def store_report(
        self,
        audit_id: str,
        content: bytes,
        format: str = "json",
    ) -> str:
        """Store a final report."""
        ref = self._generate_ref(ArtifactType.REPORT, audit_id)

        mime_types = {
            "json": "application/json",
            "csv": "text/csv",
            "html": "text/html",
            "pdf": "application/pdf",
        }

        metadata = ArtifactMetadata(
            artifact_ref=ref,
            artifact_type=ArtifactType.REPORT,
            audit_id=audit_id,
            mime_type=mime_types.get(format, "application/octet-stream"),
            retention_policy=RetentionPolicy.PERMANENT,
            tags={"format": format},
        )

        return await self.backend.store(ref, content, metadata)

    async def retrieve(self, ref: str) -> Optional[bytes]:
        """Retrieve artifact content by reference."""
        return await self.backend.retrieve(ref)

    async def get_metadata(self, ref: str) -> Optional[ArtifactMetadata]:
        """Get artifact metadata by reference."""
        return await self.backend.get_metadata(ref)

    async def delete(self, ref: str) -> bool:
        """Delete an artifact."""
        return await self.backend.delete(ref)

    async def list_audit_artifacts(
        self,
        audit_id: str,
        artifact_type: ArtifactType = None,
    ) -> List[ArtifactMetadata]:
        """List all artifacts for an audit."""
        return await self.backend.list_artifacts(audit_id, artifact_type)

    async def cleanup_expired(self) -> int:
        """
        Clean up expired artifacts.

        Returns the number of artifacts deleted.
        """
        all_artifacts = await self.backend.list_artifacts()
        deleted = 0
        now = datetime.utcnow()

        for artifact in all_artifacts:
            if artifact.expires_at and artifact.expires_at < now:
                if await self.backend.delete(artifact.artifact_ref):
                    deleted += 1

        return deleted

    async def get_storage_stats(self, audit_id: str = None) -> Dict[str, Any]:
        """Get storage statistics."""
        artifacts = await self.backend.list_artifacts(audit_id)

        total_size = sum(a.size_bytes for a in artifacts)
        by_type: Dict[str, int] = {}

        for a in artifacts:
            type_name = a.artifact_type.value
            by_type[type_name] = by_type.get(type_name, 0) + 1

        return {
            "total_artifacts": len(artifacts),
            "total_size_bytes": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "by_type": by_type,
        }


# Global artifact store instance
_artifact_store: Optional[ArtifactStore] = None


def get_artifact_store() -> ArtifactStore:
    """Get the global artifact store instance."""
    global _artifact_store
    if _artifact_store is None:
        _artifact_store = ArtifactStore()
    return _artifact_store
