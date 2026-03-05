"""Models for the Spec Analysis Merger agent."""

from typing import Any, Dict, List

from pydantic import BaseModel, Field


class SpecAnalysisMergerInput(BaseModel):
    """Input for the Spec Analysis Merger agent."""

    chunk_results: List[Dict[str, Any]] = Field(
        ...,
        description="List of chunk analysis JSONs from SpecChunkAnalyzer",
    )
    spec_outline: str = Field(
        default="",
        description="Optional short outline of spec sections (e.g. section titles, <2K chars)",
    )


class MergedSpecAnalysis(BaseModel):
    """Merged spec analysis output (same schema as SpecChunkAnalysis)."""

    data_entities: List[Dict[str, Any]] = Field(default_factory=list)
    api_endpoints: List[Dict[str, Any]] = Field(default_factory=list)
    ui_screens: List[Dict[str, Any]] = Field(default_factory=list)
    user_flows: List[Dict[str, Any]] = Field(default_factory=list)
    non_functional: List[Dict[str, Any]] = Field(default_factory=list)
    infrastructure: List[Dict[str, Any]] = Field(default_factory=list)
    integrations: List[Dict[str, Any]] = Field(default_factory=list)
    total_deliverable_count: int = Field(default=0)
    summary: str = Field(default="")
