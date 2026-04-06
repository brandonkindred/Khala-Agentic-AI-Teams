"""
Configuration for the Unified API Server.
"""

import os
from dataclasses import dataclass, field


@dataclass
class TeamConfig:
    """Configuration for an agent team API."""

    name: str
    prefix: str
    description: str
    enabled: bool = True
    tags: list[str] = field(default_factory=list)
    # When set, this team is a logical sub-team of another mounted team (e.g. coding_team under software_engineering).
    parent_team_key: str | None = None
    # Cell grouping for blast radius containment (teams in the same cell share failure domains).
    cell: str = "default"
    # Per-team proxy timeout in seconds. Long-running teams (SE pipeline) get higher timeouts.
    timeout_seconds: float = 60.0


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
TEAM_CONFIGS: dict[str, TeamConfig] = {
    "blogging": TeamConfig(
        name="Blogging",
        prefix="/api/blogging",
        description="Blog research, review, draft, copy-edit, and publication pipeline",
        tags=["blogging", "content"],
        cell="content",
        timeout_seconds=300.0,
    ),
    "software_engineering": TeamConfig(
        name="Software Engineering",
        prefix="/api/software-engineering",
        description="Full dev team simulation: architecture, planning, coding, review",
        tags=["software", "development"],
        cell="core_dev",
        timeout_seconds=300.0,
    ),
    "personal_assistant": TeamConfig(
        name="Personal Assistant",
        prefix="/api/personal-assistant",
        description="Personal assistant for email, calendar, tasks, deals, reservations",
        tags=["personal", "assistant"],
        cell="personal",
        timeout_seconds=120.0,
    ),
    "market_research": TeamConfig(
        name="Market Research",
        prefix="/api/market-research",
        description="User discovery and product concept viability research",
        tags=["research", "market"],
        cell="business",
        timeout_seconds=120.0,
    ),
    "soc2_compliance": TeamConfig(
        name="SOC2 Compliance",
        prefix="/api/soc2-compliance",
        description="SOC2 compliance audit and certification workflow",
        tags=["compliance", "security"],
        cell="business",
        timeout_seconds=300.0,
    ),
    "social_marketing": TeamConfig(
        name="Social Media Marketing",
        prefix="/api/social-marketing",
        description="Cross-platform campaign planning with platform specialists",
        tags=["marketing", "social"],
        cell="content",
        timeout_seconds=120.0,
    ),
    "branding": TeamConfig(
        name="Branding",
        prefix="/api/branding",
        description="Brand strategy, moodboards, design and writing standards",
        tags=["branding", "design"],
        cell="content",
        timeout_seconds=120.0,
    ),
    "agent_provisioning": TeamConfig(
        name="Agent Provisioning",
        prefix="/api/agent-provisioning",
        description="Provision agent environments with databases, git, docker",
        tags=["provisioning", "infrastructure"],
        cell="core_dev",
        timeout_seconds=120.0,
    ),
    "accessibility_audit": TeamConfig(
        name="Accessibility Audit",
        prefix="/api/accessibility-audit",
        description="Accessibility auditing (WCAG 2.2, Section 508) for web and mobile",
        tags=["accessibility", "audit", "wcag", "508"],
        cell="personal",
        timeout_seconds=120.0,
    ),
    "ai_systems": TeamConfig(
        name="AI Systems",
        prefix="/api/ai-systems",
        description="Spec-driven AI agent system factory",
        tags=["ai", "agents", "systems"],
        cell="core_dev",
        timeout_seconds=300.0,
    ),
    "investment": TeamConfig(
        name="Investment",
        prefix="/api/investment",
        description=(
            "Investment organization: (1) Financial advisor — IPS, proposals, promotion, memos (user profile); "
            "(2) Strategy lab — ideation, backtests, generic strategies without a user profile. Same API prefix."
        ),
        tags=["investment", "finance", "investment-advisor", "investment-strategy-lab"],
        cell="business",
        timeout_seconds=120.0,
    ),
    "investment_strategy_lab": TeamConfig(
        name="Investment Strategy Lab",
        prefix="/api/investment-strategy-lab",
        description=(
            "Logical sub-team of Investment: strategy ideation and backtests without InvestmentProfile. "
            "HTTP routes are served on the parent Investment API: POST /api/investment/strategy-lab/run, "
            "GET /api/investment/strategy-lab/results (not this prefix)."
        ),
        enabled=False,
        tags=["investment", "strategy-lab", "backtest", "stocks", "crypto"],
        parent_team_key="investment",
        cell="business",
    ),
    "nutrition_meal_planning": TeamConfig(
        name="Nutrition & Meal Planning",
        prefix="/api/nutrition-meal-planning",
        description="Personal nutrition and meal planning with learning from feedback",
        tags=["nutrition", "meal-planning", "health"],
        cell="personal",
        timeout_seconds=120.0,
    ),
    "planning_v3": TeamConfig(
        name="Planning V3",
        prefix="/api/planning-v3",
        description="Client-facing discovery and requirements; PRD and handoff for dev/UI/UX",
        tags=["planning", "discovery", "prd"],
        cell="core_dev",
        timeout_seconds=300.0,
    ),
    "coding_team": TeamConfig(
        name="Coding Team",
        prefix="/api/coding-team",
        description=(
            "Software Engineering sub-team: Tech Lead and stack-specialist Senior Software Engineers "
            "with Task Graph (invoked by the SE orchestrator after planning)"
        ),
        tags=["coding", "development", "software-engineering"],
        parent_team_key="software_engineering",
        cell="core_dev",
        timeout_seconds=300.0,
    ),
    "studio_grid": TeamConfig(
        name="StudioGrid",
        prefix="/api/studio-grid",
        description="Design-system multi-agent workflow: wireframes, design system, hi-fi, handoff",
        tags=["design", "ux"],
        cell="content",
        timeout_seconds=120.0,
    ),
    "sales_team": TeamConfig(
        name="AI Sales Team",
        prefix="/api/sales",
        description=(
            "Full B2B sales pod: prospecting, cold outreach, qualification, nurturing, "
            "discovery, proposals, and closing — powered by AWS Strands agents"
        ),
        tags=["sales", "crm", "pipeline", "outreach"],
        cell="business",
        timeout_seconds=120.0,
    ),
    "road_trip_planning": TeamConfig(
        name="Road Trip Planning",
        prefix="/api/road-trip-planning",
        description=(
            "Multi-agent road trip planner: traveler profiling, route optimization, "
            "activity recommendations, logistics, and day-by-day itinerary generation"
        ),
        tags=["travel", "road-trip", "itinerary", "planning"],
        cell="personal",
        timeout_seconds=120.0,
    ),
    "agentic_team_provisioning": TeamConfig(
        name="Agentic Team Provisioning",
        prefix="/api/agentic-team-provisioning",
        description="Create agentic teams and define their processes through conversation",
        tags=["agentic", "teams", "processes", "provisioning"],
        cell="core_dev",
        timeout_seconds=120.0,
    ),
    "startup_advisor": TeamConfig(
        name="Startup Advisor",
        prefix="/api/startup-advisor",
        description="Persistent conversational startup advisor with probing dialogue and artifact generation",
        tags=["startup", "advisor", "coaching", "strategy"],
        cell="business",
        timeout_seconds=120.0,
    ),
    "user_agent_founder": TeamConfig(
        name="User Agent Founder",
        prefix="/api/user-agent-founder",
        description="Autonomous startup founder agent that drives the SE team to build a task management service",
        tags=["user-agent", "founder", "simulation"],
        cell="core_dev",
        timeout_seconds=300.0,
    ),
    "deepthought": TeamConfig(
        name="Deepthought",
        prefix="/api/deepthought",
        description="Recursive self-organising agent that dynamically creates specialist sub-agents to answer complex questions",
        tags=["deepthought", "recursive", "multi-agent"],
        cell="core_dev",
        timeout_seconds=120.0,
    ),
}


def get_enabled_teams() -> dict[str, TeamConfig]:
    """Return only enabled team configurations."""
    return {k: v for k, v in TEAM_CONFIGS.items() if v.enabled}
