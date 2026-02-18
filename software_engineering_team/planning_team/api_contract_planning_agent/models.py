"""Models for the API and Contract Design agent."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ApiContractPlanningInput(BaseModel):
    """Input for the API and Contract Design agent."""

    spec_content: str = Field(default="", description="Raw or validated spec content")
    architecture_overview: str = Field(default="", description="System architecture overview")
    requirements_title: str = Field(default="", description="Product/project title")
    acceptance_criteria: List[str] = Field(default_factory=list, description="Acceptance criteria from spec")
    plan_dir: Optional[Any] = Field(None, description="Path to plan folder for writing artifacts")


class ApiContractPlanningOutput(BaseModel):
    """Output from the API and Contract Design agent."""

    openapi_path: Optional[Path] = Field(None, description="Path to written OpenAPI spec")
    error_model_doc: str = Field(default="", description="Error model and standard response shapes")
    versioning_policy: str = Field(default="", description="Versioning and deprecation policy")
    contract_tests_plan: str = Field(default="", description="Consumer-driven contract tests plan")
    summary: str = Field(default="", description="Brief summary")
