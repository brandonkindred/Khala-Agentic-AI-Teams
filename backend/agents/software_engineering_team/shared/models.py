"""
Shared models for the software engineering team.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


class TaskType(str, Enum):
    """Type of engineering task."""

    ARCHITECTURE = "architecture"
    GIT_SETUP = "git_setup"
    DEVOPS = "devops"
    SECURITY = "security"
    BACKEND = "backend"
    FRONTEND = "frontend"
    QA = "qa"
    DOCUMENTATION = "documentation"


class PricingTier(str, Enum):
    """Pricing model for a tool or service."""

    FREE = "free"
    FREEMIUM = "freemium"
    PAID = "paid"
    ENTERPRISE = "enterprise"
    USAGE_BASED = "usage_based"


class LicenseType(str, Enum):
    """License type for a tool or library."""

    MIT = "mit"
    APACHE_2 = "apache_2"
    GPL = "gpl"
    BSD = "bsd"
    PROPRIETARY = "proprietary"
    CUSTOM_OSS = "custom_oss"
    UNKNOWN = "unknown"


class ToolRecommendation(BaseModel):
    """
    Structured recommendation for a tool, library, framework, or service.

    Provides decision-relevant details for founders and technical leaders:
    pricing, licensing, adoption complexity, and risk factors.
    """

    name: str = Field(..., description="Name of the tool/service")
    category: str = Field(
        ...,
        description="Category: database, ci_cd, monitoring, framework, hosting, auth, etc.",
    )
    description: str = Field(..., description="Brief description of what the tool does")
    rationale: str = Field(..., description="Why this tool is recommended for this use case")

    # Pricing
    pricing_tier: PricingTier = Field(..., description="Pricing model")
    pricing_details: str = Field(
        ...,
        description="Specific pricing info, e.g. 'Free tier: 10k events/mo; Pro: $25/mo'",
    )
    estimated_monthly_cost: Optional[str] = Field(
        default=None,
        description="Estimated monthly cost for the use case, e.g. '$0-50', 'usage-based'",
    )

    # Licensing
    license_type: LicenseType = Field(..., description="License type")
    is_open_source: bool = Field(..., description="Whether the tool is open source")
    source_url: Optional[str] = Field(
        default=None,
        description="URL to source code if open source (GitHub, GitLab, etc.)",
    )

    # Adoption considerations
    ease_of_integration: str = Field(
        default="medium",
        description="Ease of integration: low, medium, high",
    )
    learning_curve: str = Field(
        default="moderate",
        description="Learning curve: minimal, moderate, steep",
    )
    documentation_quality: str = Field(
        default="good",
        description="Documentation quality: poor, adequate, good, excellent",
    )
    community_size: str = Field(
        default="medium",
        description="Community size: small, medium, large, massive",
    )
    maturity: str = Field(
        default="mature",
        description="Maturity level: emerging, growing, mature, legacy",
    )

    # Risk factors
    vendor_lock_in_risk: str = Field(
        default="low",
        description="Vendor lock-in risk: none, low, medium, high",
    )
    migration_complexity: str = Field(
        default="moderate",
        description="Migration complexity if switching away: trivial, moderate, complex",
    )

    # Alternatives
    alternatives: List[str] = Field(
        default_factory=list,
        description="1-3 alternative tools/services",
    )
    why_not_alternatives: str = Field(
        default="",
        description="Brief explanation of why the primary recommendation beats alternatives",
    )

    # Confidence
    confidence: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        description="Confidence score (0.0-1.0) that this is the right choice",
    )

    @field_validator("estimated_monthly_cost", mode="before")
    @classmethod
    def coerce_cost_to_string(cls, v):
        """Coerce numeric values to strings for estimated_monthly_cost.
        
        LLMs sometimes return float/int values instead of strings.
        """
        if v is None:
            return v
        if isinstance(v, (int, float)):
            if v == 0:
                return "$0"
            return f"${v:.2f}" if isinstance(v, float) else f"${v}"
        return v


class TaskStatus(str, Enum):
    """Status of a task."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    READY_FOR_REVIEW = "ready_for_review"
    APPROVED = "approved"
    MERGED = "merged"
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
        description="Architecture decision records (ADR-001, ADR-002, ...)",
    )
    tenancy_model: str = Field(
        default="",
        description="Tenancy model: single tenant, pooled, isolated, hybrid",
    )
    reliability_model: str = Field(
        default="",
        description="Reliability model: blast radius, failure modes, graceful degradation",
    )
    planning_hints: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Derived hints for planning agents (e.g. grouped backend/frontend/infra "
            "components, important data flows, and other planning-oriented summaries."
        ),
    )


class Task(BaseModel):
    """A single task assigned to a team member."""

    id: str
    type: TaskType
    title: str = Field(
        default="",
        description="Descriptive title for the task, e.g. 'Backend Todo CRUD API'",
    )
    description: str = Field(
        default="",
        description="In-depth description of outcomes based on the spec",
    )
    user_story: str = Field(
        default="",
        description="User story in the format: As a <role>, I want <goal> so that <benefit>",
    )
    assignee: str  # agent identifier
    requirements: str = ""
    dependencies: List[str] = Field(default_factory=list)
    acceptance_criteria: List[str] = Field(
        default_factory=list,
        description="Specific, testable acceptance criteria for this task (3-7 items)",
    )
    status: TaskStatus = TaskStatus.PENDING
    feature_branch_name: Optional[str] = Field(
        None,
        description="Feature branch for this task, e.g. feature/backend-todo-crud-api",
    )
    output: Optional[str] = None
    artifacts: Dict[str, str] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Optional arbitrary metadata (e.g. linked architecture component, user_story source).",
    )


