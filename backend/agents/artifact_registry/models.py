"""
Artifact manifest schema for the provenance registry.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class ArtifactManifest:
    """Structured metadata for an artifact produced by an agent team.

    Every artifact (code file, blog draft, architecture doc, research brief)
    should be registered with a manifest that captures its full provenance.
    """

    artifact_type: str  # "code_file", "blog_draft", "architecture_doc", "research_brief", etc.
    team: str  # Producing team
    job_id: str  # Parent job
    agent_key: str  # Producing agent (e.g. "blog_writer", "backend_expert")
    artifact_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: float = field(default_factory=time.time)

    # Content reference (file path, object store key, or inline for small artifacts)
    content_ref: str = ""  # Path or URI where the artifact content is stored
    content_hash: str = ""  # SHA-256 of artifact content (for dedup and integrity)

    # Lineage
    parent_artifacts: List[str] = field(default_factory=list)  # Artifact IDs this was derived from
    llm_call_ids: List[str] = field(default_factory=list)  # LLM call records that produced this

    # Team-specific metadata
    metadata: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def compute_content_hash(content: str | bytes) -> str:
        """Compute SHA-256 hash of artifact content."""
        if isinstance(content, str):
            content = content.encode("utf-8")
        return hashlib.sha256(content).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            "team": self.team,
            "job_id": self.job_id,
            "agent_key": self.agent_key,
            "created_at": self.created_at,
            "content_ref": self.content_ref,
            "content_hash": self.content_hash,
            "parent_artifacts": self.parent_artifacts,
            "llm_call_ids": self.llm_call_ids,
            "metadata": self.metadata,
        }
