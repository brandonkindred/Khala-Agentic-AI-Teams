"""Models for the Personal Assistant Orchestrator."""

from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, Field


class Intent(BaseModel):
    """Classified intent from user message."""

    primary: str
    secondary: List[str] = Field(default_factory=list)
    confidence: float = 0.0
    entities: Dict[str, Any] = Field(default_factory=dict)


class OrchestratorRequest(BaseModel):
    """Request to the orchestrator."""

    user_id: str
    message: str
    context: Dict[str, Any] = Field(default_factory=dict)


class OrchestratorResponse(BaseModel):
    """Response from the orchestrator."""

    message: str
    intent: Intent
    actions_taken: List[str] = Field(default_factory=list)
    data: Dict[str, Any] = Field(default_factory=dict)
    follow_up_suggestions: List[str] = Field(default_factory=list)
    profile_updates: List[Dict[str, Any]] = Field(default_factory=list)


class AgentAction(BaseModel):
    """An action taken by an agent."""

    agent: str
    action: str
    result: Dict[str, Any] = Field(default_factory=dict)
    success: bool = True
