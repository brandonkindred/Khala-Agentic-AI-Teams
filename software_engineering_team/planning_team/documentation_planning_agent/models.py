"""Models for the Documentation Planning agent."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from planning_team.planning_graph import PlanningGraph
from shared.models import ProductRequirements, SystemArchitecture


class DocumentationPlanningInput(BaseModel):
    """Input for the Documentation Planning agent."""

    requirements: ProductRequirements
    architecture: Optional[SystemArchitecture] = None
    spec_content: str = ""
    project_overview: Optional[Dict[str, Any]] = None
    existing_task_ids: List[str] = Field(default_factory=list)


class DocumentationPlanningOutput(BaseModel):
    """Output from the Documentation Planning agent."""

    planning_graph: PlanningGraph
    summary: str = ""
