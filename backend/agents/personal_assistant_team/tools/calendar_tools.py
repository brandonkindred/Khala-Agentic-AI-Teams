"""Calendar tools for Google Calendar and Outlook integration."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from ..models import CalendarEvent
from ..shared.credential_store import CredentialStore

logger = logging.getLogger(__name__)


class CalendarToolError(Exception):
    """Raised when calendar operations fail."""


class CalendarToolAgent:
    """
    Tool agent for calendar operations.
    
    Supports Google Calendar and Outlook Calendar via OAuth.
    """

    def __init__(self, credential_store: Optional[CredentialStore] = None) -> None:
        """Initialize the calendar tool agent."""
        self.credential_store = credential_store or CredentialStore()

    def list_events(
        self,
        user_id: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        limit: int = 50,
    ) -> List[CalendarEvent]:
        """
        List calendar events.
        
        Args:
            user_id: The user ID
            start_date: Start of date range (defaults to now)
            end_date: End of date range (defaults to 30 days from now)
            limit: Maximum events to return
            
        Returns:
            List of CalendarEvent objects
        """
        cred_data = self.credential_store.get_calendar_credentials(user_id)
        if not cred_data:
            raise CalendarToolError("No calendar credentials found for user")
        
        provider = cred_data.get("provider", "google")
        
        if provider == "google":
            return self._list_google_events(cred_data, start_date, end_date, limit)
        else:
            raise CalendarToolError(f"Unsupported calendar provider: {provider}")

    def _list_google_events(
        self,
        cred_data: Dict[str, Any],
        start_date: Optional[datetime],
        end_date: Optional[datetime],
        limit: int,
    ) -> List[CalendarEvent]:
        """List events from Google Calendar."""
        try:
            from googleapiclient.discovery import build
            from google.oauth2.credentials import Credentials
        except ImportError:
            raise CalendarToolError("Google API client not installed")
        
        try:
            creds = Credentials(
                token=cred_data.get("access_token"),
                refresh_token=cred_data.get("refresh_token"),
                token_uri="https://oauth2.googleapis.com/token",
                client_id=os.getenv("GOOGLE_CLIENT_ID"),
                client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
            )
            
            service = build("calendar", "v3", credentials=creds)
            
            time_min = (start_date or datetime.utcnow()).isoformat() + "Z"
            time_max = (end_date or datetime.utcnow() + timedelta(days=30)).isoformat() + "Z"
            
            events_result = service.events().list(
                calendarId="primary",
                timeMin=time_min,
                timeMax=time_max,
                maxResults=limit,
                singleEvents=True,
                orderBy="startTime",
            ).execute()
            
            events = []
            for item in events_result.get("items", []):
                start = item.get("start", {})
                end = item.get("end", {})
                
                start_time = start.get("dateTime", start.get("date", ""))
                end_time = end.get("dateTime", end.get("date", ""))
                
                events.append(CalendarEvent(
                    event_id=item.get("id", ""),
                    title=item.get("summary", ""),
                    description=item.get("description", ""),
                    start_time=datetime.fromisoformat(start_time.replace("Z", "+00:00")),
                    end_time=datetime.fromisoformat(end_time.replace("Z", "+00:00")),
                    location=item.get("location"),
                    attendees=[a.get("email", "") for a in item.get("attendees", [])],
                    is_all_day="date" in start,
                    source="google_calendar",
                ))
            
            return events
        except Exception as e:
            logger.error("Failed to list Google Calendar events: %s", e)
            raise CalendarToolError(f"Failed to list events: {e}") from e

    def create_event(
        self,
        user_id: str,
        event: CalendarEvent,
    ) -> str:
        """
        Create a calendar event.
        
        Args:
            user_id: The user ID
            event: The event to create
            
        Returns:
            Created event ID
        """
        cred_data = self.credential_store.get_calendar_credentials(user_id)
        if not cred_data:
            raise CalendarToolError("No calendar credentials found for user")
        
        provider = cred_data.get("provider", "google")
        
        if provider == "google":
            return self._create_google_event(cred_data, event)
        else:
            raise CalendarToolError(f"Unsupported calendar provider: {provider}")

    def _create_google_event(
        self,
        cred_data: Dict[str, Any],
        event: CalendarEvent,
    ) -> str:
        """Create an event in Google Calendar."""
        try:
            from googleapiclient.discovery import build
            from google.oauth2.credentials import Credentials
        except ImportError:
            raise CalendarToolError("Google API client not installed")
        
        try:
            creds = Credentials(
                token=cred_data.get("access_token"),
                refresh_token=cred_data.get("refresh_token"),
                token_uri="https://oauth2.googleapis.com/token",
                client_id=os.getenv("GOOGLE_CLIENT_ID"),
                client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
            )
            
            service = build("calendar", "v3", credentials=creds)
            
            event_body = {
                "summary": event.title,
                "description": event.description,
                "start": {
                    "dateTime": event.start_time.isoformat(),
                    "timeZone": "UTC",
                },
                "end": {
                    "dateTime": event.end_time.isoformat(),
                    "timeZone": "UTC",
                },
            }
            
            if event.location:
                event_body["location"] = event.location
            
            if event.attendees:
                event_body["attendees"] = [{"email": e} for e in event.attendees]
            
            if event.reminders:
                event_body["reminders"] = {
                    "useDefault": False,
                    "overrides": [
                        {"method": "popup", "minutes": m} for m in event.reminders
                    ],
                }
            
            result = service.events().insert(
                calendarId="primary",
                body=event_body,
            ).execute()
            
            logger.info("Created calendar event: %s", result.get("id"))
            return result.get("id", "")
        except Exception as e:
            logger.error("Failed to create Google Calendar event: %s", e)
            raise CalendarToolError(f"Failed to create event: {e}") from e

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
            event_id: The event ID to update
            updates: Fields to update
            
        Returns:
            True if successful
        """
        cred_data = self.credential_store.get_calendar_credentials(user_id)
        if not cred_data:
            raise CalendarToolError("No calendar credentials found for user")
        
        try:
            from googleapiclient.discovery import build
            from google.oauth2.credentials import Credentials
        except ImportError:
            raise CalendarToolError("Google API client not installed")
        
        try:
            creds = Credentials(
                token=cred_data.get("access_token"),
                refresh_token=cred_data.get("refresh_token"),
                token_uri="https://oauth2.googleapis.com/token",
                client_id=os.getenv("GOOGLE_CLIENT_ID"),
                client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
            )
            
            service = build("calendar", "v3", credentials=creds)
            
            event = service.events().get(
                calendarId="primary",
                eventId=event_id,
            ).execute()
            
            if "title" in updates:
                event["summary"] = updates["title"]
            if "description" in updates:
                event["description"] = updates["description"]
            if "location" in updates:
                event["location"] = updates["location"]
            if "start_time" in updates:
                event["start"]["dateTime"] = updates["start_time"].isoformat()
            if "end_time" in updates:
                event["end"]["dateTime"] = updates["end_time"].isoformat()
            
            service.events().update(
                calendarId="primary",
                eventId=event_id,
                body=event,
            ).execute()
            
            return True
        except Exception as e:
            logger.error("Failed to update event: %s", e)
            raise CalendarToolError(f"Failed to update event: {e}") from e

    def delete_event(
        self,
        user_id: str,
        event_id: str,
    ) -> bool:
        """
        Delete a calendar event.
        
        Args:
            user_id: The user ID
            event_id: The event ID to delete
            
        Returns:
            True if successful
        """
        cred_data = self.credential_store.get_calendar_credentials(user_id)
        if not cred_data:
            raise CalendarToolError("No calendar credentials found for user")
        
        try:
            from googleapiclient.discovery import build
            from google.oauth2.credentials import Credentials
        except ImportError:
            raise CalendarToolError("Google API client not installed")
        
        try:
            creds = Credentials(
                token=cred_data.get("access_token"),
                refresh_token=cred_data.get("refresh_token"),
                token_uri="https://oauth2.googleapis.com/token",
                client_id=os.getenv("GOOGLE_CLIENT_ID"),
                client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
            )
            
            service = build("calendar", "v3", credentials=creds)
            
            service.events().delete(
                calendarId="primary",
                eventId=event_id,
            ).execute()
            
            return True
        except Exception as e:
            logger.error("Failed to delete event: %s", e)
            raise CalendarToolError(f"Failed to delete event: {e}") from e

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
            start_time: Start of the time slot
            end_time: End of the time slot
            
        Returns:
            True if the slot is free
        """
        events = self.list_events(
            user_id,
            start_date=start_time - timedelta(hours=1),
            end_date=end_time + timedelta(hours=1),
        )
        
        for event in events:
            if event.start_time < end_time and event.end_time > start_time:
                return False
        
        return True

    def find_free_slots(
        self,
        user_id: str,
        date: datetime,
        duration_minutes: int = 60,
        work_hours: tuple = (9, 17),
    ) -> List[Dict[str, datetime]]:
        """
        Find free time slots on a given day.
        
        Args:
            user_id: The user ID
            date: The date to check
            duration_minutes: Required slot duration
            work_hours: Working hours (start, end)
            
        Returns:
            List of available slots with start/end times
        """
        start_of_day = date.replace(hour=work_hours[0], minute=0, second=0, microsecond=0)
        end_of_day = date.replace(hour=work_hours[1], minute=0, second=0, microsecond=0)
        
        events = self.list_events(
            user_id,
            start_date=start_of_day,
            end_date=end_of_day,
        )
        
        events.sort(key=lambda e: e.start_time)
        
        free_slots = []
        current_time = start_of_day
        
        for event in events:
            if event.start_time > current_time:
                slot_duration = (event.start_time - current_time).total_seconds() / 60
                if slot_duration >= duration_minutes:
                    free_slots.append({
                        "start": current_time,
                        "end": event.start_time,
                    })
            current_time = max(current_time, event.end_time)
        
        if current_time < end_of_day:
            remaining = (end_of_day - current_time).total_seconds() / 60
            if remaining >= duration_minutes:
                free_slots.append({
                    "start": current_time,
                    "end": end_of_day,
                })
        
        return free_slots
