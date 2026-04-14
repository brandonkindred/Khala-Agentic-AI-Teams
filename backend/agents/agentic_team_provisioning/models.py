"""Pydantic models for the Agentic Team Provisioning service."""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TriggerType(str, Enum):
    """How a process is initiated."""

    MESSAGE = "message"
    EVENT = "event"
    SCHEDULE = "schedule"
    MANUAL = "manual"


class StepType(str, Enum):
    """The kind of work a process step represents."""

    ACTION = "action"
    DECISION = "decision"
    PARALLEL_SPLIT = "parallel_split"
    PARALLEL_JOIN = "parallel_join"
    WAIT = "wait"
    SUBPROCESS = "subprocess"


class ProcessStatus(str, Enum):
    """Lifecycle status of a process definition."""

    DRAFT = "draft"
    COMPLETE = "complete"
    ARCHIVED = "archived"


class TeamMode(str, Enum):
    """Toggle between development and interactive testing."""

    DEVELOPMENT = "development"
    TESTING = "testing"


class MessageRating(str, Enum):
    """Thumbs-up / thumbs-down rating for a chat message."""

    THUMBS_UP = "thumbs_up"
    THUMBS_DOWN = "thumbs_down"


class PipelineRunStatus(str, Enum):
    """Lifecycle of an end-to-end pipeline test run."""

    RUNNING = "running"
    WAITING_FOR_INPUT = "waiting_for_input"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# Process building blocks
# ---------------------------------------------------------------------------


class ProcessStepAgent(BaseModel):
    """An agent assigned to a process step."""

    agent_name: str = Field(..., description="Display name of the agent")
    role: str = Field(..., description="What this agent does in the step")


class ProcessStep(BaseModel):
    """A single step within a process."""

    step_id: str = Field(..., description="Unique id within the process (e.g. step_1)")
    name: str = Field(..., description="Human-readable step name")
    description: str = Field(default="", description="What happens in this step")
    step_type: StepType = Field(default=StepType.ACTION)
    agents: list[ProcessStepAgent] = Field(
        default_factory=list, description="Agents responsible for this step"
    )
    next_steps: list[str] = Field(
        default_factory=list, description="step_ids that follow this step"
    )
    condition: Optional[str] = Field(
        default=None, description="Condition expression for decision steps"
    )


class ProcessTrigger(BaseModel):
    """Describes what initiates a process."""

    trigger_type: TriggerType = Field(default=TriggerType.MESSAGE)
    description: str = Field(default="", description="Human-readable description of the trigger")


class ProcessOutput(BaseModel):
    """Describes the deliverable produced when a process completes."""

    description: str = Field(default="", description="What is produced at the end")
    destination: str = Field(default="", description="Where/how the output is delivered")


class ProcessDefinition(BaseModel):
    """A complete process definition for an agentic team."""

    process_id: str = Field(..., description="Unique id (UUID)")
    name: str = Field(default="", description="Process name")
    description: str = Field(default="", description="Short description of the process")
    trigger: ProcessTrigger = Field(default_factory=ProcessTrigger)
    steps: list[ProcessStep] = Field(default_factory=list)
    output: ProcessOutput = Field(default_factory=ProcessOutput)
    status: ProcessStatus = Field(default=ProcessStatus.DRAFT)


# ---------------------------------------------------------------------------
# Agents pool (team-level roster)
# ---------------------------------------------------------------------------


class AgenticTeamAgent(BaseModel):
    """A named agent in the team's roster.

    The roster captures enough detail to validate that the team is fully
    staffed: every process need (skill, capability, tool, expertise) must be
    covered by at least one agent on the roster.
    """

    agent_name: str = Field(..., description="Stable, unique name within the team")
    role: str = Field(..., description="Primary role this agent fills on the team")
    skills: list[str] = Field(
        default_factory=list,
        description="Specific skills the agent possesses (e.g. 'data analysis', 'copywriting')",
    )
    capabilities: list[str] = Field(
        default_factory=list,
        description="Functional capabilities (e.g. 'code generation', 'web search', 'image classification')",
    )
    tools: list[str] = Field(
        default_factory=list,
        description="Tools or integrations the agent can use (e.g. 'Git', 'PostgreSQL', 'Slack API')",
    )
    expertise: list[str] = Field(
        default_factory=list,
        description="Domain expertise areas (e.g. 'customer support', 'financial analysis', 'HIPAA compliance')",
    )


