"""Models for the Performance Engineer agent."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from software_engineering_team.shared.models import SystemArchitecture


class PerformanceEngineerInput(BaseModel):
    """Input for the Performance Engineer agent."""

    code: str
    task_description: str = ""
    task_id: str = ""
    build_output: str = ""  # ng build output, bundle size info if available
    architecture: Optional[SystemArchitecture] = None


class PerformanceEngineerOutput(BaseModel):
    """Output from the Performance Engineer agent."""

    issues: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Performance issues as code_review-style for implementation",
    )
    approved: bool = Field(
        default=True,
        description="True when performance budgets and practices are acceptable",
    )
    performance_budgets: str = Field(
        default="",
        description="Recommended or enforced budgets: bundle size, route chunks, LCP/INP targets",
    )
    code_splitting_plan: str = Field(
        default="",
        description="Code splitting and lazy loading recommendations",
    )
    caching_strategy: str = Field(
        default="",
        description="Caching strategy recommendations",
    )
    summary: str = ""
