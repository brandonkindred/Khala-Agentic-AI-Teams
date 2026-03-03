"""Models for the UX Designer agent."""

from typing import Optional

from pydantic import BaseModel, Field

from software_engineering_team.shared.models import SystemArchitecture


class UXDesignerInput(BaseModel):
    """Input for the UX Designer agent."""

    task_description: str
    task_id: str = ""
    spec_content: str = ""
    architecture: Optional[SystemArchitecture] = None
    user_story: str = ""