# ---------------------------------------------------------------------------
# Agentic Team
# ---------------------------------------------------------------------------


class AgenticTeam(BaseModel):
    """Top-level team definition containing an agents pool and processes."""

    team_id: str = Field(..., description="Unique id (UUID)")
    name: str = Field(..., description="Team display name")
    description: str = Field(default="", description="Short description of what the team does")
    mode: TeamMode = Field(default=TeamMode.DEVELOPMENT)
    agents: list[AgenticTeamAgent] = Field(
        default_factory=list, description="Named agents pool (Agent 1 … Agent N)"
    )
    processes: list[ProcessDefinition] = Field(default_factory=list)
    created_at: str = Field(default="")
    updated_at: str = Field(default="")


# ---------------------------------------------------------------------------
# API request / response models
# ---------------------------------------------------------------------------


class CreateTeamRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)


class CreateTeamResponse(BaseModel):
    team_id: str
    name: str
    description: str
    created_at: str


class TeamSummary(BaseModel):
    team_id: str
    name: str
    description: str
    process_count: int
    created_at: str
    updated_at: str


class TeamDetailResponse(BaseModel):
    team: AgenticTeam


# ---------------------------------------------------------------------------
# Conversation models
# ---------------------------------------------------------------------------


class ConversationMessage(BaseModel):
    role: str = Field(..., pattern=r"^(user|assistant)$")
    content: str
    timestamp: str


class CreateConversationRequest(BaseModel):
    initial_message: Optional[str] = None
    team_id: str


class SendMessageRequest(BaseModel):
    message: str = Field(..., min_length=1)


class ConversationStateResponse(BaseModel):
    conversation_id: str
    team_id: str
    messages: list[ConversationMessage] = Field(default_factory=list)
    current_process: Optional[ProcessDefinition] = None
    suggested_questions: list[str] = Field(default_factory=list)


class ConversationSummaryResponse(BaseModel):
    conversation_id: str
    team_id: str
    created_at: str
    updated_at: str
    message_count: int


# ---------------------------------------------------------------------------
# Per-team infrastructure API models
# ---------------------------------------------------------------------------


class FormRecord(BaseModel):
    """A single form data record."""

    record_id: str
    form_key: str
    data: dict = Field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


class CreateFormRecordRequest(BaseModel):
    data: dict = Field(..., description="Arbitrary JSON data for this form record")


class UpdateFormRecordRequest(BaseModel):
    data: dict = Field(..., description="Updated JSON data")


class AssetInfo(BaseModel):
    """Metadata for a file in the team's asset directory."""

    name: str
    size_bytes: int
    modified_at: str


class TeamJobSummary(BaseModel):
    job_id: str
    status: str
    created_at: str = ""
    updated_at: str = ""


class TeamJobDetail(BaseModel):
    job_id: str
    status: str
    data: dict = Field(default_factory=dict)


class TeamPendingQuestion(BaseModel):
    job_id: str
    question: dict = Field(default_factory=dict)


class SubmitTeamAnswersRequest(BaseModel):
    answers: list[dict] = Field(..., description="List of answer objects")


class RosterGap(BaseModel):
    """A specific gap in the team's roster coverage."""

    category: str = Field(
        ...,
        description="Gap category: 'unrostered_agent', 'missing_skill', 'missing_capability', "
        "'missing_tool', 'missing_expertise', 'unstaffed_step'",
    )
    detail: str = Field(..., description="Human-readable description of the gap")
    process_id: Optional[str] = Field(default=None, description="Process where the gap was found")
    step_id: Optional[str] = Field(default=None, description="Step where the gap was found")
    agent_name: Optional[str] = Field(default=None, description="Agent involved (if applicable)")


