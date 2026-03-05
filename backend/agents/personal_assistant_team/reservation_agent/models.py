"""Models for the Reservation Agent."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from ..models import ReservationType


class MakeReservationRequest(BaseModel):
    """Request to make a reservation."""

    user_id: str
    reservation_type: ReservationType
    venue_name: Optional[str] = None
    datetime: datetime
    party_size: int = 1
    preferences: Dict[str, Any] = Field(default_factory=dict)
    notes: str = ""


class SearchVenuesRequest(BaseModel):
    """Request to search for venues."""

    user_id: str
    venue_type: str
    location: Optional[str] = None
    cuisine: Optional[str] = None
    price_range: Optional[str] = None
    max_results: int = 10


class VenueResult(BaseModel):
    """A venue search result."""

    name: str
    address: Optional[str] = None
    phone: Optional[str] = None
    rating: Optional[float] = None
    price_range: Optional[str] = None
    cuisine_type: Optional[str] = None
    url: Optional[str] = None
    description: str = ""
    relevance_score: float = 0.0
    matching_preferences: List[str] = Field(default_factory=list)


class ReservationResult(BaseModel):
    """Result of a reservation attempt."""

    success: bool
    reservation_id: Optional[str] = None
    confirmation_number: Optional[str] = None
    venue_name: str
    datetime: datetime
    party_size: int
    notes: str = ""
    status: str = "pending"
    action_required: Optional[str] = None


class CancelReservationRequest(BaseModel):
    """Request to cancel a reservation."""

    user_id: str
    reservation_id: str
    reason: str = ""


class ListReservationsRequest(BaseModel):
    """Request to list reservations."""

    user_id: str
    include_past: bool = False
    reservation_type: Optional[ReservationType] = None
