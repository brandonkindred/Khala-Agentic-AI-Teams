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
    agents: list[ProcessStepAgent] = Field(default_factory=list, description="Agents responsible for this step")
    next_steps: list[str] = Field(default_factory=list, description="step_ids that follow this step")
    condition: Optional[str] = Field(default=None, description="Condition expression for decision steps")


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
# Agentic Team
# ---------------------------------------------------------------------------

class AgenticTeam(BaseModel):
    """Top-level team definition containing processes."""

    team_id: str = Field(..., description="Unique id (UUID)")
    name: str = Field(..., description="Team display name")
    description: str = Field(default="", description="Short description of what the team does")
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