class RosterValidationResult(BaseModel):
    """Result of validating whether the roster fully covers the team's needs."""

    is_fully_staffed: bool = Field(..., description="True when no gaps were found")
    agent_count: int = Field(default=0)
    process_count: int = Field(default=0)
    gaps: list[RosterGap] = Field(default_factory=list)
    summary: str = Field(default="", description="Human-readable summary of the validation")


class RecommendedAgent(BaseModel):
    """An agent recommended for a process step."""

    agent_name: str = Field(..., description="Agent identifier")
    source: str = Field(
        ..., description="Where the recommendation came from: 'registry' or 'roster'"
    )
    role: str = Field(default="", description="Suggested role for this agent")
    skills: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    match_score: float = Field(default=0.0, description="Relevance score (higher is better)")


class RecommendAgentsResponse(BaseModel):
    """Response for agent recommendation requests."""

    step_id: str
    step_name: str
    recommended_agents: list[RecommendedAgent] = Field(default_factory=list)


class AgentEnvProvisionSummary(BaseModel):
    """Status of an Agent Provisioning run for a process step agent."""

    stable_key: str
    process_id: str
    step_id: str
    agent_name: str
    provisioning_agent_id: str = Field(
        ...,
        description="agent_id passed to agent_provisioning_team",
    )
    status: str
    error_message: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""


# ---------------------------------------------------------------------------
# Interactive Testing Mode models
# ---------------------------------------------------------------------------


class TestChatSession(BaseModel):
    """An interactive chat test session with a specific agent."""

    session_id: str
    team_id: str
    agent_name: str
    session_name: str = Field(default="")
    created_at: str = ""
    updated_at: str = ""


class TestChatMessage(BaseModel):
    """A single message in a test chat session."""

    message_id: str
    session_id: str
    role: str = Field(..., pattern=r"^(user|assistant)$")
    content: str
    rating: Optional[MessageRating] = None
    created_at: str = ""


class TestChatSessionDetail(BaseModel):
    """Session with full message history and starter prompts."""

    session: TestChatSession
    messages: list[TestChatMessage] = Field(default_factory=list)
    suggested_prompts: list[str] = Field(default_factory=list)


class AgentQualityScore(BaseModel):
    """Aggregated quality score for an agent based on chat ratings."""

    agent_name: str
    total_rated: int = 0
    thumbs_up: int = 0
    thumbs_down: int = 0
    score_pct: float = Field(default=0.0, description="Percentage of positive ratings (0-100)")


class PipelineStepResult(BaseModel):
    """Output of a single step within a pipeline test run."""

    step_id: str
    step_name: str = ""
    agent_name: str = ""
    input: str = ""
    output: str = ""
    status: str = "pending"


class TestPipelineRun(BaseModel):
    """An end-to-end pipeline test run."""

    run_id: str
    team_id: str
    process_id: str
    status: PipelineRunStatus = PipelineRunStatus.RUNNING
    current_step_id: Optional[str] = None
    initial_input: Optional[str] = None
    step_results: list[PipelineStepResult] = Field(default_factory=list)
    human_prompt: Optional[str] = None
    error: Optional[str] = None
    started_at: str = ""
    finished_at: Optional[str] = None


# Test mode request DTOs


class SetTeamModeRequest(BaseModel):
    mode: TeamMode


class CreateTestChatSessionRequest(BaseModel):
    agent_name: str = Field(..., min_length=1)


class RenameTestChatSessionRequest(BaseModel):
    session_name: str = Field(..., min_length=1, max_length=200)


class SendTestChatMessageRequest(BaseModel):
    content: str = Field(..., min_length=1)


class RateMessageRequest(BaseModel):
    rating: MessageRating


class StartPipelineRunRequest(BaseModel):
    process_id: str = Field(..., min_length=1)
    initial_input: Optional[str] = None


class SubmitPipelineInputRequest(BaseModel):
    input: str = Field(..., min_length=1)
