from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class InfrastructurePlanningInput(BaseModel):
    architecture_overview: str = ""
    tenancy_model: str = ""
    requirements_title: str = ""
    plan_dir: Optional[Any] = None


class InfrastructurePlanningOutput(BaseModel):
    cloud_diagram: str = ""
    environment_strategy: str = ""
    iam_model: str = ""
    cost_model: str = ""
    summary: str = ""
