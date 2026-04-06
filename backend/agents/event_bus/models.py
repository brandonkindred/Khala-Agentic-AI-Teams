"""
Event schema definitions for the Strands event bus.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class EventType(str, Enum):
    """Standard event types published by teams and infrastructure."""

    # Job lifecycle
    JOB_STARTED = "team.job.started"
    JOB_PROGRESS = "team.job.progress"
    JOB_COMPLETED = "team.job.completed"
    JOB_FAILED = "team.job.failed"
    JOB_CANCELLED = "team.job.cancelled"

    # Artifact lifecycle
    ARTIFACT_CREATED = "team.artifact.created"
    ARTIFACT_UPDATED = "team.artifact.updated"

    # LLM events
    LLM_CALL_COMPLETED = "llm.call.completed"
    LLM_RATE_LIMITED = "llm.rate_limited"

    # System events
    TEAM_HEALTH_CHANGED = "system.team.health_changed"


@dataclass
class Event:
    """A structured event in the Strands event bus."""

    event_type: EventType
    payload: Dict[str, Any]
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    source_team: Optional[str] = None
    job_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "event_id": self.event_id,
            "event_type": self.event_type.value if isinstance(self.event_type, EventType) else self.event_type,
            "timestamp": self.timestamp,
            "payload": self.payload,
        }
        if self.source_team:
            d["source_team"] = self.source_team
        if self.job_id:
            d["job_id"] = self.job_id
        return d

    def to_sse(self) -> str:
        """Format as a Server-Sent Event string."""
        import json

        event_name = self.event_type.value if isinstance(self.event_type, EventType) else self.event_type
        data = json.dumps(self.to_dict())
        return f"event: {event_name}\ndata: {data}\n\n"
