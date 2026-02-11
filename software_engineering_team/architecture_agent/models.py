"""Models for the Architecture Expert agent."""

from typing import List, Optional

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
        description="Preferred technologies (e.g. Python, Angular, Kubernetes)",
    )


class ArchitectureOutput(BaseModel):
    """Output from the Architecture Expert agent."""

    architecture: SystemArchitecture
    summary: str = ""
