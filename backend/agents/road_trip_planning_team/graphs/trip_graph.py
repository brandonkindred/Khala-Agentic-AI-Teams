"""Road trip planning sequential graph.

Five-agent pipeline:
  traveler_profiler → route_planner → activities_expert → logistics → itinerary_composer

Each agent receives the accumulated context from all upstream agents plus the
original trip request task, producing progressively richer outputs that the
final itinerary composer synthesizes into a complete day-by-day plan.
"""

from __future__ import annotations

from strands.multiagent.graph import Graph

from shared_graph import build_agent, build_sequential

# ---------------------------------------------------------------------------
# Agent system prompts (adapted from per-agent modules for graph context flow)
# ---------------------------------------------------------------------------

_TRAVELER_PROFILER_PROMPT = """\
You are an expert travel consultant specializing in group travel dynamics.
You will receive a road trip request with traveler details. Synthesize a unified group travel profile.

Output JSON with:
- group_description: string summarizing who is in this group
- combined_interests: array of strings
- combined_needs: array of strings (accessibility, dietary, etc.)
- age_groups_present: array of strings (e.g. ["child", "adult", "senior"])
- activity_pace: string — one of: "relaxed", "moderate", "active"
- food_requirements: array of strings
- accessibility_requirements: array of strings
- travel_style_notes: string — concise guidance for downstream planners

Be practical: if a group has both young children and adults, lean toward a pace both can enjoy.
Output only valid JSON."""

_ROUTE_PLANNER_PROMPT = """\
You are an expert road trip route planner with deep knowledge of geography and highways.
You will receive a trip request and a group travel profile from a previous agent.
Plan the optimal ordered route through all required stops.

Add intermediate overnight stops if driving distances exceed 5-6 hours per day.

Output JSON with:
- ordered_stops: array of stop objects, each with:
  - location: string (city/state or landmark)
  - driving_from: string or null
  - estimated_driving_miles: number or null
  - estimated_driving_hours: number or null
  - recommended_nights: integer
  - stop_type: one of "start", "destination", "overnight", "landmark", "end"
  - notes: string
- total_driving_miles: number or null
- total_driving_hours: number or null
- route_summary: string
- suggested_total_days: integer

Make the route geographically logical. Output only valid JSON."""

_ACTIVITIES_EXPERT_PROMPT = """\
You are a travel activities and dining expert.
You will receive a trip request, group profile, and route plan from previous agents.
For each stop on the route, recommend tailored activities and dining options.

Output JSON with:
- stops: array matching the route's ordered_stops, each with:
  - location: string
  - activities: array of objects with name, description, duration_hours, suitable_for, cost_level
  - dining: array of objects with name, cuisine, price_range, notes
  - day_plan_notes: string (tips for scheduling the day)

Match activity intensity to the group's pace. Consider ages, interests, and needs.
Output only valid JSON."""

_LOGISTICS_PROMPT = """\
You are a travel logistics specialist handling accommodations, packing, and practical tips.
You will receive a trip request, group profile, and route plan from previous agents.

Output JSON with:
- accommodations: array matching route stops, each with:
  - location: string
  - options: array of objects with name, type, price_range, notes
- packing_list: array of strings (essentials for the trip)
- travel_tips: array of strings (practical advice for this specific trip)
- emergency_info: string (general safety/emergency guidance)

Consider the group's needs, budget level, and vehicle type.
Output only valid JSON."""

_ITINERARY_COMPOSER_PROMPT = """\
You are a travel writer and itinerary designer.
You will receive a complete set of trip planning data from previous agents:
group profile, route plan, per-stop activities, and logistics.

Synthesize everything into a polished, complete day-by-day itinerary.

Output JSON with:
- title: string (catchy trip title)
- summary: string (2-3 sentence overview)
- total_days: integer
- days: array of day objects, each with:
  - day_number: integer
  - date_label: string (e.g. "Day 1")
  - location: string
  - driving: object with from_location, to_location, miles, hours (null if no driving)
  - activities: array of objects with time, name, description
  - meals: array of objects with meal_type, venue, notes
  - accommodation: object with name, type, notes (null if last day)
  - day_notes: string
- packing_list: array of strings
- travel_tips: array of strings
- budget_estimate: string (rough total estimate)

Create an engaging, practical itinerary the travelers can follow directly.
Output only valid JSON."""


def build_trip_graph() -> Graph:
    """Build the 5-agent sequential road trip planning graph.

    Returns
    -------
    Graph
        traveler_profiler → route_planner → activities_expert → logistics → itinerary_composer
    """
    return build_sequential(
        stages=[
            ("traveler_profiler", build_agent(
                name="traveler_profiler",
                system_prompt=_TRAVELER_PROFILER_PROMPT,
                agent_key="road_trip_planning",
                description="Synthesizes group travel profile from traveler details",
            )),
            ("route_planner", build_agent(
                name="route_planner",
                system_prompt=_ROUTE_PLANNER_PROMPT,
                agent_key="road_trip_planning",
                description="Plans optimal ordered route through all stops",
            )),
            ("activities_expert", build_agent(
                name="activities_expert",
                system_prompt=_ACTIVITIES_EXPERT_PROMPT,
                agent_key="road_trip_planning",
                description="Recommends activities and dining at each stop",
            )),
            ("logistics", build_agent(
                name="logistics",
                system_prompt=_LOGISTICS_PROMPT,
                agent_key="road_trip_planning",
                description="Plans accommodations, packing, and practical logistics",
            )),
            ("itinerary_composer", build_agent(
                name="itinerary_composer",
                system_prompt=_ITINERARY_COMPOSER_PROMPT,
                agent_key="road_trip_planning",
                description="Assembles final day-by-day itinerary from all inputs",
            )),
        ],
        graph_id="road_trip_pipeline",
        execution_timeout=600.0,
        node_timeout=180.0,
    )
