from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, Field


class QaTestStrategyInput(BaseModel):
    spec_content: str = ""
    architecture_overview: str = ""
    acceptance_criteria: List[str] = Field(default_factory=list)
    requirement_ids: List[str] = Field(default_factory=list, description="REQ-001, REQ-002, ...")
    requirements_title: str = ""
    plan_dir: Optional[Any] = None


class QaTestStrategyOutput(BaseModel):
    test_pyramid: str = ""
    test_case_matrix: str = ""
    test_data_strategy: str = ""
    smoke_tests: str = ""
    summary: str = ""
