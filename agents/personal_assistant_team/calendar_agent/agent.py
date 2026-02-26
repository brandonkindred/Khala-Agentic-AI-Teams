"""Calendar Agent - manages calendar events and scheduling."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from uuid import uuid4

from ..models import CalendarEvent
from ..shared.credential_store import CredentialStore
from ..shared.llm import LLMClient, JSONExtractionFailure
from ..shared.user_profile_store import UserProfileStore
from ..tools.calendar_tools import CalendarToolAgent
from .models import (
    CreateEventRequest,
    EventFromTextRequest,
    EventFromTextResult,
    ListEventsRequest,
    ScheduleRequest,
    ScheduleSuggestion,
)
from .prompts import CONFLICT_RESOLUTION_PROMPT, PARSE_EVENT_PROMPT, SCHEDULE_SUGGESTION_PROMPT

logger = logging.getLogger(__name__)


class CalendarAgent:
    """
    Agent for managing calendar events.
    
    Capabilities:
    - Create, update, delete events
    - Extract events from text/emails
    - Smart scheduling with conflict detection
    - Find free time slots
    """

    def __init__(
        self,
        llm: LLMClient,
        credential_store: Optional[CredentialStore] = None,
        profile_store: Optional[UserProfileStore] = None,
    ) -> None:
        """
        Initialize the Calendar Agent.
        
        Args:
            llm: LLM client for parsing and suggestions
            credential_store: Credential storage
            profile_store: User profile storage
        """
        self.llm = llm
        self.credential_store = credential_store or CredentialStore()
        self.profile_store = profile_store or UserProfileStore()
        self.calendar_tool = CalendarToolAgent(self.credential_store)

    def has_credentials(self, user_id: str) -> bool:
        """Check if user has calendar credentials."""
        return self.credential_store.has_calendar_credentials(user_id)

    def list_events(self, request: ListEventsRequest) -> List[CalendarEvent]:
        """
        List calendar events.
        
        Args:
            request: List events request
            
        Returns:
            List of calendar events
        """
        return self.calendar_tool.list_events(
            user_id=request.user_id,
            start_date=request.start_date,
            end_date=request.end_date,
            limit=request.limit,
        )

    def create_event(self, request: CreateEventRequest) -> str:
        """
        Create a calendar event.
        
        Args:
            request: Create event request
            
        Returns:
            Created event ID
        """
        end_time = request.end_time
        if end_time is None:
            end_time = request.start_time + timedelta(minutes=request.duration_minutes)
        
        event = CalendarEvent(
            event_id=str(uuid4())[:8],
            title=request.title,
            description=request.description,
            start_time=request.start_time,
            end_time=end_time,
            location=request.location,
            attendees=request.attendees,
            reminders=request.reminders,
        )
        
        return self.calendar_tool.create_event(request.user_id, event)

    def update_event(
        self,
        user_id: str,
        event_id: str,
        updates: Dict[str, Any],
    ) -> bool:
        """
        Update a calendar event.
        
        Args:
            user_id: The user ID
            event_id: Event to update
            updates: Fields to update
            
        Returns:
            True if successful
        """
        return self.calendar_tool.update_event(user_id, event_id, updates)

    def delete_event(self, user_id: str, event_id: str) -> bool:
        """
        Delete a calendar event.
        
        Args:
            user_id: The user ID
            event_id: Event to delete
            
        Returns:
            True if successful
        """
        return self.calendar_tool.delete_event(user_id, event_id)

    def parse_event_from_text(self, request: EventFromTextRequest) -> EventFromTextResult:
        """
        Parse event details from natural language.
        
        Args:
            request: Parse request with text
            
        Returns:
            Parsed event details
        """
        profile = self.profile_store.load_profile(request.user_id)
        timezone = "UTC"
        if profile and profile.identity.timezone:
            timezone = profile.identity.timezone
        
        prompt = PARSE_EVENT_PROMPT.format(
            text=request.text,
            current_datetime=datetime.utcnow().isoformat(),
            timezone=timezone,
        )
        
        try:
            data = self.llm.complete_json(
                prompt,
                temperature=0.1,
                expected_keys=["events", "ambiguities"],
            )
        except JSONExtractionFailure as e:
            logger.error("Failed to parse event from text (JSON extraction failed):\n%s", e)
            return EventFromTextResult(
                events=[],
                needs_confirmation=True,
                ambiguities=["JSON extraction failed - could not parse event details"],
            )
        except Exception as e:
            logger.error("Failed to parse event from text: %s", e)
            return EventFromTextResult(events=[], needs_confirmation=True)
        
        events = data.get("events", [])
        ambiguities = data.get("ambiguities", [])
        
        needs_confirmation = len(ambiguities) > 0 or any(
            e.get("confidence", 0) < 0.8 for e in events
        )
        
        return EventFromTextResult(
            events=events,
            ambiguities=ambiguities,
            needs_confirmation=needs_confirmation,
        )

    def create_event_from_text(
        self,
        user_id: str,
        text: str,
        auto_create: bool = False,
    ) -> Dict[str, Any]:
        """
        Parse and optionally create an event from text.
        
        Args:
            user_id: The user ID
            text: Natural language event description
            auto_create: Create without confirmation if confident
            
        Returns:
            Result with created event or pending confirmation
        """
        result = self.parse_event_from_text(EventFromTextRequest(
            user_id=user_id,
            text=text,
        ))
        
        if not result.events:
            return {
                "success": False,
                "message": "Could not parse any events from the text",
            }
        
        if auto_create and not result.needs_confirmation:
            created_ids = []
            for event_data in result.events:
                try:
                    start_time = datetime.fromisoformat(event_data.get("start_time", ""))
                    end_time = event_data.get("end_time")
                    if end_time:
                        end_time = datetime.fromisoformat(end_time)
                    
                    request = CreateEventRequest(
                        user_id=user_id,
                        title=event_data.get("title", "Untitled Event"),
                        start_time=start_time,
                        end_time=end_time,
                        duration_minutes=event_data.get("duration_minutes") or 60,
                        location=event_data.get("location"),
                        description=event_data.get("description", ""),
                        attendees=event_data.get("attendees", []),
                    )
                    
                    event_id = self.create_event(request)
                    created_ids.append(event_id)
                except Exception as e:
                    logger.error("Failed to create event: %s", e)
            
            return {
                "success": True,
                "created_event_ids": created_ids,
                "events": result.events,
            }
        
        return {
            "success": True,
            "needs_confirmation": True,
            "parsed_events": result.events,
            "ambiguities": result.ambiguities,
        }

    def find_free_slots(
        self,
        user_id: str,
        date: datetime,
        duration_minutes: int = 60,
    ) -> List[Dict[str, datetime]]:
        """
        Find free time slots on a given day.
        
        Args:
            user_id: The user ID
            date: Date to check
            duration_minutes: Required slot duration
            
        Returns:
            List of available slots
        """
        return self.calendar_tool.find_free_slots(
            user_id=user_id,
            date=date,
            duration_minutes=duration_minutes,
        )

    def check_availability(
        self,
        user_id: str,
        start_time: datetime,
        end_time: datetime,
    ) -> bool:
        """
        Check if a time slot is available.
        
        Args:
            user_id: The user ID
            start_time: Start of slot
            end_time: End of slot
            
        Returns:
            True if slot is free
        """
        return self.calendar_tool.check_availability(user_id, start_time, end_time)

    def suggest_schedule(self, request: ScheduleRequest) -> List[ScheduleSuggestion]:
        """
        Suggest optimal times for scheduling.
        
        Args:
            request: Schedule request
            
        Returns:
            List of suggested time slots
        """
        date = request.preferred_date or datetime.utcnow() + timedelta(days=1)
        
        available_slots = self.find_free_slots(
            user_id=request.user_id,
            date=date,
            duration_minutes=request.duration_minutes,
        )
        
        if not available_slots:
            for i in range(1, 8):
                check_date = date + timedelta(days=i)
                available_slots = self.find_free_slots(
                    user_id=request.user_id,
                    date=check_date,
                    duration_minutes=request.duration_minutes,
                )
                if available_slots:
                    break
        
        if not available_slots:
            return []
        
        slots_text = "\n".join([
            f"- {s['start'].isoformat()} to {s['end'].isoformat()}"
            for s in available_slots
        ])
        
        prompt = SCHEDULE_SUGGESTION_PROMPT.format(
            title=request.title,
            duration_minutes=request.duration_minutes,
            attendees=", ".join(request.attendees) if request.attendees else "None",
            preferred_date=date.strftime("%Y-%m-%d"),
            preferred_time_range=request.preferred_time_range or "business hours",
            constraints=request.constraints,
            available_slots=slots_text,
            schedule_patterns="No historical data available",
        )
        
        try:
            data = self.llm.complete_json(
                prompt,
                temperature=0.3,
                expected_keys=["suggestions"],
            )
        except JSONExtractionFailure as e:
            logger.error("Failed to generate schedule suggestions (JSON extraction failed):\n%s", e)
            suggestions = []
            for slot in available_slots[:3]:
                suggestions.append(ScheduleSuggestion(
                    start_time=slot["start"],
                    end_time=slot["start"] + timedelta(minutes=request.duration_minutes),
                    score=0.7,
                    reason="Available slot (fallback due to JSON extraction failure)",
                ))
            return suggestions
        except Exception as e:
            logger.error("Failed to generate schedule suggestions: %s", e)
            suggestions = []
            for slot in available_slots[:3]:
                suggestions.append(ScheduleSuggestion(
                    start_time=slot["start"],
                    end_time=slot["start"] + timedelta(minutes=request.duration_minutes),
                    score=0.7,
                    reason="Available slot",
                ))
            return suggestions
        
        suggestions = []
        for item in data.get("suggestions", [])[:3]:
            try:
                suggestions.append(ScheduleSuggestion(
                    start_time=datetime.fromisoformat(item.get("start_time", "")),
                    end_time=datetime.fromisoformat(item.get("end_time", "")),
                    score=float(item.get("score", 0.5)),
                    reason=item.get("reason", ""),
                ))
            except Exception:
                continue
        
        return suggestions

    def get_today_events(self, user_id: str) -> List[CalendarEvent]:
        """Get events for today."""
        today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow = today + timedelta(days=1)
        
        return self.list_events(ListEventsRequest(
            user_id=user_id,
            start_date=today,
            end_date=tomorrow,
        ))

    def get_upcoming_events(
        self,
        user_id: str,
        days: int = 7,
    ) -> List[CalendarEvent]:
        """Get events for the next N days."""
        now = datetime.utcnow()
        end = now + timedelta(days=days)
        
        return self.list_events(ListEventsRequest(
            user_id=user_id,
            start_date=now,
            end_date=end,
        ))
