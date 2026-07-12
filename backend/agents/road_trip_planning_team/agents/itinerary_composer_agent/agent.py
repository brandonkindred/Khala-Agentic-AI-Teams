"""Itinerary Composer Agent: assembles all specialist inputs into a polished day-by-day itinerary."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import List

from strands import Agent

from llm_service import get_strands_model

from ...models import (
    Accommodation,
    Activity,
    DayPlan,
    LogisticsPlan,
    RoutePlan,
    StopActivities,
    TravelerGroupProfile,
    TripItinerary,
    TripRequest,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a master travel itinerary composer. Given a route plan, activities, logistics, and group profile,
assemble a complete day-by-day road trip itinerary.

Output JSON with:
- title: string (catchy trip title, e.g. "Pacific Coast Highway Adventure")
- overview: string (2-3 sentences describing the trip's highlights and character)
- total_days: integer
- total_driving_miles: number or null
- route_summary: array of strings (ordered location names)
- traveler_highlights: string (what makes this itinerary perfect for THIS group)
- days: array of day plan objects, each with:
  - day_number: integer (1-based)
  - date: string or null (ISO date if start_date provided)
  - location: string
  - driving_from: string or null
  - driving_distance_miles: number or null
  - driving_time_hours: number or null
  - driving_notes: string (scenic route tips, rest stops, etc.)
  - morning_activities: array of activity objects
  - afternoon_activities: array of activity objects
  - evening_activities: array of activity objects
  - meals: array of activity objects (breakfast, lunch, dinner recommendations)
  - accommodation: object or null with name, accommodation_type, approximate_cost_per_night, amenities, booking_tips
  - day_summary: string (1-2 sentence narrative of the day)
  - day_tips: array of strings
- travel_tips: array of strings
- packing_suggestions: array of strings
- budget_estimate: string

Each activity object has: name, description, duration_hours, activity_type, address, tips (array), good_for (array), approximate_cost.

Balance the days well: don't overload any single day. Place longer drives on shorter activity days.
Make the itinerary feel curated and personal for this specific group.
Output only valid JSON."""


