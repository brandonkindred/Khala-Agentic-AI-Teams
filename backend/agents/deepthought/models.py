"""Pydantic models for the Deepthought recursive agent system."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


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


# Allow recursive reference resolution
AgentResult.model_rebuild()


class DeepthoughtRequest(BaseModel):
    """Top-level request to the Deepthought system."""

    message: str = Field(..., min_length=1, description="The user's question or message")
    max_depth: int = Field(default=10, ge=1, le=10, description="Maximum recursion depth (1-10)")
    conversation_history: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Prior conversation turns as [{role, content}, ...]",
    )


class DeepthoughtResponse(BaseModel):
    """Top-level response from the Deepthought system."""

    answer: str = Field(..., description="Final synthesised answer")
    agent_tree: AgentResult = Field(..., description="Full tree of agent decomposition")
    total_agents_spawned: int = Field(default=0, description="Number of agents created")
    max_depth_reached: int = Field(default=0, description="Deepest recursion level used")
