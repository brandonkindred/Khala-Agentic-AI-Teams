from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class PerformancePlanningDocInput(BaseModel):
    spec_content: str = ""
    architecture_overview: str = ""
    requirements_title: str = ""
    plan_dir: Optional[Any] = None


class PerformancePlanningDocOutput(BaseModel):
    profiling_plan: str = ""
    load_tests: str = ""
    caching_cdn: str = ""
    summary: str = ""
