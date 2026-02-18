"""Models for the Test Planning agent."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from planning_team.planning_graph import PlanningGraph
from shared.models import ProductRequirements, SystemArchitecture


class TestPlanningInput(BaseModel):
    """Input for the Test Planning agent."""

    requirements: ProductRequirements
    architecture: Optional[SystemArchitecture] = None
    spec_content: str = ""
    project_overview: Optional[Dict[str, Any]] = None
    existing_task_ids: List[str] = Field(
        default_factory=list,
        description="Task IDs from backend/frontend plans to attach VERIFIES edges",
    )


class TestPlanningOutput(BaseModel):
    """Output from the Test Planning agent."""

    planning_graph: PlanningGraph
    summary: str = ""
