"""Nutrition meal planning sequential graph.

Topology::

    intake_profiler → nutritionist → meal_planner

The intake profiler analyzes client needs, the nutritionist creates a
nutrition plan, and the meal planner generates specific meal suggestions.
"""

from __future__ import annotations

from strands.multiagent.graph import Graph

from shared_graph import build_agent, build_sequential


def build_plan_graph() -> Graph:
    """Build the nutrition meal planning sequential graph."""
    return build_sequential(
        stages=[
            ("intake_profiler", build_agent(
                name="intake_profiler",
                system_prompt=(
                    "You are a nutrition intake specialist. Analyze the client's dietary needs, "
                    "allergies, lifestyle, preferences, and health goals. Produce a comprehensive "
                    "client profile with all nutritional requirements. Return structured JSON."
                ),
                agent_key="nutrition_meal_planning",
                description="Analyzes client needs and builds nutritional profile",
            )),
            ("nutritionist", build_agent(
                name="nutritionist",
                system_prompt=(
                    "You are a registered dietitian. Based on the client profile, create a "
                    "personalized nutrition plan with daily calorie targets, macronutrient ratios, "
                    "meal frequency, and any supplementation recommendations. Return structured JSON."
                ),
                agent_key="nutrition_meal_planning",
                description="Creates personalized nutrition plans",
            )),
            ("meal_planner", build_agent(
                name="meal_planner",
                system_prompt=(
                    "You are a meal planning specialist. Based on the nutrition plan and client "
                    "preferences, generate specific meal suggestions with recipes, ingredients, "
                    "prep time, and nutritional breakdown. Consider meal history to avoid repetition. "
                    "Return structured JSON with meal suggestions array."
                ),
                agent_key="nutrition_meal_planning",
                description="Generates specific meal suggestions",
            )),
        ],
        graph_id="nutrition_planning",
        execution_timeout=300.0,
        node_timeout=120.0,
    )
