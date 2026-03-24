"""Reservation Agent - manages restaurant and appointment reservations."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from ..models import Reservation, ReservationType
from ..shared.llm import JSONExtractionFailure, LLMClient
from ..shared.user_profile_store import UserProfileStore
from ..tools.web_search import WebSearchTool
from .models import (
    CancelReservationRequest,
    ListReservationsRequest,
    MakeReservationRequest,
    ReservationResult,
    SearchVenuesRequest,
    VenueResult,
)
from .prompts import PARSE_RESERVATION_PROMPT

logger = logging.getLogger(__name__)


class ReservationAgent:
    """
    Agent for managing reservations.

    Capabilities:
    - Search for restaurants and services
    - Make reservations (with user action for actual booking)
    - Track reservation history
    - Recommend venues based on preferences
    """

    def __init__(
        self,
        llm: LLMClient,
        profile_store: Optional[UserProfileStore] = None,
        storage_dir: Optional[str] = None,
    ) -> None:
        """
        Initialize the Reservation Agent.

        Args:
            llm: LLM client for recommendations
            profile_store: User profile storage
            storage_dir: Directory for reservation storage
        """
        self.llm = llm
        self.profile_store = profile_store or UserProfileStore()
        self.storage_dir = Path(
            storage_dir or os.getenv("PA_RESERVATIONS_DIR", ".agent_cache/reservations")
        )
        self.storage_dir.mkdir(parents=True, exist_ok=True)

        self.web_search = WebSearchTool()

    def _get_user_file(self, user_id: str) -> Path:
        """Get path to user's reservations file."""
        return self.storage_dir / f"{user_id}_reservations.json"

    def _load_reservations(self, user_id: str) -> List[Reservation]:
        """Load user's reservations."""
        file_path = self._get_user_file(user_id)
        if not file_path.exists():
            return []

        try:
            data = json.loads(file_path.read_text())
            return [Reservation(**r) for r in data]
        except Exception as e:
            logger.error("Failed to load reservations: %s", e)
            return []

    def _save_reservations(self, user_id: str, reservations: List[Reservation]) -> None:
        """Save user's reservations."""
        file_path = self._get_user_file(user_id)
        data = [r.model_dump(mode="json") for r in reservations]
        file_path.write_text(json.dumps(data, indent=2, default=str))

    def search_venues(self, request: SearchVenuesRequest) -> List[VenueResult]:
        """
        Search for venues matching criteria.

        Args:
            request: Search request

        Returns:
            List of matching venues
        """
        profile = self.profile_store.load_profile(request.user_id)

        location = request.location
        if not location and profile and profile.identity.address:
            location = profile.identity.address

        query_parts = [request.venue_type]
        if request.cuisine:
            query_parts.append(request.cuisine)
        if location:
            query_parts.append(f"near {location}")

        " ".join(query_parts)

        try:
            if request.venue_type.lower() in ("restaurant", "restaurants", "dining"):
                results = self.web_search.search_restaurants(
                    cuisine=request.cuisine or "popular",
                    location=location or "nearby",
                    max_results=request.max_results,
                )
            else:
                results = self.web_search.search_services(
                    service_type=request.venue_type,
                    location=location or "nearby",
                    max_results=request.max_results,
                )
        except Exception as e:
            logger.error("Venue search failed: %s", e)
            return []

        venues = []
        for result in results:
            venue = VenueResult(
                name=result.title,
                description=result.snippet,
                url=str(result.url),
            )

            venue = self._score_venue(request.user_id, venue, request)
            venues.append(venue)

        venues.sort(key=lambda v: v.relevance_score, reverse=True)
        return venues

    def _score_venue(
        self,
        user_id: str,
        venue: VenueResult,
        request: SearchVenuesRequest,
    ) -> VenueResult:
        """Score a venue based on user preferences."""
        profile = self.profile_store.load_profile(user_id)

        if not profile:
            venue.relevance_score = 0.5
            return venue

        score = 0.5
        matches = []

        if profile.preferences.cuisines_ranked:
            for cuisine in profile.preferences.cuisines_ranked[:5]:
                if cuisine.lower() in venue.description.lower():
                    score += 0.2
                    matches.append(f"Matches cuisine: {cuisine}")
                    break

        if profile.preferences.dietary_restrictions:
            for restriction in profile.preferences.dietary_restrictions:
                if restriction.lower() in venue.description.lower():
                    score += 0.1
                    matches.append(f"Accommodates: {restriction}")

        if profile.shopping.favorite_stores:
            for store in profile.shopping.favorite_stores:
                if store.lower() in venue.name.lower():
                    score += 0.15
                    matches.append(f"Favorite: {store}")

        venue.relevance_score = min(1.0, score)
        venue.matching_preferences = matches

        return venue

    def parse_reservation_request(
        self,
        user_id: str,
        text: str,
    ) -> Dict[str, Any]:
        """
        Parse a natural language reservation request.

        Args:
            user_id: The user ID
            text: Natural language request

        Returns:
            Parsed reservation details
        """
        profile = self.profile_store.load_profile(user_id)

        preferences = ""
        if profile:
            if profile.preferences.cuisines_ranked:
                preferences += (
                    f"Favorite cuisines: {', '.join(profile.preferences.cuisines_ranked[:5])}\n"
                )
            if profile.preferences.dietary_restrictions:
                preferences += (
                    f"Dietary restrictions: {', '.join(profile.preferences.dietary_restrictions)}\n"
                )

        prompt = PARSE_RESERVATION_PROMPT.format(
            request=text,
            preferences=preferences or "No specific preferences",
            current_datetime=datetime.utcnow().isoformat(),
        )

        try:
            data = self.llm.complete_json(
                prompt,
                temperature=0.1,
                expected_keys=["reservation_type", "venue_name", "datetime", "party_size", "notes"],
            )
            return data
        except JSONExtractionFailure as e:
            logger.error("Failed to parse reservation request (JSON extraction failed):\n%s", e)
            return {}
        except Exception as e:
            logger.error("Failed to parse reservation request: %s", e)
            return {}

    def make_reservation(self, request: MakeReservationRequest) -> ReservationResult:
        """
        Create a reservation record.

        Note: This creates a local record. Actual booking may require
        user action or integration with booking APIs.

        Args:
            request: Reservation request

        Returns:
            ReservationResult with status
        """
        reservations = self._load_reservations(request.user_id)

        reservation = Reservation(
            reservation_id=str(uuid4())[:8],
            reservation_type=request.reservation_type,
            venue_name=request.venue_name or "TBD",
            datetime=request.datetime,
            party_size=request.party_size,
            notes=request.notes,
            status="pending",
        )

        reservations.append(reservation)
        self._save_reservations(request.user_id, reservations)

        action_required = None
        if request.reservation_type == ReservationType.RESTAURANT:
            action_required = (
                f"Please confirm the reservation at {request.venue_name} "
                f"by calling them or using their online booking system."
            )

        return ReservationResult(
            success=True,
            reservation_id=reservation.reservation_id,
            venue_name=reservation.venue_name,
            datetime=reservation.datetime,
            party_size=reservation.party_size,
            notes=reservation.notes,
            status="pending",
            action_required=action_required,
        )

    def confirm_reservation(
        self,
        user_id: str,
        reservation_id: str,
        confirmation_number: Optional[str] = None,
    ) -> bool:
        """
        Confirm a pending reservation.

        Args:
            user_id: The user ID
            reservation_id: Reservation to confirm
            confirmation_number: External confirmation number

        Returns:
            True if confirmed
        """
        reservations = self._load_reservations(user_id)

        for res in reservations:
            if res.reservation_id == reservation_id:
                res.status = "confirmed"
                if confirmation_number:
                    res.confirmation_number = confirmation_number
                self._save_reservations(user_id, reservations)
                return True

        return False

    def cancel_reservation(self, request: CancelReservationRequest) -> bool:
        """
        Cancel a reservation.

        Args:
            request: Cancel request

        Returns:
            True if cancelled
        """
        reservations = self._load_reservations(request.user_id)

        for res in reservations:
            if res.reservation_id == request.reservation_id:
                res.status = "cancelled"
                res.notes += f"\nCancelled: {request.reason}" if request.reason else ""
                self._save_reservations(request.user_id, reservations)
                return True

        return False

    def list_reservations(self, request: ListReservationsRequest) -> List[Reservation]:
        """
        List user's reservations.

        Args:
            request: List request

        Returns:
            List of reservations
        """
        reservations = self._load_reservations(request.user_id)
        now = datetime.utcnow()

        if not request.include_past:
            reservations = [r for r in reservations if r.datetime > now]

        if request.reservation_type:
            reservations = [
                r for r in reservations if r.reservation_type == request.reservation_type
            ]

        reservations.sort(key=lambda r: r.datetime)
        return reservations

    def get_upcoming_reservations(self, user_id: str) -> List[Reservation]:
        """Get upcoming reservations."""
        return self.list_reservations(
            ListReservationsRequest(
                user_id=user_id,
                include_past=False,
            )
        )

    def recommend_restaurants(
        self,
        user_id: str,
        occasion: Optional[str] = None,
        location: Optional[str] = None,
    ) -> List[VenueResult]:
        """
        Get restaurant recommendations based on profile.

        Args:
            user_id: The user ID
            occasion: Type of occasion (date, business, casual)
            location: Preferred location

        Returns:
            List of recommended restaurants
        """
        profile = self.profile_store.load_profile(user_id)

        cuisine = None
        if profile and profile.preferences.cuisines_ranked:
            cuisine = profile.preferences.cuisines_ranked[0]

        return self.search_venues(
            SearchVenuesRequest(
                user_id=user_id,
                venue_type="restaurant",
                location=location,
                cuisine=cuisine,
            )
        )

    def create_reservation_from_text(
        self,
        user_id: str,
        text: str,
    ) -> Dict[str, Any]:
        """
        Parse text and create a reservation.

        Args:
            user_id: The user ID
            text: Natural language request

        Returns:
            Result with reservation or needs_confirmation
        """
        parsed = self.parse_reservation_request(user_id, text)

        if not parsed:
            return {
                "success": False,
                "message": "Could not understand the reservation request",
            }

        confidence = parsed.get("confidence", 0)

        if confidence < 0.7:
            return {
                "success": True,
                "needs_confirmation": True,
                "parsed": parsed,
                "message": "Please confirm the reservation details",
            }

        try:
            request = MakeReservationRequest(
                user_id=user_id,
                reservation_type=ReservationType(parsed.get("reservation_type", "restaurant")),
                venue_name=parsed.get("venue_name"),
                datetime=datetime.fromisoformat(parsed.get("datetime", "")),
                party_size=parsed.get("party_size", 1),
                notes=parsed.get("special_requests", ""),
            )

            result = self.make_reservation(request)
            return {
                "success": True,
                "reservation": result.model_dump(),
            }
        except Exception as e:
            logger.error("Failed to create reservation: %s", e)
            return {
                "success": False,
                "message": str(e),
                "parsed": parsed,
            }
