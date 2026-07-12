"""Route Planner Agent: plans the optimal road trip route through required stops."""

from __future__ import annotations

import json
import logging

from strands import Agent

from llm_service import get_strands_model

from ...models import RoutePlan, RouteStop, TravelerGroupProfile, TripRequest

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert road trip route planner with deep knowledge of North American geography and highways.
Given a trip request (start, required stops, end, duration, vehicle type), plan the optimal ordered route.

You may add intermediate overnight stops if the driving distances are too long for one day (aim for max 5-6 hours driving per day).

Output JSON with:
- ordered_stops: array of stop objects, each with:
  - location: string (city, state or landmark name)
  - driving_from: string or null (previous location)
  - estimated_driving_miles: number or null
  - estimated_driving_hours: number or null
  - recommended_nights: integer (nights to stay, 0 for pass-through stops)
  - stop_type: string — one of: "start", "destination", "overnight", "landmark", "end"
  - notes: string (why this stop, what makes it special, any route notes)
- total_driving_miles: number or null
- total_driving_hours: number or null
- route_summary: string (brief narrative of the overall route)
- suggested_total_days: integer

Make the route logical geographically. Consider scenic byways if the group prefers scenic routes.
Output only valid JSON."""


class RoutePlannerAgent:
    """Plans the optimal ordered route for a road trip."""

    def __init__(self, llm=None) -> None:
        self._agent = (
            llm
            if llm is not None
            else Agent(
                model=get_strands_model("road_trip_planning"),
                system_prompt=SYSTEM_PROMPT,
            )
        )

    def run(self, trip: TripRequest, group_profile: TravelerGroupProfile) -> RoutePlan:
        """Generate an ordered route plan from the trip request.

        Postconditions:
            - The returned ``RoutePlan`` always has a non-empty ``ordered_stops``
              covering start → required stops → end. If the LLM output can't be
              parsed, or is valid JSON that yields no usable stops (e.g. ``{}``
              from a refusal), a derived fallback route is returned so a request
              with required stops never composes into an empty itinerary.
        """
        end = trip.end_location or trip.start_location
        prompt = (
            f"Start: {trip.start_location}\n"
            f"Required stops (in any order): {', '.join(trip.required_stops) or 'none'}\n"
            f"End: {end}\n"
            f"Trip duration: {trip.trip_duration_days or 'flexible'} days\n"
            f"Vehicle: {trip.vehicle_type}\n"
            f"Group activity pace: {group_profile.activity_pace}\n"
            f"Travel preferences: {', '.join(trip.preferences) or 'none'}\n"
            f"Group needs: {', '.join(group_profile.combined_needs) or 'none'}\n\n"
            "Plan the optimal road trip route. Include all required stops. "
            "Add intermediate overnight stops as needed. Output the route JSON."
        )

        try:
            result = self._agent(prompt)
            raw = str(result).strip()
            data = json.loads(raw)
            raw_stops = data.get("ordered_stops") or []
            stops = [RouteStop.model_validate(s) for s in raw_stops if isinstance(s, dict)]
        except Exception as e:
            # Covers both a malformed LLM response (JSON parse failure) and a
            # syntactically-valid-but-schema-invalid stop (pydantic ValidationError)
            # — either way the postcondition below holds: fall back rather than raise.
            logger.warning("RoutePlannerAgent JSON parse/validation failed: %s", e)
            return self._fallback_route(trip, end)

        if not stops or not self._covers_required_stops(stops, trip):
            # Valid JSON that yields no usable stops (e.g. an empty object from a
            # refusal) or that silently drops a must-visit stop — fall back to a
            # route covering start → required stops → end rather than composing an
            # itinerary that is empty or missing a required stop.
            logger.warning("RoutePlannerAgent route missing required stops; using fallback route")
            return self._fallback_route(trip, end)

        return RoutePlan(
            ordered_stops=stops,
            total_driving_miles=data.get("total_driving_miles"),
            total_driving_hours=data.get("total_driving_hours"),
            route_summary=data.get("route_summary", ""),
            suggested_total_days=data.get("suggested_total_days", trip.trip_duration_days or 7),
        )

    def _covers_required_stops(self, stops: list[RouteStop], trip: TripRequest) -> bool:
        """Return True if every ``trip.required_stops`` location is represented.

        Matching is case-insensitive and bidirectional-substring (a required
        ``"Yosemite"`` matches a planned ``"Yosemite National Park"`` and vice
        versa) to tolerate the LLM naming a stop more or less verbosely than the
        request.

        Preconditions:
            - ``stops`` is the parsed, non-empty route.

        Postconditions:
            - Returns True when no required stop is missing (trivially True when
              there are no required stops).
        """
        planned = [s.location.lower() for s in stops if s.location]
        for req in trip.required_stops:
            r = req.lower()
            if not any(r in p or p in r for p in planned):
                return False
        return True

    def _fallback_route(self, trip: TripRequest, end: str) -> RoutePlan:
        """Build a minimal route covering start → required stops → end.

        Preconditions:
            - ``end`` is the resolved end location (``trip.end_location`` or the
              start location for a round trip).

        Postconditions:
            - Returns a ``RoutePlan`` whose ``ordered_stops`` begins at a
              pass-through start, includes every required stop as an overnight
              destination, and ends at a pass-through end — never empty. The
              start/end carry ``recommended_nights=0`` so the activities and
              composer steps treat them as pass-through and don't turn them into
              extra overnight days or LLM calls.
        """
        stops = [RouteStop(location=trip.start_location, stop_type="start", recommended_nights=0)]
        for s in trip.required_stops:
            stops.append(RouteStop(location=s, stop_type="destination", recommended_nights=1))
        stops.append(RouteStop(location=end, stop_type="end", recommended_nights=0))
        return RoutePlan(
            ordered_stops=stops,
            # max(1, ...): a trip with no explicit duration and no required stops
            # (start → end only) would otherwise evaluate to 0 days.
            suggested_total_days=max(1, trip.trip_duration_days or len(trip.required_stops) * 2),
        )
