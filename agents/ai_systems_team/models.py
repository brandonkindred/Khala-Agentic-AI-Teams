"""
Domain models for the AI Systems Team.

Defines phases, request/response models, and blueprint types
for the AI system generation workflow.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class Phase(str, Enum):
    """Lifecycle phases of the AI system generation workflow."""

    SPEC_INTAKE = "spec_intake"
    ARCHITECTURE = "architecture"
    CAPABILITIES = "capabilities"
    EVALUATION = "evaluation"
    SAFETY = "safety"
    BUILD = "build"


class OrchestrationPattern(str, Enum):
    """Agent orchestration patterns."""

    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"
    HIERARCHICAL = "hierarchical"
    EVENT_DRIVEN = "event_driven"
    HYBRID = "hybrid"


class AgentRole(BaseModel):
    """Definition of an agent role in the system."""

    name: str = Field(..., description="Agent role name")
    description: str = Field(..., description="What this agent does")
    capabilities: List[str] = Field(default_factory=list, description="Agent capabilities")
    tools: List[str] = Field(default_factory=list, description="Tools the agent can use")
    inputs: List[str] = Field(default_factory=list, description="Expected inputs")
    outputs: List[str] = Field(default_factory=list, description="Expected outputs")


class HandoffRule(BaseModel):
    """Rule for agent-to-agent handoff."""

    from_agent: str
    to_agent: str
    condition: str = Field(..., description="When this handoff occurs")
    data_passed: List[str] = Field(default_factory=list)


class OrchestrationGraph(BaseModel):
    """Orchestration structure for the agent system."""

    pattern: OrchestrationPattern = OrchestrationPattern.SEQUENTIAL
    agents: List[AgentRole] = Field(default_factory=list)
    handoffs: List[HandoffRule] = Field(default_factory=list)
    entry_point: Optional[str] = None
    exit_points: List[str] = Field(default_factory=list)


class ToolContract(BaseModel):
    """Contract for a tool integration."""

    name: str
    description: str
    inputs: Dict[str, str] = Field(default_factory=dict)
    outputs: Dict[str, str] = Field(default_factory=dict)
    error_handling: str = ""
    rate_limits: Optional[str] = None


class MemoryPolicy(BaseModel):
    """Memory and state management policy."""

    session_memory: bool = True
    long_term_memory: bool = False
    retrieval_enabled: bool = False
    audit_trail: bool = True
    retention_days: int = 30


class SafetyCheckpoint(BaseModel):
    """Safety and governance checkpoint."""

    name: str
    description: str
    trigger: str = Field(..., description="When this checkpoint activates")
    action: str = Field(..., description="What happens at this checkpoint")
    requires_human_approval: bool = False


class AcceptanceTest(BaseModel):
    """Acceptance test definition."""

    name: str
    description: str
    input_scenario: str
    expected_outcome: str
    pass_criteria: str


class KPI(BaseModel):
    """Key performance indicator."""

    name: str
    description: str
    metric: str
    target_value: str
    measurement_method: str


class EvaluationHarness(BaseModel):
    """Evaluation framework for the agent system."""

    acceptance_tests: List[AcceptanceTest] = Field(default_factory=list)
    adversarial_tests: List[str] = Field(default_factory=list)
    kpis: List[KPI] = Field(default_factory=list)
    pass_threshold: float = 0.8


class RolloutStage(BaseModel):
    """Stage in the rollout plan."""

    name: str
    description: str
    criteria_to_advance: str
    rollback_criteria: str


class RolloutPlan(BaseModel):
    """Rollout plan for the agent system."""

    stages: List[RolloutStage] = Field(default_factory=list)


class SpecIntakeResult(BaseModel):
    """Result of the spec intake phase."""

    success: bool
    goals: List[str] = Field(default_factory=list)
    non_goals: List[str] = Field(default_factory=list)
    assumptions: List[str] = Field(default_factory=list)
    constraints: List[str] = Field(default_factory=list)
    allowed_actions: List[str] = Field(default_factory=list)
    disallowed_actions: List[str] = Field(default_factory=list)
    human_approval_points: List[str] = Field(default_factory=list)
    quality_expectations: Dict[str, str] = Field(default_factory=dict)
    error: Optional[str] = None


class ArchitectureResult(BaseModel):
    """Result of the architecture phase."""

    success: bool
    orchestration: Optional[OrchestrationGraph] = None
    rationale: str = ""
    error: Optional[str] = None


class CapabilitiesResult(BaseModel):
    """Result of the capabilities planning phase."""

    model_config = ConfigDict(protected_namespaces=())

    success: bool
    tool_contracts: List[ToolContract] = Field(default_factory=list)
    memory_policy: Optional[MemoryPolicy] = None
    model_requirements: Dict[str, str] = Field(default_factory=dict)
    error: Optional[str] = None


class EvaluationResult(BaseModel):
    """Result of the evaluation phase."""

    success: bool
    harness: Optional[EvaluationHarness] = None
    error: Optional[str] = None


class SafetyResult(BaseModel):
    """Result of the safety and governance phase."""

    success: bool
    checkpoints: List[SafetyCheckpoint] = Field(default_factory=list)
    guardrails: List[str] = Field(default_factory=list)
    policy_requirements: List[str] = Field(default_factory=list)
    error: Optional[str] = None


class BuildResult(BaseModel):
    """Result of the build/packaging phase."""

    success: bool
    artifacts: List[str] = Field(default_factory=list)
    rollout_plan: Optional[RolloutPlan] = None
    finalized_at: Optional[datetime] = None
    error: Optional[str] = None


class AgentBlueprint(BaseModel):
    """Complete blueprint for an AI agent system."""

    project_name: str
    version: str = "1.0.0"
    created_at: Optional[datetime] = None

    spec_intake: Optional[SpecIntakeResult] = None
    architecture: Optional[ArchitectureResult] = None
    capabilities: Optional[CapabilitiesResult] = None
    evaluation: Optional[EvaluationResult] = None
    safety: Optional[SafetyResult] = None
    build: Optional[BuildResult] = None

    current_phase: Phase = Phase.SPEC_INTAKE
    completed_phases: List[Phase] = Field(default_factory=list)
    success: bool = False
    error: Optional[str] = None


class AISystemRequest(BaseModel):
    """Request to generate a new AI agent system."""

    project_name: str = Field(..., description="Name for the AI system project")
    spec_path: str = Field(
        ...,
        description="Path to the specification file",
    )
    constraints: Dict[str, Any] = Field(
        default_factory=dict,
        description="Additional constraints (budget, latency, privacy, etc.)",
    )
    output_dir: Optional[str] = Field(
        default=None,
        description="Directory to output generated artifacts",
    )


class AISystemJobResponse(BaseModel):
    """Response when starting an AI system build job."""

    job_id: str
    status: str
    message: str


class AISystemStatusResponse(BaseModel):
    """Response for job status queries."""

    job_id: str
    status: str
    project_name: Optional[str] = None
    current_phase: Optional[str] = None
    progress: int = 0
    completed_phases: List[str] = Field(default_factory=list)
    error: Optional[str] = None
    blueprint: Optional[AgentBlueprint] = None


class AISystemJobSummary(BaseModel):
    """Summary of an AI system job for listing."""

    job_id: str
    project_name: str
    status: str
    created_at: Optional[str] = None
    current_phase: Optional[str] = None
    progress: int = 0


class AISystemJobsListResponse(BaseModel):
    """Response for listing AI system jobs."""

    jobs: List[AISystemJobSummary] = Field(default_factory=list)