class TaskUpdate(BaseModel):
    """Completion report from a specialist agent after finishing a task."""

    task_id: str = Field(..., description="ID of the completed task")
    agent_type: str = Field(..., description="Type of agent that completed the task: backend, frontend, devops")
    status: str = Field(
        default="completed",
        description="Completion status: completed, failed, partial",
    )
    summary: str = Field(
        default="",
        description="Agent's summary of what was done",
    )
    files_changed: List[str] = Field(
        default_factory=list,
        description="List of files created or modified",
    )
    needs_followup: bool = Field(
        default=False,
        description="Whether the agent flagged follow-up work is needed",
    )
    failure_reason: Optional[str] = Field(
        default=None,
        description="When status is failed, the build/test error or reason. Used by Tech Lead to create targeted fix tasks.",
    )
    failure_class: Optional[str] = Field(
        default=None,
        description="When status is failed, optional classification e.g. 'llm_connectivity'. Tech Lead may skip creating fix tasks for certain classes.",
    )


class TaskAssignment(BaseModel):
    """Output from Tech Lead: tasks distributed to the team."""

    tasks: List[Task] = Field(default_factory=list)
    execution_order: List[str] = Field(
        default_factory=list,
        description="Ordered task IDs for execution",
    )
    rationale: str = ""


class TaskPlan(BaseModel):
    """
    A single task under a story. This is the unit distributed to backend, frontend, or devops.
    When flattened, becomes a shared Task in TaskAssignment.
    """

    id: str
    title: str = ""
    description: str = Field(
        default="",
        description="Detailed description of what to build; expected behavior; done criteria.",
    )
    user_story: str = ""
    assignee: str = Field(
        ...,
        description="Engineer type: frontend, backend, or devops",
    )
    requirements: str = ""
    dependencies: List[str] = Field(default_factory=list)
    acceptance_criteria: List[str] = Field(
        default_factory=list,
        description="Specific, testable acceptance criteria for this task.",
    )
    example: Optional[str] = Field(
        default=None,
        description="Optional example (e.g. sample request/response, UI mockup description).",
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Optional metadata (e.g. framework_target for frontend).",
    )
    complexity_points: int = Field(
        default=2,
        ge=1,
        le=13,
        description="Fibonacci complexity estimate. Planning V2 guideline: most tasks 2-5 days (points 2, 3, 5); point 1 (~1 day) is allowed as an exception. Downstream teams break tasks into subtasks.",
    )


class StoryPlan(BaseModel):
    """
    A user story that is part of an epic. Contains one or more tasks (TaskPlan).
    Stories and tasks are both highly detailed (acceptance criteria, description, example).
    The tasks under a story are what get distributed to teams.
    """

    id: str
    title: str
    description: str = Field(
        default="",
        description="Detailed description of the story; what done looks like.",
    )
    user_story: str = ""
    requirements: str = ""
    acceptance_criteria: List[str] = Field(
        default_factory=list,
        description="Acceptance criteria for the story as a whole.",
    )
    example: Optional[str] = Field(
        default=None,
        description="Optional example (e.g. user flow, sample scenario).",
    )
    tasks: List[TaskPlan] = Field(
        default_factory=list,
        description="Tasks under this story; these are distributed to backend/frontend/devops.",
    )
    assignee: Optional[str] = Field(
        default=None,
        description="Used only when tasks is empty (backward compat): assignee for the single task derived from this story.",
    )
    dependencies: List[str] = Field(
        default_factory=list,
        description="Used only when tasks is empty (backward compat): dependencies for the single task derived from this story.",
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Optional metadata.",
    )


class Epic(BaseModel):
    """Feature definition containing high-level user stories and acceptance criteria."""

    id: str
    title: str
    description: str = ""
    user_stories_summary: List[str] = Field(default_factory=list)
    acceptance_criteria: List[str] = Field(default_factory=list)
    stories: List[StoryPlan] = Field(default_factory=list)


class Initiative(BaseModel):
    """High-level goal that contains epics."""

    id: str
    title: str
    description: str = ""
    epics: List[Epic] = Field(default_factory=list)


class PlanningHierarchy(BaseModel):
    """Full planning output: Initiative -> Epic -> Story hierarchy."""

    initiatives: List[Initiative] = Field(default_factory=list)
    execution_order: List[str] = Field(
        default_factory=list,
        description="Task IDs in execution order (tasks are the assignable units under stories)",
    )
    rationale: str = ""


def model_to_dict(obj: Any) -> Dict[str, Any]:
    """
    Convert a Pydantic model (v1 or v2) or similar object to a plain dict.
    Supports both .model_dump() (Pydantic v2) and .dict() (Pydantic v1).
    Uses try/except to handle edge cases where hasattr is True but the method
    raises (e.g. proxy objects, partial implementations).
    """
    if obj is None:
        return {}
    try:
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
    except AttributeError:
        pass
    try:
        if hasattr(obj, "dict"):
            return obj.dict()
    except AttributeError:
        pass
    if hasattr(obj, "__dict__"):
        return dict(obj.__dict__)
    return {}
