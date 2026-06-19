"""
Pydantic models for the coding_team: tasks, stacks, plan input, and job state.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    """Status of a task in the Task Graph."""

    TO_DO = "to_do"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    MERGED = "merged"
    FAILED = "failed"


class StackSpec(BaseModel):
    """Defines one tech stack (e.g. frontend, backend). One Senior SWE per stack."""

    tools_services: List[str] = Field(
        default_factory=list,
        description="List of tools/services, e.g. ['Angular', 'Tailwind CSS'] or ['Java', 'Spring Boot', 'Postgres']",
    )
    name: Optional[str] = Field(
        default=None,
        description="Optional human-readable stack name, e.g. 'frontend', 'backend'",
    )


class Subtask(BaseModel):
    """A subtask belonging to a parent task. Can have dependencies on other subtasks."""

    id: str = Field(..., description="Unique subtask id")
    title: str = Field(default="", description="Subtask title")
    description: str = Field(default="", description="Subtask description")
    dependencies: List[str] = Field(
        default_factory=list,
        description="Ids of subtasks that must be complete before this one",
    )
    status: TaskStatus = Field(default=TaskStatus.TO_DO)
    completed_at: Optional[datetime] = None


class Task(BaseModel):
    """A task in the Task Graph. Supports acceptance criteria, out-of-scope, priority, subtasks."""

    id: str = Field(..., description="Unique task id")
    title: str = Field(default="", description="Task title")
    description: str = Field(default="", description="Task description")
    dependencies: List[str] = Field(
        default_factory=list,
        description="Ids of tasks that must be merged before this task can be assigned",
    )
    status: TaskStatus = Field(default=TaskStatus.TO_DO)
    assigned_agent_id: Optional[str] = Field(
        default=None,
        description="Agent (Senior SWE) assigned to this task",
    )
    feature_branch: Optional[str] = Field(
        default=None,
        description="Git feature branch for this task",
    )
    merged_at: Optional[datetime] = Field(
        default=None,
        description="When the feature branch was merged",
    )
    acceptance_criteria: List[str] = Field(
        default_factory=list,
        description="Conditions that must be met for the task to be complete",
    )
    out_of_scope: str = Field(
        default="",
        description="What is explicitly not part of this task",
    )
    priority: str = Field(default="medium", description="Priority: high, medium, low")
    subtasks: List[Subtask] = Field(
        default_factory=list,
        description="Well-defined subtasks with optional dependencies between them",
    )
    changes_summary: Optional[str] = Field(
        default=None,
        description="Senior SWE's summary of the implemented changes, for Tech Lead review",
    )
    revision_count: int = Field(
        default=0,
        description="Number of times returned for revision (quality gate or Tech Lead rejection)",
    )
    revision_feedback: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Feedback from prior revision rounds (quality gate or Tech Lead review)",
    )
    no_change_revisits: int = Field(
        default=0,
        description=(
            "Consecutive revision rounds that produced NO change to the task's branch diff "
            "(the engineer revisited work already flagged done without altering the code). "
            "Reset to 0 the moment a round changes the diff. Distinct from revision_count, which "
            "counts every revision; this counts only zero-progress re-evaluations and caps them."
        ),
    )
    last_change_digest: str = Field(
        default="",
        description=(
            "Hash of the task's branch diff at the previous bounce, used to detect a round that "
            "made no change. Empty until the first bounce records a baseline."
        ),
    )
    resolved_without_changes: bool = Field(
        default=False,
        description=(
            "True when the Tech Lead adjudicated a stalled (no-change) task as already complete: "
            "the task is terminal (MERGED) but landed no diff, so the job-level outcome treats it "
            "as 'already done' rather than real merged work."
        ),
    )


class SeniorEngineerSpec(BaseModel):
    """Spec for one Senior SWE agent: agent_id and the stack they specialize in."""

    agent_id: str = Field(..., description="Unique agent id (e.g. stack name or uuid)")
    stack_spec: StackSpec = Field(..., description="Tech stack this agent specializes in")


# ---------------------------------------------------------------------------
# Plan input (from Planning team handoff)
# ---------------------------------------------------------------------------


class CodingTeamPlanInput(BaseModel):
    """Input passed from the software_engineering_team orchestrator to coding_team.
    Mirrors what the Planning team (Planning V3) produces; architecture comes from handoff.
    """

    requirements_title: str = Field(default="Project", description="Product/project title")
    requirements_description: str = Field(
        default="",
        description="Requirements description (e.g. from PRD + validated spec)",
    )
    project_overview: Dict[str, Any] = Field(
        default_factory=dict,
        description="Project overview (features_and_functionality_doc, goals, etc.)",
    )
    hierarchy: Optional[Any] = Field(
        default=None,
        description="PlanningHierarchy if available (initiatives/epics/stories)",
    )
    final_spec_content: Optional[str] = Field(
        default=None,
        description="Final approved spec content from Planning V3",
    )
    repo_path: str = Field(..., description="Path to the repository")
    architecture_overview: Optional[str] = Field(
        default=None,
        description="Architecture overview from Planning V3 handoff",
    )
    existing_code_summary: Optional[str] = Field(
        default=None,
        description="Optional summary of existing codebase",
    )
    resolved_questions: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="User-provided answers from clarification",
    )
    open_questions: List[str] = Field(default_factory=list)
    assumptions: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Job state (for persistence / status API)
# ---------------------------------------------------------------------------


class CodingTeamJobState(BaseModel):
    """Persisted state for a coding_team job: task graph snapshot and agent-task mapping."""

    job_id: str = Field(...)
    repo_path: str = Field(default="")
    phase: str = Field(default="task_graph", description="e.g. task_graph, coding, execution")
    status_text: str = Field(default="")
    task_graph_snapshot: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Serialized tasks for this job",
    )
    agent_task_map: Dict[str, str] = Field(
        default_factory=dict,
        description="agent_id -> task_id for currently assigned non-merged task",
    )
    stack_specs: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="StackSpec list for this job",
    )
    updated_at: Optional[datetime] = None


class AgentStatusEntry(BaseModel):
    """Live status of one coding-team agent, derived for the status API / UI.

    Derived (never persisted) from the job's stack specs, agent->task map, task-graph
    snapshot, and current_activity by ``coding_team.agent_status.build_agent_statuses``. One
    entry per agent: the Tech Lead (coordinator) plus one Senior SWE per stack.

    Invariants:
        - ``role`` is ``"tech_lead"`` or ``"senior_engineer"``.
        - For an engineer, ``status`` is ``working``/``in_review``/``idle``; for the Tech
          Lead, ``planning``/``reviewing``/``idle``.
        - ``current_task_id``/``current_task_title`` are set only while the agent holds a live
          (non-terminal) task; the ``current_step``/``activity_detail``/``activity_fraction``
          fields only when this agent owns the single live ``current_activity``.
    """

    agent_id: str = Field(..., description="Stable agent id (engineer stack name, or 'tech_lead')")
    role: str = Field(..., description="'tech_lead' or 'senior_engineer'")
    display_name: str = Field(..., description="Human-readable label for the agent card")
    stack: Optional[str] = Field(
        default=None, description="Stack name for engineers; None for the Tech Lead"
    )
    tools_services: List[str] = Field(
        default_factory=list,
        description="Tools/services the engineer specializes in (empty for the Tech Lead)",
    )
    status: str = Field(
        default="idle",
        description="working/in_review/idle (engineer) or planning/reviewing/idle (tech lead)",
    )
    current_task_id: Optional[str] = Field(
        default=None, description="Id of the task the agent is currently working"
    )
    current_task_title: Optional[str] = Field(
        default=None, description="Title of the task the agent is currently working"
    )
    current_step: Optional[str] = Field(
        default=None, description="Live sub-step from current_activity, when this agent owns it"
    )
    activity_detail: Optional[str] = Field(
        default=None, description="Human detail from current_activity, when this agent owns it"
    )
    activity_fraction: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="0.0-1.0 progress of the live sub-step, when this agent owns it",
    )
