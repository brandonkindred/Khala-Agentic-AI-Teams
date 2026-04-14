"""Traveler Profiler Agent: synthesizes a group travel profile from individual traveler inputs."""

from __future__ import annotations

import json
import logging

from strands import Agent

from llm_service import get_strands_model

from ...models import TravelerGroupProfile, TripRequest

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert travel consultant specializing in group travel dynamics.
Given a list of travelers with their ages, interests, and needs, synthesize a unified group travel profile.

Output JSON with:
- group_description: string summarizing who is in this group (e.g. "family with young kids and teens")
- combined_interests: array of strings — activities/themes that will appeal across the group
- combined_needs: array of strings — requirements that must be met (accessibility, dietary, etc.)
- age_groups_present: array of strings (e.g. ["child", "adult", "senior"])
- activity_pace: string — one of: "relaxed", "moderate", "active" (based on group constraints)
- food_requirements: array of strings — dietary restrictions or food preferences for the group
- accessibility_requirements: array of strings — physical accessibility needs
- travel_style_notes: string — concise guidance for planners (e.g. "need frequent breaks, mix of active and calm")

Be practical: if a group has both young children and adults, lean toward a pace and activities both can enjoy.
Output only valid JSON."""


class TravelerProfilerAgent:
    """Produces a TravelerGroupProfile from the list of travelers in a TripRequest."""

    def __init__(self, llm=None) -> None:
        self._agent = (
            llm
            if llm is not None
            else Agent(
                model=get_strands_model("road_trip_planning"),
                system_prompt=SYSTEM_PROMPT,
            )
        )

    def run(self, trip: TripRequest) -> TravelerGroupProfile:
        """Analyze travelers and produce a group travel profile."""
        traveler_data = [t.model_dump() for t in trip.travelers]
        prompt = (
            "Travelers:\n"
            + str(traveler_data)
            + "\n\nTrip preferences: "
            + ", ".join(trip.preferences or ["none specified"])
            + "\n\nBudget level: "
            + trip.budget_level
            + "\n\nSynthesize the group travel profile JSON."
        )

        try:
            result = self._agent(prompt)
            raw = str(result).strip()
            data = json.loads(raw)
        except Exception as e:
            logger.warning("TravelerProfilerAgent JSON parse failed: %s", e)
            return TravelerGroupProfile(
                group_description="Group of travelers",
                combined_interests=[i for t in trip.travelers for i in t.interests],
                combined_needs=[n for t in trip.travelers for n in t.needs],
                age_groups_present=list({t.age_group for t in trip.travelers}),
            )

        return TravelerGroupProfile.model_validate(data)
