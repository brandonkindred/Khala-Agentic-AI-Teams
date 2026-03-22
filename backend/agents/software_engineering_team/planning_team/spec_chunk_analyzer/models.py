"""Models for the Spec Chunk Analyzer agent."""

from typing import Any, Dict, List

from pydantic import BaseModel, Field

# Max chars per spec chunk (enforced by caller; ~12K for ~3K tokens)
MAX_SPEC_CHUNK_CHARS = 12000


class SpecChunkAnalyzerInput(BaseModel):
    """Input for the Spec Chunk Analyzer agent."""

    spec_chunk: str = Field(
        ...,
        description="One chunk of the spec to analyze (max ~12K chars)",
    )
    chunk_index: int = Field(
        ...,
        description="1-based index of this chunk (e.g. 1 of 3)",
    )
    total_chunks: int = Field(
        ...,
        description="Total number of chunks",
    )
    requirements_header: Dict[str, Any] = Field(
        default_factory=dict,
        description="Product context: title, description, acceptance_criteria, constraints, priority",
    )


class SpecChunkAnalysis(BaseModel):
    """Output schema for one spec chunk analysis (matches Tech Lead spec analysis)."""

    data_entities: List[Dict[str, Any]] = Field(default_factory=list)
    api_endpoints: List[Dict[str, Any]] = Field(default_factory=list)
    ui_screens: List[Dict[str, Any]] = Field(default_factory=list)
    user_flows: List[Dict[str, Any]] = Field(default_factory=list)
    non_functional: List[Dict[str, Any]] = Field(default_factory=list)
    infrastructure: List[Dict[str, Any]] = Field(default_factory=list)
    integrations: List[Dict[str, Any]] = Field(default_factory=list)
    total_deliverable_count: int = Field(default=0)
    summary: str = Field(default="")
