"""Models for the UI / Visual Designer agent."""

from typing import Optional

from pydantic import BaseModel, Field

from frontend_team_deprecated.models import UXDesignerOutput
from software_engineering_team.shared.models import SystemArchitecture


class UIDesignerInput(BaseModel):
    """Input for the UI / Visual Designer agent."""

    task_description: str
    task_id: str = ""
    spec_content: str = ""
    architecture: Optional[SystemArchitecture] = None
    user_story: str = ""
    ux_output: Optional[UXDesignerOutput] = None
