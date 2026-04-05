"""Pydantic models for the Deepthought recursive agent system."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Decomposition strategies
# ---------------------------------------------------------------------------


class DecompositionStrategy(str, Enum):
    """How to break a question into sub-agent tasks."""

    AUTO = "auto"
    BY_DISCIPLINE = "by_discipline"  # factual: decompose by knowledge domain
    BY_CONCERN = "by_concern"  # design: decompose by feasibility, cost, risk, etc.
    BY_OPTION = "by_option"  # comparison: decompose by each option to evaluate
    BY_PERSPECTIVE = "by_perspective"  # opinion/policy: decompose by stakeholder viewpoint
    NONE = "none"  # force direct answer, no decomposition


# ---------------------------------------------------------------------------
# Streaming events
# ---------------------------------------------------------------------------


class AgentEventType(str, Enum):
    """Types of events emitted during recursive execution."""

    AGENT_SPAWNED = "agent_spawned"
    AGENT_ANALYSING = "agent_analysing"
    AGENT_ANSWERING = "agent_answering"
    AGENT_DECOMPOSING = "agent_decomposing"
    AGENT_DELIBERATING = "agent_deliberating"
    AGENT_SYNTHESISING = "agent_synthesising"
    AGENT_COMPLETE = "agent_complete"
    BUDGET_WARNING = "budget_warning"
    KNOWLEDGE_REUSED = "knowledge_reused"


class AgentEvent(BaseModel):
    """A single event emitted during agent execution, for SSE streaming."""

    event_type: AgentEventType
    agent_id: str
    agent_name: str
    depth: int
    detail: str = ""


# ---------------------------------------------------------------------------
# Shared knowledge base
# ---------------------------------------------------------------------------


class KnowledgeEntry(BaseModel):
    """A single finding stored in the shared knowledge base."""

    agent_id: str
    agent_name: str
    focus_question: str
    finding: str
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    tags: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Core agent models
# ---------------------------------------------------------------------------


class SkillRequirement(BaseModel):
    """A specialist skill/knowledge area identified during query analysis."""

    name: str = Field(..., description="Short identifier, e.g. 'quantum_physics_expert'")
    description: str = Field(..., description="What this specialist knows or does")
    focus_question: str = Field(
        ..., description="The specific sub-question for this specialist to answer"
    )
    reasoning: str = Field(..., description="Why this specialist is needed for the query")


class QueryAnalysis(BaseModel):
    """Result of analysing a user query or sub-query."""

    summary: str = Field(..., description="Concise restatement of the question")
    can_answer_directly: bool = Field(
        ..., description="True when the agent can answer without spawning sub-agents"
    )
    direct_answer: str | None = Field(
        None, description="The answer text when can_answer_directly is True"
    )
    confidence: float = Field(
        default=0.0, ge=0.0, le=1.0, description="Confidence in the direct answer (0-1)"
    )
    skill_requirements: list[SkillRequirement] = Field(
        default_factory=list,
        description="Specialist skills needed if the agent cannot answer directly (max 5)",
    )


class AgentSpec(BaseModel):
    """Specification for a dynamically created sub-agent."""

    agent_id: str = Field(..., description="Unique identifier (UUID)")
    name: str = Field(..., description="Human-readable specialist name")
    role_description: str = Field(..., description="What this agent specialises in")
    focus_question: str = Field(..., description="The question this agent must answer")
    depth: int = Field(..., ge=0, description="Current recursion depth")
    parent_id: str | None = Field(None, description="Parent agent ID (None for root)")


class AgentResult(BaseModel):
    """Result from a single agent's work, forming a recursive tree."""

    agent_id: str
    agent_name: str
    depth: int
    focus_question: str
    answer: str = Field(..., description="This agent's synthesised answer")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    child_results: list[AgentResult] = Field(
        default_factory=list, description="Results from sub-agents"
    )
    was_decomposed: bool = Field(default=False, description="Whether this agent spawned children")
    deliberation_notes: str | None = Field(
        None, description="Notes from the deliberation phase (contradiction resolution, follow-ups)"
    )
    reused_from_cache: bool = Field(
        default=False, description="True if this result was served from the knowledge cache"
    )


# Allow recursive reference resolution
AgentResult.model_rebuild()


# ---------------------------------------------------------------------------
# Request / Response
# ---------------------------------------------------------------------------


class DeepthoughtRequest(BaseModel):
    """Top-level request to the Deepthought system."""

    message: str = Field(..., min_length=1, description="The user's question or message")
    max_depth: int = Field(default=10, ge=1, le=10, description="Maximum recursion depth (1-10)")
    conversation_history: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Prior conversation turns as [{role, content}, ...]",
    )
    decomposition_strategy: DecompositionStrategy = Field(
        default=DecompositionStrategy.AUTO,
        description="Strategy for decomposing the question into sub-agents",
    )


class DeepthoughtResponse(BaseModel):
    """Top-level response from the Deepthought system."""

    answer: str = Field(..., description="Final synthesised answer")
    agent_tree: AgentResult = Field(..., description="Full tree of agent decomposition")
    total_agents_spawned: int = Field(default=0, description="Number of agents created")
    max_depth_reached: int = Field(default=0, description="Deepest recursion level used")
    knowledge_entries: list[KnowledgeEntry] = Field(
        default_factory=list, description="All findings stored in the shared knowledge base"
    )
    events: list[AgentEvent] = Field(
        default_factory=list, description="Chronological log of agent activity events"
    )
