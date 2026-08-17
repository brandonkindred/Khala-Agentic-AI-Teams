"""Models for AI Agent Development Team orchestration."""

from __future__ import annotations

from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from software_engineering_team.shared.v2_models import (
    ExecutionResult as _SharedExecutionResult,
)
from software_engineering_team.shared.v2_models import (
    Microtask as Microtask,
)
from software_engineering_team.shared.v2_models import (
    MicrotaskStatus as MicrotaskStatus,
)
from software_engineering_team.shared.v2_models import (
    PlanningResult,
)
from software_engineering_team.shared.v2_models import (
    ToolAgentInput as _SharedToolAgentInput,
)
from software_engineering_team.shared.v2_models import (
    ToolAgentPhaseInput as ToolAgentPhaseInput,
)


class Phase(str, Enum):
    INTAKE = "intake"
    PLANNING = "planning"
    EXECUTION = "execution"
    REVIEW = "review"
    PROBLEM_SOLVING = "problem_solving"
    DELIVER = "deliver"


class ToolAgentKind(str, Enum):
    GENERAL = "general"
    PROMPT_ENGINEERING = "prompt_engineering"
    MEMORY_RAG = "memory_rag"
    SAFETY_GOVERNANCE = "safety_governance"
    EVALUATION_HARNESS = "evaluation_harness"
    AGENT_RUNTIME = "agent_runtime"
    MCP_SERVER_CONNECTIVITY = "mcp_server_connectivity"


class IntakeResult(BaseModel):
    system_goal: str = ""
    constraints: List[str] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    success_metrics: List[str] = Field(default_factory=list)
    summary: str = ""


class ToolAgentInput(_SharedToolAgentInput):
    """Adds ``spec_context``: the LLM prompt context threaded through this team's
    ``JsonGeneratorToolAgent`` (unlike the code-v2 teams, this team has no
    ``language`` notion, so that shared field stays unused here)."""

    spec_context: str = ""


class ToolAgentOutput(BaseModel):
    files: Dict[str, str] = Field(default_factory=dict)
    recommendations: List[str] = Field(default_factory=list)
    summary: str = ""
    success: bool = True


class ExecutionResult(_SharedExecutionResult):
    """Adds ``notes``: free-form notes accumulated from tool-agent
    recommendations across an execution run."""

    notes: List[str] = Field(default_factory=list)


class ReviewIssue(BaseModel):
    source: str = "review"
    severity: str = "medium"
    description: str = ""
    recommendation: str = ""


class ReviewResult(BaseModel):
    passed: bool = False
    issues: List[ReviewIssue] = Field(default_factory=list)
    required_artifacts_ok: bool = False
    summary: str = ""


class ProblemSolvingResult(BaseModel):
    resolved: bool = False
    fixes_applied: List[str] = Field(default_factory=list)
    files: Dict[str, str] = Field(default_factory=dict)
    summary: str = ""


class DeliverResult(BaseModel):
    summary: str = ""
    handoff_notes: List[str] = Field(default_factory=list)
    runbook: List[str] = Field(default_factory=list)


class WorkflowTraceEvent(BaseModel):
    phase: Phase
    message: str = ""


class AIAgentDevelopmentWorkflowResult(BaseModel):
    """Result envelope for ``AIAgentDevelopmentTeamLead.run_workflow``.

    ``final_files`` is the last attempted file set after execution and any
    problem-solving patches — not strictly "files that passed review". On
    success it matches the reviewed/passing set; on abort or exhausted-retry
    paths it may include unreviewed partial fixes so callers can inspect
    progress. Check ``success`` (and ``needs_followup``) before treating
    ``final_files`` as validated deliverables.
    """

    task_id: str = ""
    success: bool = False
    current_phase: Phase = Phase.INTAKE
    iterations_used: int = 0
    intake_result: Optional[IntakeResult] = None
    planning_result: Optional[PlanningResult] = None
    execution_result: Optional[ExecutionResult] = None
    review_result: Optional[ReviewResult] = None
    problem_solving_result: Optional[ProblemSolvingResult] = None
    deliver_result: Optional[DeliverResult] = None
    final_files: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Last attempted file set after execution/problem-solving. On "
            "success this is the reviewed set; on abort/exhausted paths it "
            "may include unreviewed partial fixes. Gate on success before "
            "treating these as validated deliverables."
        ),
    )
    summary: str = ""
    failure_reason: str = ""
    needs_followup: bool = False
    trace: List[WorkflowTraceEvent] = Field(default_factory=list)
