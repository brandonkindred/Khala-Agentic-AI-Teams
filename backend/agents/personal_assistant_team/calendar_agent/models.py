"""Models for the Calendar Agent."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class CreateEventRequest(BaseModel):
    """Request to create a calendar event."""

    user_id: str
    title: str
    start_time: datetime
    end_time: Optional[datetime] = None
    duration_minutes: int = 60
    location: Optional[str] = None
    description: str = ""
    attendees: List[str] = Field(default_factory=list)
    reminders: List[int] = Field(default_factory=lambda: [30])


class ListEventsRequest(BaseModel):
    """Request to list calendar events."""

    user_id: str
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    limit: int = 50


class ScheduleRequest(BaseModel):
    """Request to find and schedule an event."""

    user_id: str
    title: str
    duration_minutes: int = 60
    preferred_date: Optional[datetime] = None
    preferred_time_range: Optional[str] = None
    attendees: List[str] = Field(default_factory=list)
    constraints: Dict[str, Any] = Field(default_factory=dict)


class ScheduleSuggestion(BaseModel):
    """A suggested time slot for scheduling."""

    start_time: datetime
    end_time: datetime
    conflicts: List[str] = Field(default_factory=list)
    score: float = 1.0
    reason: str = ""


class EventFromTextRequest(BaseModel):
    """Request to create event from natural language."""

    user_id: str
    text: str
    source: str = "conversation"


class EventFromTextResult(BaseModel):
    """Result of parsing event from text."""

    events: List[Dict[str, Any]]
    ambiguities: List[str] = Field(default_factory=list)
    needs_confirmation: bool = False
