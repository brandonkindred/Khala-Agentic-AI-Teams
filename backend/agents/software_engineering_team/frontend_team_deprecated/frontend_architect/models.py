"""Models for the Frontend Architect agent."""

from typing import Optional

from frontend_team_deprecated.models import DesignSystemOutput, UIDesignerOutput, UXDesignerOutput
from pydantic import BaseModel

from software_engineering_team.shared.models import SystemArchitecture


class FrontendArchitectInput(BaseModel):
    """Input for the Frontend Architect agent."""

    task_description: str
    task_id: str = ""
    spec_content: str = ""
    architecture: Optional[SystemArchitecture] = None
    user_story: str = ""
    ux_output: Optional[UXDesignerOutput] = None
    ui_output: Optional[UIDesignerOutput] = None
    design_system_output: Optional[DesignSystemOutput] = None
