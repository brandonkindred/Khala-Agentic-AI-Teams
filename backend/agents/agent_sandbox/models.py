"""Pydantic models for the agent sandbox lifecycle."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class SandboxStatus(str, Enum):
    """Lifecycle states for a per-team sandbox."""

    COLD = "cold"
    WARMING = "warming"
    WARM = "warm"
    ERROR = "error"


class SandboxHandle(BaseModel):
    """Handle returned by SandboxManager.ensure_warm()."""

    team: str
    status: SandboxStatus
    url: str | None = Field(
        default=None,
        description="Base URL of the sandbox service (e.g. http://localhost:8200). None until warm.",
    )
    service_name: str = Field(..., description="Compose service name, e.g. 'blogging-sandbox'.")
    container_name: str = Field(
        ..., description="Docker container name, e.g. 'khala-sandbox-blogging'."
    )
    host_port: int = Field(..., description="Host port the sandbox service is bound to.")
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    idle_seconds: int | None = None
    error: str | None = None


class SandboxState(BaseModel):
    """On-disk state for a single sandbox, checkpointed to JSON."""

    team: str
    service_name: str
    container_name: str
    host_port: int
    status: SandboxStatus
    created_at: datetime
    last_used_at: datetime
    error: str | None = None
