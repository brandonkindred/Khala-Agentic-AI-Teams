"""
Tool: evidence.create_pack

Create an evidence bundle with stable refs.
"""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
import uuid

from pydantic import BaseModel, Field

from ...models import EvidenceArtifact, EvidencePack, EnvironmentInfo, Surface


class ArtifactInput(BaseModel):
    """Input for a single artifact."""

    artifact_type: str = Field(
        ..., description="Type: screenshot, video, dom_snapshot, a11y_tree, log, audio"
    )
    content_ref: str = Field(default="", description="Reference to content or path")
    description: str = Field(default="")


class EnvironmentInput(BaseModel):
    """Environment information input."""

    surface: Literal["web", "ios", "android", "pdf"] = Field(default="web")
    browser_or_device: str = Field(default="")
    os_version: str = Field(default="")
    viewport_or_scale: str = Field(default="")
    assistive_tech: Optional[str] = Field(default=None)


class CreatePackInput(BaseModel):
    """Input for creating an evidence pack."""

    audit_id: str = Field(..., description="Audit identifier")
    finding_id: str = Field(..., description="Finding this evidence supports")
    artifacts: List[ArtifactInput] = Field(
        default_factory=list, description="Artifacts to include"
    )
    environment: EnvironmentInput = Field(default_factory=EnvironmentInput)
    notes: str = Field(default="")


class CreatePackOutput(BaseModel):
    """Output from creating an evidence pack."""

    evidence_pack: EvidencePack
    pack_ref: str
    artifact_count: int = Field(default=0)


async def create_pack(input_data: CreatePackInput) -> CreatePackOutput:
    """
    Create an evidence bundle for a finding.

    Evidence packs ensure findings are credible and reproducible by
    bundling all supporting artifacts with environment metadata.

    A finding without evidence is NOT reportable.

    Used by Reproduction & Evidence Engineer (REE).
    """
    # Generate pack reference
    pack_ref = f"evidence_{input_data.audit_id}_{input_data.finding_id}_{uuid.uuid4().hex[:8]}"

    # Convert artifacts
    artifacts = [
        EvidenceArtifact(
            artifact_type=a.artifact_type,
            ref=a.content_ref or f"{pack_ref}_{i}",
            description=a.description,
            timestamp=datetime.utcnow(),
        )
        for i, a in enumerate(input_data.artifacts)
    ]

    # Convert environment
    env = EnvironmentInfo(
        surface=Surface(input_data.environment.surface),
        browser_or_device=input_data.environment.browser_or_device,
        os_version=input_data.environment.os_version,
        viewport_or_scale=input_data.environment.viewport_or_scale,
        assistive_tech=input_data.environment.assistive_tech,
    )

    # Create the evidence pack
    evidence_pack = EvidencePack(
        pack_ref=pack_ref,
        finding_id=input_data.finding_id,
        artifacts=artifacts,
        environment=env,
        notes=input_data.notes,
    )

    return CreatePackOutput(
        evidence_pack=evidence_pack,
        pack_ref=pack_ref,
        artifact_count=len(artifacts),
    )
