"""Per-team assistant configurations.

Each entry defines the system prompt context, required/optional fields,
welcome message, default suggested questions, and — when the team has a
runnable workflow — a :class:`LaunchSpec` describing how to translate the
conversation context into an HTTP call against the team's real run
endpoint. ``launch_spec=None`` marks teams that have no workflow to
launch (e.g. the personal assistant, which is CRUD-only).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List

from team_assistant.launch_spec import BuiltBody, LaunchSpec, declarative_builder


@dataclass
class TeamAssistantConfig:
    """Configuration for one team's conversational assistant."""

    team_key: str
    team_name: str
    system_prompt_context: str
    required_fields: List[dict] = field(default_factory=list)
    optional_fields: List[dict] = field(default_factory=list)
    welcome_message: str = ""
    default_suggested_questions: List[str] = field(default_factory=list)
    llm_agent_key: str | None = None
    launch_spec: LaunchSpec | None = None


# ---------------------------------------------------------------------------
# Per-team body builders that need custom logic (not just a declarative map)
# ---------------------------------------------------------------------------


def _se_body_builder(context: dict[str, Any]) -> BuiltBody:
    """Software Engineering: either pass an existing workspace or upload a spec.

    - If the user supplied ``repo_path``, POST JSON to ``/run-team``.
    - Otherwise, take the free-text ``spec`` and upload it as a multipart
      spec file to ``/run-team/upload``. Optional ``tech_stack`` and
      ``constraints`` are appended as markdown sections so nothing the
      user told the assistant is lost.
    """
    repo_path = str(context.get("repo_path") or "").strip()
    if repo_path:
        return BuiltBody(json={"repo_path": repo_path})

    spec = str(context.get("spec") or "").strip()
    tech_stack = str(context.get("tech_stack") or "").strip()
    constraints = str(context.get("constraints") or "").strip()
    parts: list[str] = [spec]
    if tech_stack:
        parts.append(f"## Tech Stack\n{tech_stack}")
    if constraints:
        parts.append(f"## Constraints\n{constraints}")
    spec_text = "\n\n".join(p for p in parts if p)

    # Derive a short project name from the first line of the spec.
    first_line = spec.splitlines()[0] if spec else ""
    project_name = (
        "".join(ch for ch in first_line[:60] if ch.isalnum() or ch in " -_").strip()
        or "assistant-project"
    )

    return BuiltBody(
        form={"project_name": project_name},
        files={"spec_file": ("initial_spec.md", spec_text.encode("utf-8"), "text/markdown")},
        path_override="/api/software-engineering/run-team/upload",
    )


def _accessibility_body_builder(context: dict[str, Any]) -> BuiltBody:
    """Accessibility: branch on audit_type, rename audit_name → name."""
    audit_type = str(context.get("audit_type") or "webpage").strip().lower()
    body: dict[str, Any] = {
        "name": context.get("audit_name"),
    }
    if audit_type == "mobile":
        mobile_apps = context.get("mobile_apps") or context.get("web_urls")
        if mobile_apps:
            body["mobile_apps"] = mobile_apps
    else:
        web_urls = context.get("web_urls")
        if web_urls:
            body["web_urls"] = web_urls
    for key in ("critical_journeys", "timebox_hours", "auth_required", "wcag_levels"):
        value = context.get(key)
        if value is not None and value != "":
            body[key] = value
    return BuiltBody(json=body)


def _road_trip_body_builder(context: dict[str, Any]) -> BuiltBody:
    """Road trip: nest slot-filled fields under a ``trip`` key."""
    trip: dict[str, Any] = {
        "start_location": context.get("start_location"),
        "travelers": context.get("travelers") or [],
    }
    for key in (
        "end_location",
        "trip_duration_days",
        "travel_start_date",
        "required_stops",
        "vehicle_type",
        "budget_level",
        "preferences",
    ):
        value = context.get(key)
        if value is not None and value != "":
            trip[key] = value
    return BuiltBody(json={"trip": trip})


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

