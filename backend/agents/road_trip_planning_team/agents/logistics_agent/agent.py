"""Logistics Agent: handles accommodations, timing, packing lists, and practical travel tips."""

from __future__ import annotations

import logging

from llm_service import LLMClient, LLMJsonParseError

from ...models import LogisticsPlan, RoutePlan, TravelerGroupProfile, TripRequest

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert travel logistics planner who specializes in road trips.
Given a route plan, group profile, and trip details, provide practical logistics for each stop and overall trip guidance.

Output JSON with:
- stop_logistics: array of logistics objects for each overnight stop, each with:
  - location: string
  - accommodation: object with:
    - name: string (example property name or type recommendation)
    - accommodation_type: string (hotel, motel, campground, airbnb, rv_park, resort)
    - approximate_cost_per_night: string or null (e.g. "$80-120", "$$")
    - amenities: array of strings (pool, pet-friendly, kitchen, WiFi, parking, etc.)
    - booking_tips: string (when to book, what to look for, area to stay in)
  - timing_notes: string (when to arrive, how much time to allow)
  - parking_notes: string or null
  - local_transport: string or null (do you need a car? walkable? rideshare?)
- packing_suggestions: array of strings (items specific to this trip's activities and vehicle type)
- travel_tips: array of strings (general road trip tips for this group and route)
- budget_estimate: string (rough overall trip cost estimate, e.g. "$1500-2500 for 2 adults, 5 days")

Be practical and specific. Consider the vehicle type, group needs, and budget level.
Output only valid JSON."""


class LogisticsAgent:
    """Produces accommodation recommendations, packing lists, and travel tips for the trip."""

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def run(
        self,
        route: RoutePlan,
        group_profile: TravelerGroupProfile,
        trip: TripRequest,
    ) -> LogisticsPlan:
        """Generate logistics plan for the road trip."""
        overnight_stops = [s for s in route.ordered_stops if s.recommended_nights > 0]
        stops_summary = "\n".join(
            f"  - {s.location}: {s.recommended_nights} night(s), driving ~{s.estimated_driving_hours or '?'} hrs from {s.driving_from or 'previous stop'}"
            for s in overnight_stops
        )

        prompt = (
            f"Route: {route.route_summary}\n"
            f"Overnight stops:\n{stops_summary}\n\n"
            f"Group: {group_profile.group_description}\n"
            f"Needs: {', '.join(group_profile.combined_needs) or 'none'}\n"
            f"Budget level: {trip.budget_level}\n"
            f"Vehicle: {trip.vehicle_type}\n"
            f"Total trip days: {route.suggested_total_days}\n"
            f"Activity pace: {group_profile.activity_pace}\n"
            f"Accessibility requirements: {', '.join(group_profile.accessibility_requirements) or 'none'}\n\n"
            "Plan accommodations and logistics for this road trip. Output JSON."
        )

        try:
            data = self.llm.complete_json(
                prompt,
                temperature=0.3,
                system_prompt=SYSTEM_PROMPT,
                expected_keys=["stop_logistics", "packing_suggestions", "travel_tips"],
                think=True,
            )
        except LLMJsonParseError as e:
            logger.warning("LogisticsAgent JSON parse failed: %s", e)
            return LogisticsPlan(
                packing_suggestions=["Sunscreen", "Snacks", "Water bottles", "First aid kit"],
                travel_tips=["Start driving early to beat traffic", "Download offline maps"],
            )

        return LogisticsPlan(
            stop_logistics=data.get("stop_logistics") or [],
            packing_suggestions=data.get("packing_suggestions") or [],
            travel_tips=data.get("travel_tips") or [],
            budget_estimate=data.get("budget_estimate") or "",
        )
