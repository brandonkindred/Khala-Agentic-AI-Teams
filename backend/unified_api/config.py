"""
Configuration for the Unified API Server.
"""

import os
from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class TeamConfig:
    """Configuration for an agent team API."""

    name: str
    prefix: str
    description: str
    enabled: bool = True
    tags: List[str] = field(default_factory=list)


# Default port for the unified API server
DEFAULT_PORT = int(os.getenv("UNIFIED_API_PORT", "8080"))
DEFAULT_HOST = os.getenv("UNIFIED_API_HOST", "0.0.0.0")

# Security gateway: when True (default), scan requests to team APIs before forwarding.
SECURITY_GATEWAY_ENABLED = os.getenv("SECURITY_GATEWAY_ENABLED", "true").lower() in ("true", "1", "yes")

# Temporal (software engineering team workflows). When TEMPORAL_ADDRESS is set, SE team uses Temporal instead of threads.
TEMPORAL_ADDRESS = os.getenv("TEMPORAL_ADDRESS", "").strip() or None
TEMPORAL_NAMESPACE = os.getenv("TEMPORAL_NAMESPACE", "default").strip()
TEMPORAL_TASK_QUEUE = os.getenv("TEMPORAL_TASK_QUEUE", "software-engineering").strip()

# Team configurations with route prefixes
TEAM_CONFIGS: Dict[str, TeamConfig] = {
    "blogging": TeamConfig(
        name="Blogging",
        prefix="/api/blogging",
        description="Blog research, review, draft, copy-edit, and publication pipeline",
        tags=["blogging", "content"],
    ),
    "software_engineering": TeamConfig(
        name="Software Engineering",
        prefix="/api/software-engineering",
        description="Full dev team simulation: architecture, planning, coding, review",
        tags=["software", "development"],
    ),
    "personal_assistant": TeamConfig(
        name="Personal Assistant",
        prefix="/api/personal-assistant",
        description="Personal assistant for email, calendar, tasks, deals, reservations",
        tags=["personal", "assistant"],
    ),
    "market_research": TeamConfig(
        name="Market Research",
        prefix="/api/market-research",
        description="User discovery and product concept viability research",
        tags=["research", "market"],
    ),
    "soc2_compliance": TeamConfig(
        name="SOC2 Compliance",
        prefix="/api/soc2-compliance",
        description="SOC2 compliance audit and certification workflow",
        tags=["compliance", "security"],
    ),
    "social_marketing": TeamConfig(
        name="Social Media Marketing",
        prefix="/api/social-marketing",
        description="Cross-platform campaign planning with platform specialists",
        tags=["marketing", "social"],
    ),
    "branding": TeamConfig(
        name="Branding",
        prefix="/api/branding",
        description="Brand strategy, moodboards, design and writing standards",
        tags=["branding", "design"],
    ),
    "agent_provisioning": TeamConfig(
        name="Agent Provisioning",
        prefix="/api/agent-provisioning",
        description="Provision agent environments with databases, git, docker",
        tags=["provisioning", "infrastructure"],
    ),
    "accessibility_audit": TeamConfig(
        name="Accessibility Audit",
        prefix="/api/accessibility-audit",
        description="Accessibility auditing (WCAG 2.2, Section 508) for web and mobile",
        tags=["accessibility", "audit", "wcag", "508"],
    ),
    "ai_systems": TeamConfig(
        name="AI Systems",
        prefix="/api/ai-systems",
        description="Spec-driven AI agent system factory",
        tags=["ai", "agents", "systems"],
    ),
    "investment": TeamConfig(
        name="Investment",
        prefix="/api/investment",
        description="Investment analysis and portfolio management",
        tags=["investment", "finance"],
    ),
    "nutrition_meal_planning": TeamConfig(
        name="Nutrition & Meal Planning",
        prefix="/api/nutrition-meal-planning",
        description="Personal nutrition and meal planning with learning from feedback",
        tags=["nutrition", "meal-planning", "health"],
    ),
    "planning_v3": TeamConfig(
        name="Planning V3",
        prefix="/api/planning-v3",
        description="Client-facing discovery and requirements; PRD and handoff for dev/UI/UX",
        tags=["planning", "discovery", "prd"],
    ),
    "coding_team": TeamConfig(
        name="Coding Team",
        prefix="/api/coding-team",
        description="Tech Lead and stack-specialist Senior Software Engineers with Task Graph",
        tags=["coding", "development"],
    ),
    "studio_grid": TeamConfig(
        name="StudioGrid",
        prefix="/api/studio-grid",
        description="Design-system multi-agent workflow: wireframes, design system, hi-fi, handoff",
        tags=["design", "ux"],
    ),
    "sales_team": TeamConfig(
        name="AI Sales Team",
        prefix="/api/sales",
        description=(
            "Full B2B sales pod: prospecting, cold outreach, qualification, nurturing, "
            "discovery, proposals, and closing — powered by AWS Strands agents"
        ),
        tags=["sales", "crm", "pipeline", "outreach"],
    ),
    "road_trip_planning": TeamConfig(
        name="Road Trip Planning",
        prefix="/api/road-trip-planning",
        description=(
            "Multi-agent road trip planner: traveler profiling, route optimization, "
            "activity recommendations, logistics, and day-by-day itinerary generation"
        ),
        tags=["travel", "road-trip", "itinerary", "planning"],
    ),
}


def get_enabled_teams() -> Dict[str, TeamConfig]:
    """Return only enabled team configurations."""
    return {k: v for k, v in TEAM_CONFIGS.items() if v.enabled}
