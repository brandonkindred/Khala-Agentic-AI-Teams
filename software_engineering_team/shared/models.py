"""
Shared models for the software engineering team.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class TaskType(str, Enum):
    """Type of engineering task."""

    ARCHITECTURE = "architecture"
    GIT_SETUP = "git_setup"
    DEVOPS = "devops"
    SECURITY = "security"
    BACKEND = "backend"
    FRONTEND = "frontend"
    QA = "qa"


class TaskStatus(str, Enum):
    """Status of a task."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


class ProductRequirements(BaseModel):
    """Product requirements from a product manager."""

    title: str = Field(..., description="Product or feature title")
    description: str = Field(..., description="Detailed description of requirements")
    acceptance_criteria: List[str] = Field(
        default_factory=list,
        description="Acceptance criteria that must be met",
    )
    constraints: List[str] = Field(
        default_factory=list,
        description="Technical or business constraints",
    )
    priority: str = Field(default="medium", description="Priority: high, medium, low")
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ArchitectureComponent(BaseModel):
    """A component in the system architecture."""

    name: str
    type: str  # e.g. backend, frontend, database, cache, queue
    description: str = ""
    technology: Optional[str] = None
    dependencies: List[str] = Field(default_factory=list)
    interfaces: List[str] = Field(default_factory=list)


class SystemArchitecture(BaseModel):
    """System architecture design produced by the Architecture Expert."""

    overview: str = Field(..., description="High-level architecture overview")
    components: List[ArchitectureComponent] = Field(default_factory=list)
    architecture_document: str = Field(
        default="",
        description="Full markdown document describing the architecture",
    )
    diagrams: Dict[str, str] = Field(
        default_factory=dict,
        description="Diagram descriptions or Mermaid/ASCII art",
    )
    decisions: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Architecture decision records",
    )


class Task(BaseModel):
    """A single task assigned to a team member."""

    id: str
    type: TaskType
    description: str
    assignee: str  # agent identifier
    requirements: str = ""
    dependencies: List[str] = Field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    output: Optional[str] = None
    artifacts: Dict[str, str] = Field(default_factory=dict)


class TaskAssignment(BaseModel):
    """Output from Tech Lead: tasks distributed to the team."""

    tasks: List[Task] = Field(default_factory=list)
    execution_order: List[str] = Field(
        default_factory=list,
        description="Ordered task IDs for execution",
    )
    rationale: str = ""
