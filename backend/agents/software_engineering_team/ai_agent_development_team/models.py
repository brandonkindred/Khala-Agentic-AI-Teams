"""Models for AI Agent Development Team orchestration."""

from __future__ import annotations

from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


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


class MicrotaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class IntakeResult(BaseModel):
    system_goal: str = ""
    constraints: List[str] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    success_metrics: List[str] = Field(default_factory=list)
    summary: str = ""


class Microtask(BaseModel):
    id: str
    title: str
    description: str = ""
    tool_agent: ToolAgentKind = ToolAgentKind.GENERAL
    depends_on: List[str] = Field(default_factory=list)
    status: MicrotaskStatus = MicrotaskStatus.PENDING
    output_files: Dict[str, str] = Field(default_factory=dict)
    notes: str = ""


class PlanningResult(BaseModel):
    microtasks: List[Microtask] = Field(default_factory=list)
    summary: str = ""


class ToolAgentInput(BaseModel):
    microtask: Microtask
    repo_path: str = ""
    spec_context: str = ""
    existing_code: str = ""


class ToolAgentOutput(BaseModel):
    files: Dict[str, str] = Field(default_factory=dict)
    recommendations: List[str] = Field(default_factory=list)
    summary: str = ""
    success: bool = True


class ExecutionResult(BaseModel):
    files: Dict[str, str] = Field(default_factory=dict)
    microtasks: List[Microtask] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)
    summary: str = ""


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
    final_files: Dict[str, str] = Field(default_factory=dict)
    summary: str = ""
    failure_reason: str = ""
    needs_followup: bool = False
    trace: List[WorkflowTraceEvent] = Field(default_factory=list)
