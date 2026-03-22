"""Models for the Design System & UI Engineering agent."""

from typing import Optional

from frontend_team_deprecated.models import UIDesignerOutput
from pydantic import BaseModel

from software_engineering_team.shared.models import SystemArchitecture


class DesignSystemInput(BaseModel):
    """Input for the Design System & UI Engineering agent."""

    task_description: str
    task_id: str = ""
    spec_content: str = ""
    architecture: Optional[SystemArchitecture] = None
    user_story: str = ""
    ui_output: Optional[UIDesignerOutput] = None
