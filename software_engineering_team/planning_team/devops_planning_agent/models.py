from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class DevOpsPlanningInput(BaseModel):
    architecture_overview: str = ""
    infrastructure_doc: str = ""
    requirements_title: str = ""
    plan_dir: Optional[Any] = None


class DevOpsPlanningOutput(BaseModel):
    ci_pipeline: str = ""
    cd_pipeline: str = ""
    iac_workflow: str = ""
    release_strategy: str = ""
    summary: str = ""