TEAM_ASSISTANT_CONFIGS: dict[str, TeamAssistantConfig] = {
    # -----------------------------------------------------------------------
    "software_engineering": TeamAssistantConfig(
        team_key="software_engineering",
        team_name="Software Engineering",
        system_prompt_context=(
            "A full-cycle software development team that takes a project specification and "
            "delivers architecture, code, tests, and documentation. The user needs to describe "
            "what they want built."
        ),
        required_fields=[
            {
                "key": "spec",
                "description": "Project specification — what the user wants built, including requirements and goals",
            },
        ],
        optional_fields=[
            {"key": "tech_stack", "description": "Preferred technology stack or languages"},
            {
                "key": "constraints",
                "description": "Constraints, deadlines, or non-functional requirements",
            },
        ],
        welcome_message=(
            "Welcome! I'm the Software Engineering team assistant. I'll help you define your project "
            "so our team can build it.\n\nWhat would you like us to build? Describe your project idea, "
            "requirements, or paste an existing spec."
        ),
        default_suggested_questions=[
            "I want to build a web application for managing tasks.",
            "I have an existing codebase that needs new features.",
            "I need help writing a project specification.",
        ],
        launch_spec=LaunchSpec(
            path="/api/software-engineering/run-team",
            body_builder=_se_body_builder,
        ),
    ),
    # -----------------------------------------------------------------------
    "blogging": TeamAssistantConfig(
        team_key="blogging",
        team_name="Blogging",
        system_prompt_context=(
            "A content pipeline that researches a topic, plans an article structure, drafts the "
            "post, and runs it through copy-editing and quality gates. The user needs to describe "
            "what they want to write about."
        ),
        required_fields=[
            {
                "key": "brief",
                "description": "What the blog post should be about — the core topic or angle",
            },
        ],
        optional_fields=[
            {"key": "audience", "description": "Target audience for the post"},
            {
                "key": "tone_or_purpose",
                "description": "Desired tone or purpose (e.g. educational, persuasive, casual)",
            },
            {
                "key": "content_profile",
                "description": "Content format: short_listicle, standard_article, technical_deep_dive, or series_instalment",
            },
        ],
        welcome_message=(
            "Welcome! I'm the Blogging team assistant. I'll help you plan your blog post.\n\n"
            "What would you like to write about? Tell me the topic, angle, or any ideas you have."
        ),
        default_suggested_questions=[
            "I want to write about the latest trends in AI.",
            "Help me plan a technical deep-dive article.",
            "I have a topic but need help narrowing the angle.",
        ],
        launch_spec=LaunchSpec(
            path="/api/blogging/full-pipeline-async",
            body_builder=declarative_builder(
                required=["brief"],
                optional=["audience", "tone_or_purpose", "content_profile"],
            ),
        ),
    ),
    # -----------------------------------------------------------------------
    "soc2_compliance": TeamAssistantConfig(
        team_key="soc2_compliance",
        team_name="SOC2 Compliance",
        system_prompt_context=(
            "A SOC2 compliance audit team that analyses a code repository for security, "
            "availability, and confidentiality controls. The user needs to provide the "
            "path to the repository to audit."
        ),
        required_fields=[
            {"key": "repo_path", "description": "Path to the code repository to audit"},
        ],
        optional_fields=[
            {
                "key": "compliance_scope",
                "description": "Specific SOC2 trust service categories to focus on",
            },
        ],
        welcome_message=(
            "Welcome! I'm the SOC2 Compliance team assistant. I'll help you set up a compliance audit.\n\n"
            "To get started, what's the path to the repository you'd like us to audit?"
        ),
        default_suggested_questions=[
            "I want to audit my main application repository.",
            "What does the SOC2 audit check for?",
            "Can I focus the audit on specific trust service categories?",
        ],
        launch_spec=LaunchSpec(
            path="/api/soc2-compliance/soc2-audit/run",
            body_builder=declarative_builder(required=["repo_path"]),
        ),
    ),
    # -----------------------------------------------------------------------
    "market_research": TeamAssistantConfig(
        team_key="market_research",
        team_name="Market Research",
        system_prompt_context=(
            "A market research team that conducts user discovery, competitive analysis, and "
            "product concept viability assessment. The user needs to describe their product "
            "concept and target audience."
        ),
        required_fields=[
            {
                "key": "product_concept",
                "description": "Description of the product or service concept",
            },
            {
                "key": "target_users",
                "description": "Who the product is for — target user persona or segment",
            },
            {"key": "business_goal", "description": "What the user hopes to learn or validate"},
        ],
        optional_fields=[
            {"key": "topology", "description": "Research topology: 'unified' or 'split'"},
            {"key": "transcripts", "description": "User interview transcripts (paste or describe)"},
        ],
        welcome_message=(
            "Welcome! I'm the Market Research team assistant. I'll help you set up a research study.\n\n"
            "Tell me about the product or idea you want to research — what is it, and who is it for?"
        ),
        default_suggested_questions=[
            "I want to validate a new product idea.",
            "I need competitive analysis for my market.",
            "I have user interview transcripts to analyse.",
        ],
        launch_spec=LaunchSpec(
            path="/api/market-research/market-research/run",
            body_builder=declarative_builder(
                required=["product_concept", "target_users", "business_goal"],
                optional=["topology", "transcripts"],
            ),
            synchronous=True,
        ),
    ),
    # -----------------------------------------------------------------------
    "social_marketing": TeamAssistantConfig(
        team_key="social_marketing",
        team_name="Social Media Marketing",
        system_prompt_context=(
            "A social media marketing team that plans cross-platform campaigns with content "
            "calendars, post copy, and performance tracking. The team works from a defined "
            "brand strategy — the user must provide a client_id and brand_id referencing a "
            "brand built via the Branding team (with at least Strategic Core and Narrative & "
            "Messaging phases complete). Brand voice, audience, and messaging are pulled "
            "automatically from the brand definition."
        ),
        required_fields=[
            {"key": "client_id", "description": "Client identifier from the branding team"},
            {"key": "brand_id", "description": "Brand identifier from the branding team"},
        ],
        optional_fields=[
            {
                "key": "goals",
                "description": "Campaign goals (e.g. engagement, follower growth, conversions)",
            },
            {"key": "cadence_posts_per_day", "description": "Number of posts per day"},
            {"key": "duration_days", "description": "Campaign duration in days"},
        ],
        welcome_message=(
            "Welcome! I'm your Social Media Marketing assistant. I help plan campaigns that "
            "truly reflect your brand -- from content calendars to platform-specific strategies.\n\n"
            "To create campaigns that resonate, I work from your brand's strategy and messaging. "
            "If you haven't defined your brand yet, the Branding team can help you set that up "
            "first -- it covers your strategic positioning and voice, and makes all the difference "
            "in campaign quality.\n\n"
            "Ready to get started? Tell me your client ID and brand ID, and I'll pull in your "
            "brand context."
        ),
        default_suggested_questions=[
            "I want to plan a 2-week social media campaign for my brand.",
            "Help me create a content calendar using my brand's voice and messaging.",
            "I haven't defined my brand yet -- where do I start?",
        ],
        launch_spec=LaunchSpec(
            path="/api/social-marketing/social-marketing/run",
            body_builder=declarative_builder(
                required=["client_id", "brand_id"],
                optional=["goals", "cadence_posts_per_day", "duration_days"],
            ),
        ),
    ),
    # -----------------------------------------------------------------------
    "road_trip_planning": TeamAssistantConfig(
        team_key="road_trip_planning",
        team_name="Road Trip Planning",
        system_prompt_context=(
            "A multi-agent road trip planner that builds a day-by-day itinerary including "
            "routes, activities, lodging, and logistics. The user needs to describe their trip."
        ),
        required_fields=[
            {"key": "start_location", "description": "Starting city or address for the trip"},
            {
                "key": "travelers",
                "description": "Who is traveling — number of people, ages, any special needs",
            },
        ],
        optional_fields=[
            {"key": "required_stops", "description": "Must-visit destinations or waypoints"},
            {"key": "duration_days", "description": "Trip length in days"},
            {"key": "budget", "description": "Approximate budget for the trip"},
            {
                "key": "preferences",
                "description": "Travel preferences (e.g. scenic routes, outdoor activities, foodie stops)",
            },
        ],
        welcome_message=(
            "Welcome! I'm the Road Trip Planning assistant. I'll help you plan an amazing trip.\n\n"
            "Where are you starting from, and who's going on this trip?"
        ),
        default_suggested_questions=[
            "I want to plan a week-long road trip from San Francisco.",
            "We're a family of four looking for a scenic route.",
            "I want to drive the Pacific Coast Highway.",
        ],
        launch_spec=LaunchSpec(
            path="/api/road-trip-planning/plan",
            body_builder=_road_trip_body_builder,
            synchronous=True,
        ),
    ),
    # -----------------------------------------------------------------------
    "accessibility_audit": TeamAssistantConfig(
        team_key="accessibility_audit",
        team_name="Accessibility Audit",
        system_prompt_context=(
            "An accessibility auditing team that evaluates web pages or mobile apps against "
            "WCAG 2.2 and Section 508 guidelines. The user needs to describe what to audit."
        ),
        required_fields=[
            {"key": "audit_name", "description": "Name for this audit (for tracking)"},
            {"key": "audit_type", "description": "Type of audit: 'webpage' or 'mobile'"},
            {"key": "web_urls", "description": "URLs to audit (for webpage audits)"},
        ],
        optional_fields=[
            {"key": "critical_journeys", "description": "Key user journeys to test"},
            {"key": "wcag_levels", "description": "WCAG conformance levels to check (A, AA, AAA)"},
            {"key": "timebox_hours", "description": "Maximum hours for the audit"},
            {
                "key": "auth_required",
                "description": "Whether authentication is needed to access the pages",
            },
        ],
        welcome_message=(
            "Welcome! I'm the Accessibility Audit assistant. I'll help you set up an audit.\n\n"
            "What would you like to audit — a website or a mobile app? Give me the name and URLs."
        ),
        default_suggested_questions=[
            "I want to audit my company's marketing website.",
            "I need a WCAG 2.2 AA compliance check.",
            "Can you audit a mobile app for accessibility?",
        ],
        launch_spec=LaunchSpec(
            path="/api/accessibility-audit/audit/create",
            body_builder=_accessibility_body_builder,
        ),
    ),
    # -----------------------------------------------------------------------
    "coding_team": TeamAssistantConfig(
        team_key="coding_team",
        team_name="Coding Team",
        system_prompt_context=(
            "You are part of the Software Engineering organization: a coding sub-team with a "
            "Tech Lead and stack-specialist Senior Engineers that implements features using a "
            "task graph. The full SE pipeline normally invokes this team after planning; "
            "standalone use needs the repository path and what to build."
        ),
        required_fields=[
            {"key": "repo_path", "description": "Path to the code repository"},
        ],
        optional_fields=[
            {"key": "plan_input", "description": "Specific coding tasks or feature descriptions"},
        ],
        welcome_message=(
            "Welcome! I'm the Coding Team assistant — a sub-team of Software Engineering. "
            "I'll help you set up a coding task (or you can run the full SE team from the "
            "Software Engineering page for end-to-end delivery).\n\n"
            "What's the path to the repository you want to work on?"
        ),
        default_suggested_questions=[
            "I want to add a new feature to my project.",
            "I need help implementing a specific module.",
            "I have a repo that needs refactoring.",
        ],
        launch_spec=LaunchSpec(
            path="/api/coding-team/run",
            body_builder=declarative_builder(
                required=["repo_path"],
                optional=["plan_input"],
            ),
        ),
    ),
    # -----------------------------------------------------------------------
    "personal_assistant": TeamAssistantConfig(
        team_key="personal_assistant",
        team_name="Personal Assistant",
        system_prompt_context=(
            "A personal assistant that manages email, calendar, tasks, deals, and reservations. "
            "The user needs to provide their user ID to get started."
        ),
        required_fields=[
            {"key": "user_id", "description": "User identifier for personal assistant features"},
        ],
        optional_fields=[],
        welcome_message=(
            "Welcome! I'm the Personal Assistant. I can help you manage your email, calendar, "
            "tasks, deals, and reservations.\n\nWhat's your user ID so I can access your data?"
        ),
        default_suggested_questions=[
            "Show me today's calendar events.",
            "What tasks do I have pending?",
            "Help me find deals on something I want to buy.",
        ],
        # Personal Assistant is a CRUD surface (email/calendar/tasks/deals/
        # reservations) with no "run a workflow" entry point — the Launch
        # button is hidden in the UI for this team.
        launch_spec=None,
    ),
    # -----------------------------------------------------------------------
    "planning_v3": TeamAssistantConfig(
        team_key="planning_v3",
        team_name="Planning V3",
        system_prompt_context=(
            "A planning team that runs client-facing discovery and requirements gathering, "
            "producing a PRD and handoff for development. The user needs to describe their "
            "project and provide a repository path."
        ),
        required_fields=[
            {"key": "repo_path", "description": "Path to the project repository"},
            {
                "key": "initial_brief",
                "description": "Initial project brief or description of what needs to be built",
            },
        ],
        optional_fields=[
            {"key": "client_name", "description": "Name of the client or project"},
        ],
        welcome_message=(
            "Welcome! I'm the Planning assistant. I'll help you scope and plan your project.\n\n"
            "Tell me about your project — what are you building and where is the repository?"
        ),
        default_suggested_questions=[
            "I need to plan a new feature for an existing project.",
            "I want to create a PRD for a greenfield project.",
            "Help me run a discovery session with requirements.",
        ],
        launch_spec=LaunchSpec(
            path="/api/planning-v3/run",
            body_builder=declarative_builder(
                required=["repo_path", "initial_brief"],
                optional=["client_name"],
            ),
        ),
    ),
    # -----------------------------------------------------------------------
    "ai_systems": TeamAssistantConfig(
        team_key="ai_systems",
        team_name="AI Systems",
        system_prompt_context=(
            "A spec-driven AI agent system factory that builds AI agent systems from "
            "specifications. The user needs to describe the AI system they want to build."
        ),
        required_fields=[
            {"key": "project_name", "description": "Name of the AI system project"},
            {"key": "spec_path", "description": "Path to the system specification file"},
        ],
        optional_fields=[
            {
                "key": "constraints",
                "description": "System constraints (performance, cost, latency requirements)",
            },
        ],
        welcome_message=(
            "Welcome! I'm the AI Systems assistant. I'll help you build an AI agent system.\n\n"
            "What's the name of your project, and where is the specification file?"
        ),
        default_suggested_questions=[
            "I want to build a multi-agent system.",
            "I have a spec file ready for an AI system.",
            "Help me design an AI agent architecture.",
        ],
        launch_spec=LaunchSpec(
            path="/api/ai-systems/build",
            body_builder=declarative_builder(
                required=["project_name", "spec_path"],
                optional=["constraints"],
            ),
        ),
    ),
    # -----------------------------------------------------------------------
    "agent_provisioning": TeamAssistantConfig(
        team_key="agent_provisioning",
        team_name="Agent Provisioning",
        system_prompt_context=(
            "An agent provisioning team that sets up agent environments with databases, "
            "git, and Docker infrastructure. The user needs to specify which agent to provision."
        ),
        required_fields=[
            {"key": "agent_id", "description": "Identifier of the agent to provision"},
        ],
        optional_fields=[
            {"key": "manifest_path", "description": "Path to provisioning manifest file"},
            {
                "key": "access_tier",
                "description": "Access tier for the agent (e.g. basic, standard, premium)",
            },
        ],
        welcome_message=(
            "Welcome! I'm the Agent Provisioning assistant. I'll help you set up an agent environment.\n\n"
            "Which agent would you like to provision?"
        ),
        default_suggested_questions=[
            "I need to provision a new agent environment.",
            "I want to set up infrastructure for an existing agent.",
            "What access tiers are available?",
        ],
        launch_spec=LaunchSpec(
            path="/api/agent-provisioning/provision",
            body_builder=declarative_builder(
                required=["agent_id"],
                optional=["manifest_path", "access_tier"],
            ),
        ),
    ),
    # -----------------------------------------------------------------------
    "deepthought": TeamAssistantConfig(
        team_key="deepthought",
        team_name="Deepthought",
        system_prompt_context=(
            "A recursive, self-organising agent system that analyses complex questions, "
            "identifies what specialist knowledge is needed, and dynamically creates "
            "expert sub-agents to provide comprehensive answers. Each sub-agent can "
            "further decompose its task up to 10 levels deep."
        ),
        required_fields=[
            {"key": "message", "description": "The question or message to decompose and answer"},
        ],
        optional_fields=[
            {"key": "max_depth", "description": "Maximum recursion depth (1-10, default 10)"},
        ],
        welcome_message=(
            "Welcome to Deepthought! I'm a recursive multi-agent system that breaks down "
            "complex questions into specialist perspectives.\n\n"
            "Ask me anything — I'll analyse your question, identify what expertise is needed, "
            "and dynamically assemble a team of specialist agents to provide a comprehensive answer."
        ),
        default_suggested_questions=[
            "What are the economic implications of universal basic income?",
            "Explain how mRNA vaccines work and their future applications.",
            "What would it take to establish a self-sustaining Mars colony?",
        ],
        llm_agent_key="deepthought",
        launch_spec=LaunchSpec(
            path="/api/deepthought/deepthought/ask",
            body_builder=declarative_builder(
                required=["message"],
                optional=["max_depth"],
            ),
            synchronous=True,
        ),
    ),
    # -----------------------------------------------------------------------
    "sales_team": TeamAssistantConfig(
        team_key="sales_team",
        team_name="AI Sales Team",
        system_prompt_context=(
            "A full B2B sales pod that runs prospecting, cold outreach, qualification, "
            "nurturing, proposals, and closing. The user needs to describe their product "
            "and target prospects."
        ),
        required_fields=[
            {"key": "product_name", "description": "Name of the product or service being sold"},
            {
                "key": "target_prospects",
                "description": "Description of target prospect companies or personas",
            },
        ],
        optional_fields=[
            {"key": "sales_strategy", "description": "Preferred sales approach or strategy"},
            {"key": "target_revenue", "description": "Revenue target for the campaign"},
            {"key": "timeline", "description": "Campaign timeline or deadline"},
        ],
        welcome_message=(
            "Welcome! I'm the Sales Team assistant. I'll help you set up a sales campaign.\n\n"
            "What product or service are you selling, and who are your target prospects?"
        ),
        default_suggested_questions=[
            "I want to run a B2B outreach campaign.",
            "Help me set up prospecting for my SaaS product.",
            "I need to create a sales pipeline from scratch.",
        ],
        # TODO: schema drift — the assistant collects {product_name,
        # target_prospects} but /api/sales/sales/pipeline/run wants
        # {pipeline_stage, prospect_ids?, account_ids?, deal_ids?}. Author a
        # launch_spec after the assistant's required_fields are redesigned
        # to match the sales pipeline stage model.
        launch_spec=None,
    ),
}
