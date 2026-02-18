"""Models for the Performance Planning agent."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from planning_team.planning_graph import PlanningGraph
from shared.models import ProductRequirements, SystemArchitecture


class PerformancePlanningInput(BaseModel):
    """Input for the Performance Planning agent."""

    requirements: ProductRequirements
    architecture: Optional[SystemArchitecture] = None
    spec_content: str = ""
    project_overview: Optional[Dict[str, Any]] = None
    existing_node_ids: List[str] = Field(default_factory=list)


class PerformancePlanningOutput(BaseModel):
    """Output from the Performance Planning agent."""

    planning_graph: PlanningGraph
    node_budgets: Dict[str, str] = Field(default_factory=dict)
    summary: str = ""
