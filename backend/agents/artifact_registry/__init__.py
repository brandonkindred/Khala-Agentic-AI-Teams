"""
Artifact lineage and provenance registry.

Tracks all artifacts produced by agent teams with full provenance metadata:
which team, agent, job, and LLM calls produced each artifact. Enables
tracing from a bug in generated code back to the planning decision and
prompt that caused it.
"""

from .models import ArtifactManifest
from .store import ArtifactRegistry

__all__ = ["ArtifactManifest", "ArtifactRegistry"]
