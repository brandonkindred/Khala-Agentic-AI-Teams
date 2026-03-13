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
