"""Models for the Frontend Planning agent."""

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from planning_team.planning_graph import PlanningGraph
from shared.models import ProductRequirements, SystemArchitecture


class FrontendPlanningInput(BaseModel):
    """Input for the Frontend Planning agent."""

    requirements: ProductRequirements
    architecture: Optional[SystemArchitecture] = None
    spec_content: str = ""
    project_overview: Optional[Dict[str, Any]] = None
    codebase_analysis: Optional[str] = None
    spec_analysis: Optional[str] = None
    backend_planning_summary: Optional[str] = Field(
        None,
        description="Summary of backend plan for API contract alignment",
    )


class FrontendPlanningOutput(BaseModel):
    """Output from the Frontend Planning agent."""

    planning_graph: PlanningGraph
    summary: str = ""
