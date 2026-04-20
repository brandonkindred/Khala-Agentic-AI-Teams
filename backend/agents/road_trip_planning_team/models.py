"""Pydantic models for the Road Trip Planning team."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Traveler and trip request models
# ---------------------------------------------------------------------------


class Traveler(BaseModel):
    """A single person going on the road trip."""

    name: str = ""
    age_group: str = "adult"  # child, teen, adult, senior
    interests: List[str] = Field(default_factory=list)  # hiking, museums, food, beaches, etc.
    needs: List[str] = Field(
        default_factory=list
    )  # wheelchair accessible, vegetarian, pet owner, etc.
    notes: str = ""


class TripRequest(BaseModel):
    """Input for planning a road trip itinerary."""

    start_location: str
    required_stops: List[str] = Field(default_factory=list)  # must-visit cities/landmarks
    end_location: Optional[str] = None  # if None, assumed round trip back to start
    travelers: List[Traveler] = Field(default_factory=list)
    trip_duration_days: Optional[int] = None  # agents infer if not provided
    budget_level: str = "moderate"  # budget, moderate, luxury
    travel_start_date: Optional[str] = None  # ISO date string, e.g. "2024-06-15"
    vehicle_type: str = "car"  # car, suv, rv, motorcycle, van
    preferences: List[str] = Field(default_factory=list)  # scenic routes, avoid highways, etc.


# ---------------------------------------------------------------------------
# Internal intermediate models used between agents
# ---------------------------------------------------------------------------


class TravelerGroupProfile(BaseModel):
    """Synthesized profile of the traveler group produced by TravelerProfilerAgent."""

    group_description: str = ""
    combined_interests: List[str] = Field(default_factory=list)
    combined_needs: List[str] = Field(default_factory=list)
    age_groups_present: List[str] = Field(default_factory=list)
    activity_pace: str = "moderate"  # relaxed, moderate, active
    food_requirements: List[str] = Field(default_factory=list)
    accessibility_requirements: List[str] = Field(default_factory=list)
    travel_style_notes: str = ""


class RouteStop(BaseModel):
    """One stop on the planned route."""

    location: str = ""
    driving_from: Optional[str] = None
    estimated_driving_miles: Optional[float] = None
    estimated_driving_hours: Optional[float] = None
    recommended_nights: int = 1
    stop_type: str = "destination"  # start, destination, overnight, landmark, end
    notes: str = ""


class RoutePlan(BaseModel):
    """Ordered sequence of stops produced by RoutePlannerAgent."""

    ordered_stops: List[RouteStop] = Field(default_factory=list)
    total_driving_miles: Optional[float] = None
    total_driving_hours: Optional[float] = None
    route_summary: str = ""
    suggested_total_days: int = 7


class StopActivities(BaseModel):
    """Activities and dining recommendations for one stop."""

    location: str = ""
    activities: List[Dict[str, Any]] = Field(default_factory=list)
    dining: List[Dict[str, Any]] = Field(default_factory=list)
    tips: List[str] = Field(default_factory=list)


class LogisticsPlan(BaseModel):
    """Accommodations, timing, and practical logistics per stop."""

    stop_logistics: List[Dict[str, Any]] = Field(default_factory=list)
    packing_suggestions: List[str] = Field(default_factory=list)
    travel_tips: List[str] = Field(default_factory=list)
    budget_estimate: str = ""


# ---------------------------------------------------------------------------
# Final itinerary output models
# ---------------------------------------------------------------------------


class Activity(BaseModel):
    """A single activity or dining experience on the itinerary."""

    name: str = ""
    description: str = ""
    duration_hours: Optional[float] = None
    activity_type: str = ""  # sightseeing, outdoor, dining, museum, rest, driving
    address: Optional[str] = None
    tips: List[str] = Field(default_factory=list)
    good_for: List[str] = Field(default_factory=list)  # age groups or interests this suits
    approximate_cost: Optional[str] = None


class Accommodation(BaseModel):
    """Lodging for one night."""

    name: str = ""
    accommodation_type: str = ""  # hotel, motel, campground, airbnb, rv_park
    address: Optional[str] = None
    approximate_cost_per_night: Optional[str] = None
    amenities: List[str] = Field(default_factory=list)
    booking_tips: str = ""


class DayPlan(BaseModel):
    """A single day's plan on the road trip."""

    day_number: int = 1
    date: Optional[str] = None
    location: str = ""
    driving_from: Optional[str] = None
    driving_distance_miles: Optional[float] = None
    driving_time_hours: Optional[float] = None
    driving_notes: str = ""
    morning_activities: List[Activity] = Field(default_factory=list)
    afternoon_activities: List[Activity] = Field(default_factory=list)
    evening_activities: List[Activity] = Field(default_factory=list)
    meals: List[Activity] = Field(default_factory=list)
    accommodation: Optional[Accommodation] = None
    day_summary: str = ""
    day_tips: List[str] = Field(default_factory=list)


class TripItinerary(BaseModel):
    """Complete road trip itinerary — the final output of the planning team."""

    title: str = ""
    overview: str = ""
    total_days: int = 0
    total_driving_miles: Optional[float] = None
    route_summary: List[str] = Field(default_factory=list)
    traveler_highlights: str = ""  # what makes this plan special for this group
    days: List[DayPlan] = Field(default_factory=list)
    travel_tips: List[str] = Field(default_factory=list)
    packing_suggestions: List[str] = Field(default_factory=list)
    budget_estimate: str = ""
    generated_at: Optional[str] = None


# ---------------------------------------------------------------------------
# API request/response wrappers
# ---------------------------------------------------------------------------


class PlanTripRequest(BaseModel):
    """Body for POST /plan."""

    trip: TripRequest
