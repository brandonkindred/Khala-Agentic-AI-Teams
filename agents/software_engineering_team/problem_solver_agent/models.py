"""Models for the General Problem Solver specialist agent."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ProblemSolverInput(BaseModel):
    """Input payload for the general problem-solving specialist."""

    task_description: str = Field(default="", description="Current backend task context")
    bug_description: str = Field(description="Observed bug/error description to diagnose")
    specialty: str = Field(
        default="general",
        description="Specialty area related to this issue (build, qa, security, api, data, auth, devops)",
    )
    current_code_snapshot: str = Field(
        default="",
        description="Relevant code snapshot from repo used for diagnosis",
    )
    cycle: int = Field(default=1, description="1-based problem-solving cycle number")


class ProblemSolverOutput(BaseModel):
    """Output from the problem-solving specialist."""

    plan: str = Field(default="", description="Specialist plan for the current cycle")
    execution_steps: str = Field(default="", description="Concrete execution steps to apply")
    review_checks: str = Field(default="", description="Review checklist for validating the patch")
    testing_strategy: str = Field(default="", description="Targeted tests to run for this bug")
    fix_recommendation: str = Field(default="", description="Actionable patch recommendation")
