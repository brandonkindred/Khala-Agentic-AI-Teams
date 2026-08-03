"""Static question registries and fallback-question data for the PRA agent.

Extracted verbatim from ``agent.py`` to keep that module focused on workflow
logic. This module holds only pure static data plus the two helpers that build
fallback :class:`OpenQuestion` lists from it:

- ``context_discovery_fallback_questions`` -- fixed context/constraint questions.
- ``SOP_PHASE1_QUESTIONS`` -- the structured SOP Phase 1 question registry.
- ``_sop_phase1_fallback_questions`` -- root-question fallback built from the registry.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .models import OpenQuestion, QuestionOption, SOPSubPhase


def context_discovery_fallback_questions() -> List[OpenQuestion]:
    """Fixed list of context/constraint questions used when LLM returns empty or invalid.

    Preconditions: none — takes no arguments and reads no external state.
    Postconditions: returns a new list of 7 fully-formed ``OpenQuestion`` instances
        (``ctx_project_type``, ``ctx_deployment``, ``ctx_cloud_provider``, ``ctx_tenets``,
        ``ctx_sla``, ``ctx_rto_rpo``), each with ``source="context_discovery"`` and at
        least 2 ``QuestionOption`` entries, exactly one of which has ``is_default=True``
        per question (``ctx_tenets`` allows two defaults since ``allow_multiple=True``).
        The returned list is freshly constructed on every call — callers may mutate it
        without affecting subsequent calls.
    """
    return [
        OpenQuestion(
            id="ctx_project_type",
            question_text="What type of organization or product context is this?",
            context="Shapes MVP scope and governance expectations.",
            options=[
                QuestionOption(
                    id="opt_startup",
                    label="Startup / early-stage (agility, speed)",
                    is_default=True,
                    rationale="Common for new products.",
                    confidence=0.6,
                ),
                QuestionOption(
                    id="opt_enterprise",
                    label="Enterprise (governance, compliance)",
                    is_default=False,
                    rationale="For established orgs.",
                    confidence=0.5,
                ),
            ],
            source="context_discovery",
            category="business",
        ),
        OpenQuestion(
            id="ctx_deployment",
            question_text="Where will this be deployed?",
            context="Deployment model affects infrastructure and provider choices.",
            options=[
                QuestionOption(
                    id="opt_cloud",
                    label="Cloud (AWS, GCP, Azure, etc.)",
                    is_default=True,
                    rationale="Most common for new apps.",
                    confidence=0.7,
                ),
                QuestionOption(
                    id="opt_onprem",
                    label="On-premises",
                    is_default=False,
                    rationale="For air-gapped or regulated environments.",
                    confidence=0.3,
                ),
                QuestionOption(
                    id="opt_hybrid",
                    label="Hybrid (cloud + on-prem)",
                    is_default=False,
                    rationale="Mix of cloud and on-prem.",
                    confidence=0.4,
                ),
            ],
            source="context_discovery",
            category="infrastructure",
        ),
        OpenQuestion(
            id="ctx_cloud_provider",
            question_text="If cloud: which provider (or primary provider)?",
            context="Affects service selection and constraints.",
            options=[
                QuestionOption(
                    id="opt_aws",
                    label="AWS",
                    is_default=True,
                    rationale="Widely used, broad service set.",
                    confidence=0.6,
                ),
                QuestionOption(
                    id="opt_gcp",
                    label="GCP",
                    is_default=False,
                    rationale="Strong data/ML offerings.",
                    confidence=0.5,
                ),
                QuestionOption(
                    id="opt_azure",
                    label="Azure",
                    is_default=False,
                    rationale="Good for Microsoft ecosystem.",
                    confidence=0.5,
                ),
                QuestionOption(
                    id="opt_other",
                    label="Other (Rackspace, DigitalOcean, Heroku, etc.)",
                    is_default=False,
                    rationale="Varies by need.",
                    confidence=0.3,
                ),
            ],
            source="context_discovery",
            category="infrastructure",
        ),
        OpenQuestion(
            id="ctx_tenets",
            question_text="What architectural or product tenets must the build follow? (select all that apply)",
            context="Principles that shape technology and design decisions.",
            options=[
                QuestionOption(
                    id="opt_event_driven",
                    label="Event-driven",
                    is_default=False,
                    rationale="Async, decoupled systems.",
                    confidence=0.5,
                ),
                QuestionOption(
                    id="opt_api_driven",
                    label="API-driven",
                    is_default=True,
                    rationale="Clear contracts, integrability.",
                    confidence=0.7,
                ),
                QuestionOption(
                    id="opt_serverless",
                    label="Serverless / managed services",
                    is_default=False,
                    rationale="Reduce ops, scale to zero.",
                    confidence=0.5,
                ),
                QuestionOption(
                    id="opt_agility",
                    label="Agility / ease of change",
                    is_default=True,
                    rationale="Fast iteration.",
                    confidence=0.7,
                ),
                QuestionOption(
                    id="opt_security_first",
                    label="Security-first",
                    is_default=False,
                    rationale="Compliance and risk focus.",
                    confidence=0.5,
                ),
            ],
            allow_multiple=True,
            source="context_discovery",
            category="architecture",
        ),
        OpenQuestion(
            id="ctx_sla",
            question_text="What availability/SLA target applies (if any)?",
            context="Organizational mandate for uptime.",
            options=[
                QuestionOption(
                    id="opt_none",
                    label="None / standard",
                    is_default=True,
                    rationale="No formal SLA.",
                    confidence=0.6,
                ),
                QuestionOption(
                    id="opt_three_nines",
                    label="99.9% (three nines)",
                    is_default=False,
                    rationale="~8.7h downtime/year.",
                    confidence=0.5,
                ),
                QuestionOption(
                    id="opt_five_nines",
                    label="99.99% or higher (four/five nines)",
                    is_default=False,
                    rationale="High availability mandate.",
                    confidence=0.4,
                ),
            ],
            source="context_discovery",
            category="business",
        ),
        OpenQuestion(
            id="ctx_rto_rpo",
            question_text="Any RTO/RPO or disaster-recovery mandates?",
            context="Recovery time and recovery point objectives.",
            options=[
                QuestionOption(
                    id="opt_none",
                    label="None / standard backup",
                    is_default=True,
                    rationale="No strict RTO/RPO.",
                    confidence=0.6,
                ),
                QuestionOption(
                    id="opt_moderate",
                    label="Moderate (e.g. RTO 4h, RPO 1h)",
                    is_default=False,
                    rationale="Some DR requirements.",
                    confidence=0.5,
                ),
                QuestionOption(
                    id="opt_strict",
                    label="Strict (e.g. RTO <1h, RPO <15min)",
                    is_default=False,
                    rationale="Critical systems.",
                    confidence=0.4,
                ),
            ],
            source="context_discovery",
            category="business",
        ),
    ]


# ---------------------------------------------------------------------------
# SOP Phase 1: Structured Question Registry
# ---------------------------------------------------------------------------
# Each sub-phase maps to a list of question definitions. Questions with
# ``depends_on`` are conditional — they are only asked when the parent
# question's answer matches one of the listed values.
#
# Invariants (hold for every question-definition dict in every sub-phase list):
# - Required keys: ``sop_id`` (globally unique str, dotted "P1.<subphase>.<letter>"
#   form, optionally suffixed e.g. ".1"/".2" for a follow-up), ``question_text``,
#   ``category``, ``allow_multiple`` (bool), ``options`` (list of option dicts),
#   ``depends_on`` (``None`` or a ``{parent_sop_id: [required_label, ...]}`` mapping
#   consumed by ``sop_engine.evaluate_sop_conditionals`` — a conditional question's
#   ``parent_sop_id`` always names a ``sop_id`` defined earlier in the registry).
# - Each option dict has ``id`` and ``label`` (both str) and ``rationale`` (str,
#   may be empty); ``is_default`` is optional and defaults to falsy when absent.
# - At most one option per question sets ``is_default: True``.
SOP_PHASE1_QUESTIONS: Dict[SOPSubPhase, List[Dict[str, Any]]] = {
    # ------------------------------------------------------------------
    # TENETS — foundational context collected BEFORE any technical details
    # ------------------------------------------------------------------
    SOPSubPhase.TENETS: [
        {
            "sop_id": "P1.tenets.a",
            "question_text": "What is the organizational context for this project?",
            "category": "business",
            "allow_multiple": False,
            "options": [
                {
                    "id": "opt_early_startup",
                    "label": "Early-stage startup (pre-revenue, small team)",
                    "rationale": "Speed and frugality dominate; MVP-first, validate before investing.",
                },
                {
                    "id": "opt_growth_startup",
                    "label": "Growth-stage startup (product-market fit, scaling)",
                    "rationale": "Balance speed with reliability; technical debt matters.",
                },
                {
                    "id": "opt_smb",
                    "label": "Small/medium business (established, modest team)",
                    "rationale": "Pragmatic choices; budget-conscious but stable.",
                },
                {
                    "id": "opt_enterprise",
                    "label": "Enterprise (large org, many teams, mature processes)",
                    "rationale": "Compliance, governance, and scale requirements from day one.",
                    "is_default": True,
                },
                {
                    "id": "opt_personal",
                    "label": "Personal/side project",
                    "rationale": "Simplicity and low cost above all.",
                },
            ],
            "depends_on": None,
        },
        {
            "sop_id": "P1.tenets.b",
            "question_text": "What is the expected user scale at launch and within the first year?",
            "category": "business",
            "allow_multiple": False,
            "options": [
                {
                    "id": "opt_zero",
                    "label": "Starting from zero (no existing user base)",
                    "rationale": "Build lean; scale-up architecture rather than scale-out.",
                    "is_default": True,
                },
                {
                    "id": "opt_small",
                    "label": "Small (hundreds to low thousands of users)",
                    "rationale": "Simple infrastructure; optimize for development speed.",
                },
                {
                    "id": "opt_medium",
                    "label": "Medium (tens of thousands of users)",
                    "rationale": "Needs reliability and basic scaling patterns.",
                },
                {
                    "id": "opt_large",
                    "label": "Large (hundreds of thousands to millions of users)",
                    "rationale": "Scalability, observability, and incident response from day one.",
                },
                {
                    "id": "opt_massive",
                    "label": "Massive (millions+ from day one, e.g. adding to existing platform)",
                    "rationale": "Production-grade everything; zero tolerance for downtime.",
                },
            ],
            "depends_on": None,
        },
        {
            "sop_id": "P1.tenets.c",
            "question_text": "How would you describe the budget philosophy for this project?",
            "category": "business",
            "allow_multiple": False,
            "options": [
                {
                    "id": "opt_frugal",
                    "label": "Frugal at all costs (minimize spend, use free tiers, open source)",
                    "rationale": "Every dollar counts; defer paid services until validated.",
                },
                {
                    "id": "opt_cost_conscious",
                    "label": "Cost-conscious (spend where it matters, but keep it reasonable)",
                    "rationale": "Invest strategically; avoid waste but don't sacrifice quality.",
                    "is_default": True,
                },
                {
                    "id": "opt_balanced",
                    "label": "Balanced (willing to pay for productivity and reliability)",
                    "rationale": "Use managed services to reduce ops burden; budget is available.",
                },
                {
                    "id": "opt_invest",
                    "label": "Invest for quality (budget is not the primary constraint)",
                    "rationale": "Focus on performance, reliability, and developer experience.",
                },
            ],
            "depends_on": None,
        },
        {
            "sop_id": "P1.tenets.d",
            "question_text": "Are there industry-specific regulations or compliance requirements?",
            "category": "business",
            "allow_multiple": True,
            "options": [
                {
                    "id": "opt_none",
                    "label": "None / not sure",
                    "rationale": "No known regulatory obligations.",
                    "is_default": True,
                },
                {
                    "id": "opt_hipaa",
                    "label": "HIPAA (healthcare data)",
                    "rationale": "PHI protection, audit trails, encryption requirements.",
                },
                {
                    "id": "opt_pci",
                    "label": "PCI-DSS (payment card data)",
                    "rationale": "Cardholder data protection, network segmentation.",
                },
                {
                    "id": "opt_soc2",
                    "label": "SOC 2 (service organization controls)",
                    "rationale": "Security, availability, confidentiality controls.",
                },
                {
                    "id": "opt_gdpr",
                    "label": "GDPR (EU data protection)",
                    "rationale": "Data residency, consent management, right to deletion.",
                },
                {
                    "id": "opt_fedramp",
                    "label": "FedRAMP / government (US federal)",
                    "rationale": "Government-grade security, authorized cloud regions.",
                },
                {
                    "id": "opt_financial",
                    "label": "Financial regulations (SEC, FINRA, etc.)",
                    "rationale": "Audit trails, data retention, reporting requirements.",
                },
                {
                    "id": "opt_other_reg",
                    "label": "Other (specify in comments)",
                    "rationale": "Industry-specific regulation not listed.",
                },
            ],
            "depends_on": None,
        },
        {
            "sop_id": "P1.tenets.e",
            "question_text": "What is the primary goal for the initial release?",
            "category": "business",
            "allow_multiple": False,
            "options": [
                {
                    "id": "opt_mvp",
                    "label": "Quick MVP (validate the idea, iterate fast)",
                    "rationale": "Ship fast, learn from real users, iterate.",
                    "is_default": True,
                },
                {
                    "id": "opt_production",
                    "label": "Production-ready (reliable, scalable from launch)",
                    "rationale": "Users expect stability; reputation matters on day one.",
                },
                {
                    "id": "opt_internal",
                    "label": "Internal tool (reliability matters, polish less so)",
                    "rationale": "Focus on function over form; internal SLAs.",
                },
                {
                    "id": "opt_migration",
                    "label": "Migration / rewrite of existing system",
                    "rationale": "Feature parity with existing system; zero data loss.",
                },
            ],
            "depends_on": None,
        },
        {
            "sop_id": "P1.tenets.f",
            "question_text": "What are your non-negotiable tenets for this system? (select all that apply)",
            "category": "business",
            "allow_multiple": True,
            "options": [
                {
                    "id": "opt_cloud_native",
                    "label": "Cloud-native (designed for cloud from the ground up)",
                    "rationale": "Leverage cloud services, auto-scaling, managed infrastructure.",
                },
                {
                    "id": "opt_open_source",
                    "label": "Open-source first (avoid vendor lock-in)",
                    "rationale": "Portability, community support, no licensing costs.",
                },
                {
                    "id": "opt_performance",
                    "label": "Performance and low latency above all",
                    "rationale": "Every millisecond counts; optimize aggressively.",
                },
                {
                    "id": "opt_security_first",
                    "label": "Security first (zero-trust, defense in depth)",
                    "rationale": "Security is a feature, not an afterthought.",
                },
                {
                    "id": "opt_simplicity",
                    "label": "Simplicity (fewer moving parts, easier to maintain)",
                    "rationale": "Reduce operational complexity; boring technology is good.",
                    "is_default": True,
                },
                {
                    "id": "opt_scalability",
                    "label": "Scalability (design for 10x growth from day one)",
                    "rationale": "Architecture decisions that support horizontal scaling.",
                },
                {
                    "id": "opt_developer_experience",
                    "label": "Developer experience (fast feedback loops, great tooling)",
                    "rationale": "Ship faster with better DX; invest in CI/CD and local dev.",
                },
                {
                    "id": "opt_data_sovereignty",
                    "label": "Data sovereignty (data stays in specific regions/jurisdictions)",
                    "rationale": "Legal or business requirement for data residency.",
                },
            ],
            "depends_on": None,
        },
        {
            "sop_id": "P1.tenets.g",
            "question_text": "If budget and timeline conflict, which wins?",
            "category": "business",
            "allow_multiple": False,
            "options": [
                {
                    "id": "opt_budget",
                    "label": "Budget (stay under budget even if it takes longer)",
                    "rationale": "Financial constraints are hard limits.",
                },
                {
                    "id": "opt_timeline",
                    "label": "Timeline (ship on time even if it costs more)",
                    "rationale": "Market window or commitment deadline is critical.",
                    "is_default": True,
                },
                {
                    "id": "opt_scope",
                    "label": "Scope (cut features to stay on budget and timeline)",
                    "rationale": "Deliver less but deliver it well and on time/budget.",
                },
            ],
            "depends_on": None,
        },
    ],
    # ------------------------------------------------------------------
    SOPSubPhase.DEPLOYMENT: [
        {
            "sop_id": "P1.deploy.a",
            "question_text": "Where will this be deployed?",
            "category": "infrastructure",
            "allow_multiple": False,
            "options": [
                {
                    "id": "opt_onprem",
                    "label": "On-prem",
                    "rationale": "For air-gapped or regulated environments.",
                },
                {
                    "id": "opt_cloud",
                    "label": "Cloud",
                    "rationale": "Most common for new applications.",
                    "is_default": True,
                },
                {
                    "id": "opt_paas",
                    "label": "PaaS",
                    "rationale": "Managed platform (Heroku, Render, etc.)",
                },
                {"id": "opt_hybrid", "label": "Hybrid", "rationale": "Mix of cloud and on-prem."},
            ],
            "depends_on": None,
        },
        {
            "sop_id": "P1.deploy.b",
            "question_text": "Which cloud provider?",
            "category": "infrastructure",
            "allow_multiple": False,
            "options": [
                {
                    "id": "opt_aws",
                    "label": "AWS",
                    "rationale": "Widely used, broad service set.",
                    "is_default": True,
                },
                {"id": "opt_gcp", "label": "GCP", "rationale": "Strong data/ML offerings."},
                {"id": "opt_azure", "label": "Azure", "rationale": "Good for Microsoft ecosystem."},
                {
                    "id": "opt_rackspace",
                    "label": "RackSpace",
                    "rationale": "Managed hosting specialist.",
                },
                {
                    "id": "opt_digitalocean",
                    "label": "DigitalOcean",
                    "rationale": "Simple, developer-friendly.",
                },
                {"id": "opt_other", "label": "Other", "rationale": "Specify your provider."},
            ],
            "depends_on": {"P1.deploy.a": ["Cloud", "Hybrid"]},
        },
        {
            "sop_id": "P1.deploy.c",
            "question_text": "Should the application use serverless compute?",
            "category": "infrastructure",
            "allow_multiple": False,
            "options": [
                {
                    "id": "opt_yes",
                    "label": "Yes",
                    "rationale": "Reduce ops overhead, scale to zero.",
                },
                {
                    "id": "opt_no",
                    "label": "No",
                    "rationale": "Full control with containers/VMs.",
                    "is_default": True,
                },
                {
                    "id": "opt_partial",
                    "label": "Partially",
                    "rationale": "Mix serverless and traditional compute.",
                },
            ],
            "depends_on": {"P1.deploy.a": ["Cloud", "Hybrid"]},
        },
        {
            "sop_id": "P1.deploy.d",
            "question_text": "Which PaaS platform?",
            "category": "infrastructure",
            "allow_multiple": False,
            "options": [
                {
                    "id": "opt_heroku",
                    "label": "Heroku",
                    "rationale": "Well-known, easy to use.",
                    "is_default": True,
                },
                {
                    "id": "opt_supabase",
                    "label": "Supabase",
                    "rationale": "Open-source Firebase alternative with Postgres.",
                },
                {
                    "id": "opt_vercel",
                    "label": "Vercel",
                    "rationale": "Great for frontend-heavy apps.",
                },
                {
                    "id": "opt_render",
                    "label": "Render",
                    "rationale": "Modern PaaS, simple pricing.",
                },
                {
                    "id": "opt_railway",
                    "label": "Railway",
                    "rationale": "Developer-friendly, fast deploys.",
                },
                {"id": "opt_other", "label": "Other", "rationale": "Specify your PaaS."},
            ],
            "depends_on": {"P1.deploy.a": ["PaaS"]},
        },
    ],
    SOPSubPhase.REGULATIONS: [
        {
            "sop_id": "P1.regulations.a",
            "question_text": "Is this project subject to any regulatory requirements?",
            "category": "business",
            "allow_multiple": True,
            "options": [
                {"id": "opt_gdpr", "label": "GDPR", "rationale": "EU data protection regulation."},
                {"id": "opt_ccpa", "label": "CCPA", "rationale": "California consumer privacy."},
                {"id": "opt_hipaa", "label": "HIPAA", "rationale": "US health data regulation."},
                {
                    "id": "opt_pci",
                    "label": "PCI-DSS",
                    "rationale": "Payment card industry standard.",
                },
                {
                    "id": "opt_none",
                    "label": "None",
                    "rationale": "No specific regulatory requirements.",
                    "is_default": True,
                },
                {
                    "id": "opt_other",
                    "label": "Other",
                    "rationale": "Specify your regulatory requirements.",
                },
            ],
            "depends_on": None,
        },
        {
            "sop_id": "P1.regulations.b",
            "question_text": "Do you need any enterprise certifications?",
            "category": "business",
            "allow_multiple": True,
            "options": [
                {"id": "opt_soc2", "label": "SOC2", "rationale": "Common for SaaS products."},
                {
                    "id": "opt_iso27001",
                    "label": "ISO 27001",
                    "rationale": "International information security standard.",
                },
                {
                    "id": "opt_fedramp",
                    "label": "FedRAMP",
                    "rationale": "US federal cloud security.",
                },
                {
                    "id": "opt_none",
                    "label": "None",
                    "rationale": "No enterprise certification needed.",
                    "is_default": True,
                },
                {"id": "opt_other", "label": "Other", "rationale": "Specify your certification."},
            ],
            "depends_on": None,
        },
    ],
    SOPSubPhase.TOOL_PREFERENCES: [
        {
            "sop_id": "P1.tools.a",
            "question_text": "Do you have a preference for open source or proprietary tools/services?",
            "category": "business",
            "allow_multiple": False,
            "options": [
                {
                    "id": "opt_open",
                    "label": "Open Source",
                    "rationale": "Community-driven, no license costs.",
                },
                {
                    "id": "opt_proprietary",
                    "label": "Proprietary",
                    "rationale": "Commercial support and SLAs.",
                },
                {
                    "id": "opt_none",
                    "label": "No preference",
                    "rationale": "Best tool for the job regardless.",
                    "is_default": True,
                },
            ],
            "depends_on": None,
        },
        {
            "sop_id": "P1.tools.b",
            "question_text": "What existing proprietary licenses or tools do you already have?",
            "category": "business",
            "allow_multiple": True,
            "options": [
                {
                    "id": "opt_jira",
                    "label": "Jira",
                    "rationale": "Project tracking and issue management.",
                },
                {
                    "id": "opt_confluence",
                    "label": "Confluence",
                    "rationale": "Documentation and knowledge base.",
                },
                {
                    "id": "opt_datadog",
                    "label": "Datadog",
                    "rationale": "Monitoring and observability platform.",
                },
                {
                    "id": "opt_pagerduty",
                    "label": "PagerDuty",
                    "rationale": "Incident management and alerting.",
                },
                {"id": "opt_splunk", "label": "Splunk", "rationale": "Log management and SIEM."},
                {
                    "id": "opt_other",
                    "label": "Other",
                    "rationale": "Specify your proprietary tools.",
                },
            ],
            "depends_on": {"P1.tools.a": ["Proprietary"]},
        },
        {
            "sop_id": "P1.tools.c",
            "question_text": "What open source tools/frameworks are you already familiar with?",
            "category": "business",
            "allow_multiple": True,
            "options": [
                {"id": "opt_docker", "label": "Docker", "rationale": "Containerization standard."},
                {
                    "id": "opt_kubernetes",
                    "label": "Kubernetes",
                    "rationale": "Container orchestration.",
                },
                {
                    "id": "opt_postgres",
                    "label": "PostgreSQL",
                    "rationale": "Full-featured relational database.",
                },
                {
                    "id": "opt_redis",
                    "label": "Redis",
                    "rationale": "In-memory data store and cache.",
                },
                {"id": "opt_nginx", "label": "Nginx", "rationale": "Web server and reverse proxy."},
                {
                    "id": "opt_other",
                    "label": "Other",
                    "rationale": "Specify your open source tools.",
                },
            ],
            "depends_on": {"P1.tools.a": ["Open Source"]},
        },
    ],
    SOPSubPhase.CODING_PREFERENCES: [
        {
            "sop_id": "P1.coding.a",
            "question_text": "Where will the source code be stored?",
            "category": "infrastructure",
            "allow_multiple": False,
            "options": [
                {
                    "id": "opt_github",
                    "label": "GitHub",
                    "rationale": "Most popular, great ecosystem.",
                    "is_default": True,
                },
                {
                    "id": "opt_gitlab",
                    "label": "GitLab",
                    "rationale": "Built-in CI/CD, self-hostable.",
                },
                {
                    "id": "opt_bitbucket",
                    "label": "BitBucket",
                    "rationale": "Atlassian integration.",
                },
                {
                    "id": "opt_codeberg",
                    "label": "Codeberg",
                    "rationale": "Open-source Gitea-based.",
                },
                {"id": "opt_other", "label": "Other", "rationale": "Specify your repository host."},
            ],
            "depends_on": None,
        },
        {
            "sop_id": "P1.coding.b",
            "question_text": "What programming language(s) do you prefer?",
            "category": "architecture",
            "allow_multiple": True,
            "options": [
                {
                    "id": "opt_java",
                    "label": "Java",
                    "rationale": "Enterprise-grade, mature ecosystem.",
                },
                {
                    "id": "opt_rust",
                    "label": "Rust",
                    "rationale": "Memory safety, high performance.",
                },
                {
                    "id": "opt_python",
                    "label": "Python",
                    "rationale": "Versatile, great for APIs and data.",
                    "is_default": True,
                },
                {"id": "opt_js", "label": "JavaScript", "rationale": "Universal web language."},
                {"id": "opt_ts", "label": "TypeScript", "rationale": "Type-safe JavaScript."},
                {"id": "opt_go", "label": "Go", "rationale": "Simple, fast, great for services."},
                {
                    "id": "opt_ruby",
                    "label": "Ruby",
                    "rationale": "Developer happiness, Rails ecosystem.",
                },
                {
                    "id": "opt_cpp",
                    "label": "C/C++",
                    "rationale": "Systems programming, maximum performance.",
                },
                {
                    "id": "opt_erlang",
                    "label": "Erlang",
                    "rationale": "Fault-tolerant, distributed systems.",
                },
                {"id": "opt_other", "label": "Other", "rationale": "Specify your language."},
            ],
            "depends_on": None,
        },
        {
            "sop_id": "P1.coding.c",
            "question_text": "Do you have any framework preferences?",
            "category": "architecture",
            "allow_multiple": True,
            "options": [
                {
                    "id": "opt_fastapi",
                    "label": "FastAPI",
                    "rationale": "Modern Python async API framework.",
                    "is_default": True,
                },
                {
                    "id": "opt_django",
                    "label": "Django",
                    "rationale": "Full-featured Python web framework.",
                },
                {
                    "id": "opt_flask",
                    "label": "Flask",
                    "rationale": "Lightweight Python web framework.",
                },
                {
                    "id": "opt_express",
                    "label": "Express.js",
                    "rationale": "Minimal Node.js web framework.",
                },
                {
                    "id": "opt_nextjs",
                    "label": "Next.js",
                    "rationale": "React meta-framework with SSR.",
                },
                {
                    "id": "opt_spring",
                    "label": "Spring Boot",
                    "rationale": "Enterprise Java framework.",
                },
                {
                    "id": "opt_rails",
                    "label": "Ruby on Rails",
                    "rationale": "Convention-over-configuration web framework.",
                },
                {"id": "opt_other", "label": "Other", "rationale": "Specify your framework."},
            ],
            "depends_on": None,
        },
        {
            "sop_id": "P1.coding.d",
            "question_text": "Do you have a package management preference?",
            "category": "architecture",
            "allow_multiple": False,
            "options": [
                {"id": "opt_pip", "label": "pip", "rationale": "Standard Python package manager."},
                {
                    "id": "opt_poetry",
                    "label": "Poetry",
                    "rationale": "Modern Python dependency management.",
                    "is_default": True,
                },
                {"id": "opt_npm", "label": "npm", "rationale": "Default Node.js package manager."},
                {
                    "id": "opt_yarn",
                    "label": "yarn",
                    "rationale": "Fast, reliable Node.js package manager.",
                },
                {
                    "id": "opt_pnpm",
                    "label": "pnpm",
                    "rationale": "Efficient, disk-space-saving Node.js package manager.",
                },
                {
                    "id": "opt_maven",
                    "label": "Maven",
                    "rationale": "Java/JVM build and dependency tool.",
                },
                {"id": "opt_gradle", "label": "Gradle", "rationale": "Flexible JVM build tool."},
                {"id": "opt_cargo", "label": "Cargo", "rationale": "Rust package manager."},
                {"id": "opt_other", "label": "Other", "rationale": "Specify your preference."},
            ],
            "depends_on": None,
        },
        {
            "sop_id": "P1.coding.e",
            "question_text": "What CI/CD pipeline service do you prefer?",
            "category": "infrastructure",
            "allow_multiple": False,
            "options": [
                {
                    "id": "opt_gh_actions",
                    "label": "GitHub Actions",
                    "rationale": "Native to GitHub, easy setup.",
                    "is_default": True,
                },
                {
                    "id": "opt_gitlab_ci",
                    "label": "GitLab CI",
                    "rationale": "Built into GitLab, powerful.",
                },
                {"id": "opt_aws_cp", "label": "AWS CodePipeline", "rationale": "Native AWS CI/CD."},
                {
                    "id": "opt_circleci",
                    "label": "CircleCI",
                    "rationale": "Fast builds, good caching.",
                },
                {
                    "id": "opt_jenkins",
                    "label": "Jenkins",
                    "rationale": "Highly customizable, self-hosted.",
                },
                {"id": "opt_other", "label": "Other", "rationale": "Specify your CI/CD."},
            ],
            "depends_on": None,
        },
    ],
    SOPSubPhase.DATA: [
        {
            "sop_id": "P1.data.a",
            "question_text": "What kinds of data will need to be stored?",
            "category": "architecture",
            "allow_multiple": True,
            "options": [
                {
                    "id": "opt_files",
                    "label": "Files / blobs",
                    "rationale": "Binary files, images, documents.",
                },
                {
                    "id": "opt_structured",
                    "label": "Structured / relational",
                    "rationale": "Tables, relations, SQL.",
                    "is_default": True,
                },
                {
                    "id": "opt_timeseries",
                    "label": "Time series",
                    "rationale": "Metrics, events over time.",
                },
                {
                    "id": "opt_events",
                    "label": "Events / logs",
                    "rationale": "Audit logs, activity streams.",
                },
                {
                    "id": "opt_graph",
                    "label": "Graph",
                    "rationale": "Relationships, social networks.",
                },
                {"id": "opt_other", "label": "Other", "rationale": "Specify your data type."},
            ],
            "depends_on": None,
        },
        {
            "sop_id": "P1.data.a.1",
            "question_text": "Do you have a preferred data storage tool or service?",
            "category": "architecture",
            "allow_multiple": True,
            "options": [
                {
                    "id": "opt_postgres",
                    "label": "PostgreSQL",
                    "rationale": "Full-featured relational DB.",
                    "is_default": True,
                },
                {"id": "opt_mysql", "label": "MySQL", "rationale": "Widely used relational DB."},
                {"id": "opt_mongodb", "label": "MongoDB", "rationale": "Document-oriented NoSQL."},
                {
                    "id": "opt_opensearch",
                    "label": "OpenSearch",
                    "rationale": "Search and analytics engine.",
                },
                {
                    "id": "opt_es",
                    "label": "ElasticSearch",
                    "rationale": "Full-text search and analytics.",
                },
                {
                    "id": "opt_s3",
                    "label": "S3 / object storage",
                    "rationale": "Scalable file/blob storage.",
                },
                {
                    "id": "opt_gcs",
                    "label": "Google Cloud Storage",
                    "rationale": "GCP object storage.",
                },
                {"id": "opt_neptune", "label": "Neptune", "rationale": "AWS managed graph DB."},
                {"id": "opt_couchdb", "label": "CouchDB", "rationale": "Distributed document DB."},
                {"id": "opt_other", "label": "Other", "rationale": "Specify your preference."},
            ],
            "depends_on": None,
        },
        {
            "sop_id": "P1.data.b",
            "question_text": "Does the system use events or data streaming?",
            "category": "architecture",
            "allow_multiple": False,
            "options": [
                {
                    "id": "opt_yes",
                    "label": "Yes",
                    "rationale": "System needs event/message streaming.",
                },
                {
                    "id": "opt_no",
                    "label": "No",
                    "rationale": "No streaming requirements.",
                    "is_default": True,
                },
                {"id": "opt_unsure", "label": "Unsure", "rationale": "Need to evaluate."},
            ],
            "depends_on": None,
        },
        {
            "sop_id": "P1.data.b.1",
            "question_text": "Which streaming tool/service do you prefer?",
            "category": "architecture",
            "allow_multiple": False,
            "options": [
                {
                    "id": "opt_kafka",
                    "label": "Kafka",
                    "rationale": "Industry standard for high-throughput streaming.",
                    "is_default": True,
                },
                {
                    "id": "opt_rabbitmq",
                    "label": "RabbitMQ",
                    "rationale": "Flexible message broker.",
                },
                {"id": "opt_kinesis", "label": "AWS Kinesis", "rationale": "Native AWS streaming."},
                {
                    "id": "opt_redis_streams",
                    "label": "Redis Streams",
                    "rationale": "Lightweight, integrated with Redis.",
                },
                {
                    "id": "opt_nats",
                    "label": "NATS",
                    "rationale": "Lightweight, cloud-native messaging.",
                },
                {"id": "opt_other", "label": "Other", "rationale": "Specify your preference."},
            ],
            "depends_on": {"P1.data.b": ["Yes"]},
        },
        {
            "sop_id": "P1.data.b.2",
            "question_text": "Does or should the system use event sourcing?",
            "category": "architecture",
            "allow_multiple": False,
            "options": [
                {
                    "id": "opt_yes",
                    "label": "Yes",
                    "rationale": "Full audit trail, replay capability.",
                },
                {
                    "id": "opt_no",
                    "label": "No",
                    "rationale": "Traditional CRUD is sufficient.",
                    "is_default": True,
                },
                {
                    "id": "opt_considering",
                    "label": "Considering it",
                    "rationale": "Need more analysis.",
                },
            ],
            "depends_on": {"P1.data.b": ["Yes"]},
        },
    ],
    SOPSubPhase.SECURITY: [
        {
            "sop_id": "P1.security.a",
            "question_text": "What auth/authorization service do you prefer?",
            "category": "security",
            "allow_multiple": False,
            "options": [
                {
                    "id": "opt_auth0",
                    "label": "Auth0",
                    "rationale": "Full-featured identity platform.",
                    "is_default": True,
                },
                {
                    "id": "opt_cognito",
                    "label": "AWS Cognito",
                    "rationale": "Native AWS auth service.",
                },
                {
                    "id": "opt_keycloak",
                    "label": "Keycloak",
                    "rationale": "Open-source identity management.",
                },
                {
                    "id": "opt_firebase",
                    "label": "Firebase Auth",
                    "rationale": "Simple auth for mobile/web.",
                },
                {"id": "opt_custom", "label": "Custom", "rationale": "Build your own auth system."},
                {"id": "opt_other", "label": "Other", "rationale": "Specify your preference."},
            ],
            "depends_on": None,
        },
        {
            "sop_id": "P1.security.b",
            "question_text": "What security tools do you prefer?",
            "category": "security",
            "allow_multiple": True,
            "options": [
                {
                    "id": "opt_sentry",
                    "label": "Sentry",
                    "rationale": "Error tracking and monitoring.",
                },
                {"id": "opt_waf", "label": "AWS WAF", "rationale": "Web application firewall."},
                {
                    "id": "opt_cloudflare",
                    "label": "Cloudflare",
                    "rationale": "CDN, DDoS protection, WAF.",
                },
                {
                    "id": "opt_snyk",
                    "label": "Snyk",
                    "rationale": "Dependency vulnerability scanning.",
                    "is_default": True,
                },
                {
                    "id": "opt_sonarqube",
                    "label": "SonarQube",
                    "rationale": "Code quality and security analysis.",
                },
                {"id": "opt_checkmarx", "label": "Checkmarx", "rationale": "SAST/DAST scanning."},
                {
                    "id": "opt_veracode",
                    "label": "Veracode",
                    "rationale": "Application security testing.",
                },
                {"id": "opt_other", "label": "Other", "rationale": "Specify your preference."},
            ],
            "depends_on": None,
        },
        {
            "sop_id": "P1.security.c.1",
            "question_text": "What key/secrets management solution do you prefer?",
            "category": "security",
            "allow_multiple": False,
            "options": [
                {
                    "id": "opt_vault",
                    "label": "HashiCorp Vault",
                    "rationale": "Industry standard secrets management.",
                    "is_default": True,
                },
                {
                    "id": "opt_aws_sm",
                    "label": "AWS Secrets Manager",
                    "rationale": "Native AWS secrets store.",
                },
                {
                    "id": "opt_gcp_sm",
                    "label": "Google Secret Manager",
                    "rationale": "Native GCP secrets store.",
                },
                {
                    "id": "opt_azure_kv",
                    "label": "Azure Key Vault",
                    "rationale": "Native Azure secrets store.",
                },
                {"id": "opt_other", "label": "Other", "rationale": "Specify your preference."},
            ],
            "depends_on": None,
        },
        {
            "sop_id": "P1.security.c.2",
            "question_text": "Do you prefer self-managed or cloud-managed key management?",
            "category": "security",
            "allow_multiple": False,
            "options": [
                {"id": "opt_self", "label": "Self-managed", "rationale": "Full control over keys."},
                {
                    "id": "opt_cloud",
                    "label": "Cloud-managed",
                    "rationale": "Lower operational burden.",
                    "is_default": True,
                },
                {
                    "id": "opt_hybrid",
                    "label": "Hybrid",
                    "rationale": "Mix of self and cloud managed.",
                },
            ],
            "depends_on": None,
        },
    ],
    SOPSubPhase.OBSERVABILITY: [
        {
            "sop_id": "P1.observability.a",
            "question_text": "What observability tools do you prefer?",
            "category": "infrastructure",
            "allow_multiple": True,
            "options": [
                {
                    "id": "opt_prometheus",
                    "label": "Prometheus",
                    "rationale": "Open-source metrics collection.",
                },
                {
                    "id": "opt_grafana",
                    "label": "Grafana",
                    "rationale": "Visualization and dashboards.",
                    "is_default": True,
                },
                {
                    "id": "opt_xray",
                    "label": "AWS X-Ray",
                    "rationale": "Distributed tracing for AWS.",
                },
                {
                    "id": "opt_cloudwatch",
                    "label": "CloudWatch",
                    "rationale": "Native AWS monitoring.",
                },
                {
                    "id": "opt_gcp_logging",
                    "label": "Google Cloud Logging",
                    "rationale": "Native GCP logging.",
                },
                {
                    "id": "opt_datadog",
                    "label": "Datadog",
                    "rationale": "Full-stack monitoring platform.",
                },
                {"id": "opt_newrelic", "label": "New Relic", "rationale": "APM and observability."},
                {"id": "opt_elk", "label": "ELK Stack", "rationale": "Open-source log analytics."},
                {"id": "opt_other", "label": "Other", "rationale": "Specify your preference."},
            ],
            "depends_on": None,
        },
    ],
    SOPSubPhase.SLA: [
        {
            "sop_id": "P1.sla.latency",
            "question_text": "What response time requirements or SLAs apply?",
            "category": "business",
            "allow_multiple": False,
            "options": [
                {
                    "id": "opt_1s",
                    "label": "< 1 second",
                    "rationale": "Fast interactive experience.",
                },
                {
                    "id": "opt_5s",
                    "label": "< 5 seconds",
                    "rationale": "Acceptable for most web apps.",
                    "is_default": True,
                },
                {
                    "id": "opt_15s",
                    "label": "< 15 seconds",
                    "rationale": "For batch or heavy operations.",
                },
                {
                    "id": "opt_realtime",
                    "label": "Real-time",
                    "rationale": "Sub-100ms, WebSocket/SSE.",
                },
                {
                    "id": "opt_none",
                    "label": "No specific requirement",
                    "rationale": "Standard best-effort.",
                },
            ],
            "depends_on": None,
        },
        {
            "sop_id": "P1.sla.robustness",
            "question_text": "What uptime and data loss requirements apply?",
            "category": "business",
            "allow_multiple": True,
            "options": [
                {"id": "opt_99_9", "label": "99.9% uptime", "rationale": "~8.7h downtime/year."},
                {"id": "opt_99_99", "label": "99.99% uptime", "rationale": "~52min downtime/year."},
                {
                    "id": "opt_rpo_4h",
                    "label": "RPO < 4 hours",
                    "rationale": "Max 4h data loss on failure.",
                },
                {
                    "id": "opt_rpo_1h",
                    "label": "RPO < 1 hour",
                    "rationale": "Max 1h data loss on failure.",
                },
                {
                    "id": "opt_rto_5m",
                    "label": "RTO < 5 minutes",
                    "rationale": "Rapid recovery from failure.",
                },
                {
                    "id": "opt_none",
                    "label": "No specific requirement",
                    "rationale": "Standard best-effort.",
                    "is_default": True,
                },
            ],
            "depends_on": None,
        },
    ],
    SOPSubPhase.BUDGET: [
        {
            "sop_id": "P1.budget.a",
            "question_text": "Is there a budget constraint for infrastructure/tooling?",
            "category": "business",
            "allow_multiple": False,
            "options": [
                {"id": "opt_yes", "label": "Yes", "rationale": "Budget is a factor in decisions."},
                {
                    "id": "opt_no",
                    "label": "No",
                    "rationale": "No specific budget constraint.",
                    "is_default": True,
                },
            ],
            "depends_on": None,
        },
        {
            "sop_id": "P1.budget.b",
            "question_text": "Is the budget flexible or rigid?",
            "category": "business",
            "allow_multiple": False,
            "options": [
                {
                    "id": "opt_flexible",
                    "label": "Flexible (can exceed if justified)",
                    "rationale": "Budget is a guideline.",
                    "is_default": True,
                },
                {
                    "id": "opt_rigid",
                    "label": "Rigid (hard maximum spend)",
                    "rationale": "Cannot exceed budget.",
                },
            ],
            "depends_on": {"P1.budget.a": ["Yes"]},
        },
    ],
    SOPSubPhase.PRIORITIES: [
        {
            "sop_id": "P1.priorities.a",
            "question_text": "What are the project priorities?",
            "category": "business",
            "allow_multiple": True,
            "options": [
                {
                    "id": "opt_resiliency",
                    "label": "Resiliency",
                    "rationale": "System reliability and fault tolerance.",
                },
                {
                    "id": "opt_performance",
                    "label": "Performance",
                    "rationale": "Speed and throughput.",
                },
                {"id": "opt_frugality", "label": "Frugality", "rationale": "Cost optimization."},
                {
                    "id": "opt_simplicity",
                    "label": "Simplicity",
                    "rationale": "Easy to build and maintain.",
                    "is_default": True,
                },
                {
                    "id": "opt_security",
                    "label": "Security",
                    "rationale": "Data protection and compliance.",
                },
                {"id": "opt_scalability", "label": "Scalability", "rationale": "Handle growth."},
                {"id": "opt_other", "label": "Other", "rationale": "Specify your priority."},
            ],
            "depends_on": None,
        },
        {
            "sop_id": "P1.priorities.b",
            "question_text": "Please rank your selected priorities from most to least important.",
            "category": "business",
            "allow_multiple": False,
            "options": [
                {
                    "id": "opt_security_first",
                    "label": "Security > Performance > Simplicity",
                    "rationale": "Security-first approach for sensitive applications.",
                },
                {
                    "id": "opt_performance_first",
                    "label": "Performance > Scalability > Simplicity",
                    "rationale": "Performance-oriented for high-traffic applications.",
                },
                {
                    "id": "opt_simplicity_first",
                    "label": "Simplicity > Frugality > Security",
                    "rationale": "Simplicity-first for rapid development and maintainability.",
                    "is_default": True,
                },
                {
                    "id": "opt_other",
                    "label": "Other",
                    "rationale": "Specify your custom priority ranking.",
                },
            ],
            "depends_on": None,
        },
    ],
}


def _sop_phase1_fallback_questions() -> List[OpenQuestion]:
    """Hardcoded fallback covering root questions from all 10 SOP sub-phases.

    Used when LLM-based spec extraction AND question generation both fail.
    Only includes root questions (no conditional follow-ups).
    Every question is guaranteed at least 3 options.

    Preconditions: none — takes no arguments; reads only the module-level
        ``SOP_PHASE1_QUESTIONS`` registry, whose entries must each carry
        ``sop_id`` and ``question_text`` (both required per the registry's
        invariants above).
    Postconditions: returns a new list with one ``OpenQuestion`` per question
        definition in ``SOP_PHASE1_QUESTIONS`` whose ``depends_on`` is ``None``
        (conditional/follow-up questions are excluded). Each returned question
        has ``source="sop_phase1"``, ``priority="high"``, ``sop_sub_phase`` set
        to the owning sub-phase's value, and at least ``MIN_OPTIONS`` (3) options
        — questions defined with fewer than 3 options are padded with an "Other"
        option and/or a free-text placeholder option to reach the minimum. Does
        not mutate ``SOP_PHASE1_QUESTIONS``.
    """
    MIN_OPTIONS = 3
    fallback: List[OpenQuestion] = []
    for sub_phase, questions in SOP_PHASE1_QUESTIONS.items():
        for q_def in questions:
            if q_def.get("depends_on") is not None:
                continue  # Skip conditional questions in fallback
            options = []
            for i, opt in enumerate(q_def.get("options", [])):
                options.append(
                    QuestionOption(
                        id=opt.get("id", f"opt{i}"),
                        label=opt["label"],
                        is_default=opt.get("is_default", False),
                        rationale=opt.get("rationale", ""),
                        confidence=0.5,
                    )
                )
            # Ensure at least MIN_OPTIONS options
            if len(options) < MIN_OPTIONS:
                # Add "Other" if not present
                if not any(o.label.lower() == "other" for o in options):
                    options.append(
                        QuestionOption(
                            id="opt_other",
                            label="Other",
                            is_default=False,
                            rationale="Specify your preference.",
                            confidence=0.3,
                        )
                    )
                # Add a free-text placeholder if still short
                if len(options) < MIN_OPTIONS:
                    options.insert(
                        0,
                        QuestionOption(
                            id="opt_text",
                            label="(Please type your answer)",
                            is_default=True,
                            rationale="",
                            confidence=0.5,
                        ),
                    )
            if not options:
                continue  # Should not happen after above logic, but guard anyway
            fallback.append(
                OpenQuestion(
                    id=q_def["sop_id"],
                    question_text=q_def["question_text"],
                    context="",
                    category=q_def.get("category", "general"),
                    priority="high",
                    allow_multiple=q_def.get("allow_multiple", False),
                    source="sop_phase1",
                    sop_sub_phase=sub_phase.value,
                    options=options,
                )
            )
    return fallback
