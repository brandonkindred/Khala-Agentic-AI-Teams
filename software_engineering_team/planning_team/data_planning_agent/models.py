"""Models for the Data Planning agent."""

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from planning_team.planning_graph import PlanningGraph
from shared.models import ProductRequirements, SystemArchitecture


class DataPlanningInput(BaseModel):
    """Input for the Data Planning agent."""

    requirements: ProductRequirements
    architecture: Optional[SystemArchitecture] = None
    spec_content: str = ""
    project_overview: Optional[Dict[str, Any]] = None


class DataPlanningOutput(BaseModel):
    """Output from the Data Planning agent."""

    planning_graph: PlanningGraph
    summary: str = ""