class ItineraryComposerAgent:
    """Assembles the final day-by-day itinerary from all specialist agent outputs."""

    def __init__(self, llm=None) -> None:
        self._agent = (
            llm
            if llm is not None
            else Agent(
                model=get_strands_model("road_trip_planning"),
                system_prompt=SYSTEM_PROMPT,
            )
        )

    def run(
        self,
        trip: TripRequest,
        group_profile: TravelerGroupProfile,
        route: RoutePlan,
        activities_per_stop: List[StopActivities],
        logistics: LogisticsPlan,
    ) -> TripItinerary:
        """Compose the final itinerary from all specialist inputs."""
        # Build a compact summary of all inputs for the LLM. activities_per_stop
        # is positionally aligned with route.ordered_stops (ActivitiesExpertAgent
        # appends exactly one StopActivities per route stop, in order — see its
        # own postcondition), so a positional zip is both simpler and correct
        # even when trip.required_stops contains a duplicate location (a
        # location-keyed lookup would silently attribute the first match's
        # activities to every same-named stop).
        stops_info = []
        for stop, stop_acts in zip(route.ordered_stops, activities_per_stop):
            stops_info.append(
                {
                    "location": stop.location,
                    "stop_type": stop.stop_type,
                    "recommended_nights": stop.recommended_nights,
                    "driving_from": stop.driving_from,
                    "driving_miles": stop.estimated_driving_miles,
                    "driving_hours": stop.estimated_driving_hours,
                    "notes": stop.notes,
                    "activities": stop_acts.activities[:5],
                    "dining": stop_acts.dining[:3],
                    "location_tips": stop_acts.tips[:3],
                    "logistics": next(
                        (
                            lg
                            for lg in logistics.stop_logistics
                            if lg.get("location") == stop.location
                        ),
                        {},
                    ),
                }
            )

        prompt = (
            f"Trip: {trip.start_location} → {', '.join(trip.required_stops)} → {trip.end_location or trip.start_location}\n"
            f"Duration: {route.suggested_total_days} days\n"
            f"Start date: {trip.travel_start_date or 'not specified'}\n"
            f"Vehicle: {trip.vehicle_type}\n"
            f"Budget: {trip.budget_level}\n\n"
            f"Group: {group_profile.group_description}\n"
            f"Interests: {', '.join(group_profile.combined_interests)}\n"
            f"Pace: {group_profile.activity_pace}\n"
            f"Needs: {', '.join(group_profile.combined_needs) or 'none'}\n\n"
            f"Route stops with activities and logistics:\n"
            + json.dumps(stops_info, indent=2)
            + f"\n\nPacking suggestions: {json.dumps(logistics.packing_suggestions)}\n"
            f"Travel tips: {json.dumps(logistics.travel_tips)}\n"
            f"Budget estimate: {logistics.budget_estimate}\n\n"
            "Compose the complete day-by-day itinerary JSON. Make it feel personalized and exciting for this group."
        )

        try:
            result = self._agent(prompt)
            raw = str(result).strip()
            data = json.loads(raw)

            days = []
            for d in data.get("days") or []:
                if not isinstance(d, dict):
                    continue
                day = DayPlan(
                    day_number=d.get("day_number", 1),
                    date=d.get("date"),
                    location=d.get("location", ""),
                    driving_from=d.get("driving_from"),
                    driving_distance_miles=d.get("driving_distance_miles"),
                    driving_time_hours=d.get("driving_time_hours"),
                    driving_notes=d.get("driving_notes", ""),
                    morning_activities=self._parse_activities(d.get("morning_activities")),
                    afternoon_activities=self._parse_activities(d.get("afternoon_activities")),
                    evening_activities=self._parse_activities(d.get("evening_activities")),
                    meals=self._parse_activities(d.get("meals")),
                    accommodation=self._parse_accommodation(d.get("accommodation")),
                    day_summary=d.get("day_summary", ""),
                    day_tips=d.get("day_tips") or [],
                )
                days.append(day)
            days = self._ensure_nonempty_days(days, route)

            return TripItinerary(
                title=data.get("title", "Road Trip Itinerary"),
                overview=data.get("overview", ""),
                total_days=data.get("total_days", route.suggested_total_days),
                total_driving_miles=data.get("total_driving_miles", route.total_driving_miles),
                route_summary=data.get("route_summary")
                or [s.location for s in route.ordered_stops],
                traveler_highlights=data.get("traveler_highlights", ""),
                days=days,
                travel_tips=data.get("travel_tips") or logistics.travel_tips,
                packing_suggestions=data.get("packing_suggestions")
                or logistics.packing_suggestions,
                budget_estimate=data.get("budget_estimate") or logistics.budget_estimate,
                generated_at=datetime.now(timezone.utc).isoformat(),
            )
        except Exception as e:
            # Covers both a malformed LLM response (JSON parse failure) and a
            # syntactically-valid-but-schema-invalid day/itinerary field
            # (pydantic ValidationError) — either way, fall back rather than
            # raise. (A valid response with an empty/missing "days" list does
            # NOT reach here — _ensure_nonempty_days above normalizes that case
            # in place.)
            logger.warning("ItineraryComposerAgent JSON parse/validation failed: %s", e)
            return self._build_fallback_itinerary(trip, route, group_profile, logistics)

    def _parse_activities(self, raw: object) -> List[Activity]:
        if not isinstance(raw, list):
            return []
        result = []
        for item in raw:
            if isinstance(item, dict):
                result.append(
                    Activity(
                        name=item.get("name", ""),
                        description=item.get("description", ""),
                        duration_hours=item.get("duration_hours"),
                        activity_type=item.get("activity_type", ""),
                        address=item.get("address"),
                        tips=item.get("tips") or [],
                        good_for=item.get("good_for") or [],
                        approximate_cost=item.get("approximate_cost"),
                    )
                )
        return result

    def _parse_accommodation(self, raw: object) -> Accommodation | None:
        if not isinstance(raw, dict):
            return None
        return Accommodation(
            name=raw.get("name", ""),
            accommodation_type=raw.get("accommodation_type", ""),
            address=raw.get("address"),
            approximate_cost_per_night=raw.get("approximate_cost_per_night"),
            amenities=raw.get("amenities") or [],
            booking_tips=raw.get("booking_tips", ""),
        )

    def _ensure_nonempty_days(self, days: List[DayPlan], route: RoutePlan) -> List[DayPlan]:
        """Guarantee at least one day when the route has stops.

        Shared by both ``run()``'s LLM-success path and
        ``_build_fallback_itinerary`` so the "never empty when the route has
        stops" guarantee lives in one place rather than being reactively
        patched at each call site.

        Postconditions:
            - Returns ``days`` unchanged if non-empty. If empty and
              ``route.ordered_stops`` is non-empty (e.g. every stop is a
              pass-through start/end, via ``RoutePlannerAgent._fallback_route``),
              returns a list with one synthesized same-day entry instead of an
              empty list paired with ``total_days >= 1``.
        """
        if days or not route.ordered_stops:
            return days
        return [
            DayPlan(
                day_number=1,
                location=route.ordered_stops[0].location,
                day_summary=f"Day trip from {route.ordered_stops[0].location}",
            )
        ]

    def _build_fallback_itinerary(
        self,
        trip: TripRequest,
        route: RoutePlan,
        group_profile: TravelerGroupProfile,
        logistics: LogisticsPlan,
    ) -> TripItinerary:
        """Minimal fallback itinerary if LLM fails.

        Postconditions:
            - Returns a ``TripItinerary`` whose ``days`` is never empty — see
              ``_ensure_nonempty_days``.
        """
        days = []
        day_num = 1
        for stop in route.ordered_stops:
            if stop.recommended_nights == 0 and stop.stop_type in ("start", "end"):
                continue
            for night in range(max(1, stop.recommended_nights)):
                logistics_entry = next(
                    (lg for lg in logistics.stop_logistics if lg.get("location") == stop.location),
                    {},
                )
                acc_data = logistics_entry.get("accommodation") or {}
                accommodation = (
                    Accommodation(
                        name=acc_data.get("name", "Local accommodation"),
                        accommodation_type=acc_data.get("accommodation_type", "hotel"),
                        amenities=acc_data.get("amenities") or [],
                        booking_tips=acc_data.get("booking_tips", ""),
                    )
                    if night == 0
                    else None
                )

                days.append(
                    DayPlan(
                        day_number=day_num,
                        location=stop.location,
                        driving_from=stop.driving_from if day_num == 1 or night == 0 else None,
                        driving_distance_miles=stop.estimated_driving_miles if night == 0 else None,
                        driving_time_hours=stop.estimated_driving_hours if night == 0 else None,
                        day_summary=f"Day {day_num} in {stop.location}",
                        accommodation=accommodation,
                    )
                )
                day_num += 1

        days = self._ensure_nonempty_days(days, route)

        return TripItinerary(
            title="Road Trip Itinerary",
            overview=f"A {route.suggested_total_days}-day road trip through {', '.join(s.location for s in route.ordered_stops)}.",
            total_days=route.suggested_total_days,
            total_driving_miles=route.total_driving_miles,
            route_summary=[s.location for s in route.ordered_stops],
            days=days,
            travel_tips=logistics.travel_tips,
            packing_suggestions=logistics.packing_suggestions,
            budget_estimate=logistics.budget_estimate,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )
