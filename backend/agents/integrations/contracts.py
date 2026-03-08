from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class IntegrationOperation(str, Enum):
    DISCOVER = "discover"
    READ = "read"
    CREATE = "create"
    UPDATE = "update"
    NOTIFY = "notify"
    SCHEDULE = "schedule"
    TOOL_LIST = "tool_list"
    TOOL_ADD = "tool_add"
    TOOL_UPDATE = "tool_update"


class IntegrationRequest(BaseModel):
    """Provider-neutral request envelope for any tool integration."""

    operation: IntegrationOperation
    capability: str = Field(..., description="High-level capability such as ticketing.create")
    payload: dict[str, Any] = Field(default_factory=dict)
    actor_id: str
    purpose: str
    data_classification: str = "internal"
    idempotency_key: str | None = None
    approval_token: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class IntegrationResponse(BaseModel):
    """Normalized response returned by a provider adapter."""

    provider: str
    status: str
    result: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] | None = None
