"""Models for the Quality Gate Planning agent."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class QualityGatePlanningInput(BaseModel):
    """Input for the Quality Gate Planning agent."""

    task_ids: List[str] = Field(default_factory=list)
    project_overview: Optional[Dict[str, Any]] = None
    delivery_strategy: str = ""


class QualityGatePlanningOutput(BaseModel):
    """Output from the Quality Gate Planning agent."""

    node_quality_gates: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Map node_id to list of quality gate names (code_review, qa, security, accessibility, dbc)",
    )
    summary: str = ""
