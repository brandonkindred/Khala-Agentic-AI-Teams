from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class SecurityPlanningInput(BaseModel):
    spec_content: str = ""
    architecture_overview: str = ""
    data_lifecycle: str = ""
    requirements_title: str = ""
    plan_dir: Optional[Any] = None


class SecurityPlanningOutput(BaseModel):
    threat_model: str = ""
    security_checklist: str = ""
    data_classification: str = ""
    audit_requirements: str = ""
    summary: str = ""
