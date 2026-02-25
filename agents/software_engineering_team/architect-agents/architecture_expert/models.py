"""Models for the Architecture Expert agent."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from shared.models import ProductRequirements, SystemArchitecture


class ArchitectureInput(BaseModel):
    """Input for the Architecture Expert agent."""

    requirements: ProductRequirements
    existing_architecture: Optional[str] = Field(
        None,
        description="Existing architecture to extend or modify",
    )
    technology_preferences: Optional[List[str]] = Field(
        None,
        description="Preferred technologies (e.g. Python, React, Angular, Vue, Kubernetes)",
    )
    project_overview: Optional[Dict[str, Any]] = Field(
        None,
        description="Project overview from ProjectPlanningAgent (goals, milestones, delivery strategy)",
    )
    features_and_functionality_doc: Optional[str] = Field(
        None,
        description="High-level features and functionality document from project planning; architecture must support these",
    )
    planning_feedback: Optional[List[str]] = Field(
        None,
        description="Feedback from planning alignment or conformance review; adjust architecture to address these",
    )


class ArchitectureOutput(BaseModel):
    """Output from the Architecture Expert agent."""

    architecture: SystemArchitecture
    summary: str = ""
