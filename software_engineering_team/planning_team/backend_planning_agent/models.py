"""Models for the Backend Planning agent."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from planning_team.planning_graph import PlanningGraph
from shared.models import ProductRequirements, SystemArchitecture


class BackendPlanningInput(BaseModel):
    """Input for the Backend Planning agent."""

    requirements: ProductRequirements
    architecture: Optional[SystemArchitecture] = None
    spec_content: str = ""
    project_overview: Optional[Dict[str, Any]] = None
    codebase_analysis: Optional[str] = None
    spec_analysis: Optional[str] = None


class BackendPlanningOutput(BaseModel):
    """Output from the Backend Planning agent."""

    planning_graph: PlanningGraph
    summary: str = ""
