"""Activities Expert Agent: recommends activities and dining for each stop on the route."""

from __future__ import annotations

import json
import logging
from typing import List

from strands import Agent

from llm_service import get_strands_model

from ...models import RoutePlan, StopActivities, TravelerGroupProfile, TripRequest

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a travel activities expert with encyclopedic knowledge of destinations across the United States and beyond.
Given a location and a group travel profile, recommend specific activities and dining tailored to that group.

Output JSON with:
- location: string
- activities: array of activity objects, each with:
  - name: string
  - description: string (1-2 sentences, why it's great for this group)
  - duration_hours: number or null
  - activity_type: string (sightseeing, outdoor, museum, entertainment, shopping, relaxation, adventure)
  - address: string or null (area/neighborhood if not exact address)
  - tips: array of strings (insider tips, best time to visit, parking, etc.)
  - good_for: array of strings (age groups or interest types this suits)
  - approximate_cost: string or null (e.g. "free", "$10-20/person", "$$")
- dining: array of dining objects with the same shape but activity_type always "dining"
- tips: array of strings (general tips for this location)

Include 4-6 activities and 2-3 dining options per location. Tailor everything to the group's interests, needs, and age groups.
Respect accessibility requirements if present. Output only valid JSON."""


class ActivitiesExpertAgent:
    """Generates tailored activity and dining recommendations for all route stops."""

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
        route: RoutePlan,
        group_profile: TravelerGroupProfile,
        trip: TripRequest,
    ) -> List[StopActivities]:
        """Generate activity recommendations for each stop on the route."""
        results: List[StopActivities] = []

        for stop in route.ordered_stops:
            if stop.stop_type in ("start", "end") and stop.recommended_nights == 0:
                results.append(StopActivities(location=stop.location))
                continue

            activities = self._get_stop_activities(stop.location, group_profile, trip)
            results.append(activities)

        return results

    def _get_stop_activities(
        self,
        location: str,
        group_profile: TravelerGroupProfile,
        trip: TripRequest,
    ) -> StopActivities:
        """Get activities for a single stop."""
        prompt = (
            f"Location: {location}\n\n"
            f"Group profile:\n"
            f"  Description: {group_profile.group_description}\n"
            f"  Interests: {', '.join(group_profile.combined_interests) or 'general'}\n"
            f"  Age groups: {', '.join(group_profile.age_groups_present) or 'adults'}\n"
            f"  Activity pace: {group_profile.activity_pace}\n"
            f"  Food requirements: {', '.join(group_profile.food_requirements) or 'none'}\n"
            f"  Accessibility needs: {', '.join(group_profile.accessibility_requirements) or 'none'}\n"
            f"  Budget level: {trip.budget_level}\n\n"
            f"Recommend activities and dining for this group at {location}. Output JSON."
        )

        try:
            result = self._agent(prompt)
            raw = str(result).strip()
            data = json.loads(raw)
        except Exception as e:
            logger.warning("ActivitiesExpertAgent JSON parse failed for %s: %s", location, e)
            return StopActivities(location=location)

        return StopActivities(
            location=location,
            activities=data.get("activities") or [],
            dining=data.get("dining") or [],
            tips=data.get("tips") or [],
        )
