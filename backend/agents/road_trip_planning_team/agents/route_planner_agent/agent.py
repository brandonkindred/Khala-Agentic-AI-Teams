"""Route Planner Agent: plans the optimal road trip route through required stops."""

from __future__ import annotations

import logging

from llm_service import LLMClient, LLMJsonParseError

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

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def run(self, trip: TripRequest, group_profile: TravelerGroupProfile) -> RoutePlan:
        """Generate an ordered route plan from the trip request."""
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
            data = self.llm.complete_json(
                prompt,
                temperature=0.3,
                system_prompt=SYSTEM_PROMPT,
                expected_keys=["ordered_stops", "route_summary", "suggested_total_days"],
            )
        except LLMJsonParseError as e:
            logger.warning("RoutePlannerAgent JSON parse failed: %s", e)
            stops = [RouteStop(location=trip.start_location, stop_type="start")]
            for s in trip.required_stops:
                stops.append(RouteStop(location=s, stop_type="destination", recommended_nights=1))
            stops.append(RouteStop(location=end, stop_type="end"))
            return RoutePlan(
                ordered_stops=stops,
                suggested_total_days=trip.trip_duration_days or len(trip.required_stops) * 2,
            )

        raw_stops = data.get("ordered_stops") or []
        stops = []
        for s in raw_stops:
            if isinstance(s, dict):
                stops.append(RouteStop.model_validate(s))

        return RoutePlan(
            ordered_stops=stops,
            total_driving_miles=data.get("total_driving_miles"),
            total_driving_hours=data.get("total_driving_hours"),
            route_summary=data.get("route_summary", ""),
            suggested_total_days=data.get("suggested_total_days", trip.trip_duration_days or 7),
        )
