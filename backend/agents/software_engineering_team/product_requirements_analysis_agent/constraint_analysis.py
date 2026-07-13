"""
Constraint-domain resolution analysis for the Product Requirements Analysis Agent.

A "constraint domain" (deployment, frontend, backend, database, auth) is resolved
progressively across up to four layers — from broad category down to specific
service. This module scans the spec text plus answered questions for keyword
indicators and reports, per domain, the deepest layer that has been resolved, then
turns that into LLM hint text so SOP Phase 1 asks only the *next* unresolved layer's
question per domain.

Extracted verbatim from ``agent.py`` to keep the workflow module focused on
orchestration. Pure functions with no LLM or I/O.
"""

from __future__ import annotations

import re
from typing import Dict, List

from .models import AnsweredQuestion

# ---------------------------------------------------------------------------
# Constraint Domain Definitions and Analysis
# ---------------------------------------------------------------------------

CONSTRAINT_DOMAINS_CONFIG = {
    "infrastructure": {
        "name": "Deployment/Hosting",
        "max_layer": 4,
        "indicators": {
            1: [  # Platform category
                ("heroku", 2),
                ("render", 2),
                ("railway", 2),  # PaaS → skip to L2
                ("aws", 2),
                ("gcp", 2),
                ("azure", 2),
                ("google cloud", 2),  # Cloud → L2
                ("self-hosted", 2),
                ("on-premises", 2),
                ("docker", 2),
                ("kubernetes", 2),
                ("vercel", 2),
                ("cloudflare", 2),
                ("netlify", 2),  # Edge → L2
                ("paas", 1),
                ("platform as a service", 1),
                ("cloud infrastructure", 1),
                ("cloud-based", 1),
                ("edge", 1),
                ("serverless", 1),
            ],
            2: [  # Specific provider
                ("heroku", 3),
                ("render", 3),
                ("railway", 3),
                ("fly.io", 3),
                ("aws", 3),
                ("amazon web services", 3),
                ("gcp", 3),
                ("google cloud platform", 3),
                ("azure", 3),
                ("microsoft azure", 3),
                ("digitalocean", 3),
                ("linode", 3),
                ("vercel", 3),
                ("cloudflare workers", 3),
                ("netlify", 3),
            ],
            3: [  # Compute model
                ("lambda", 4),
                ("cloud functions", 4),
                ("serverless", 4),
                ("ecs", 4),
                ("fargate", 4),
                ("cloud run", 4),
                ("container", 4),
                ("ec2", 4),
                ("compute engine", 4),
                ("vm", 4),
                ("virtual machine", 4),
                ("app runner", 4),
                ("elastic beanstalk", 4),
            ],
            4: [  # Specific services
                ("lambda", 4),
                ("api gateway", 4),
                ("step functions", 4),
                ("ecs fargate", 4),
                ("ecs ec2", 4),
                ("cloud run", 4),
                ("app engine", 4),
                ("app runner", 4),
            ],
        },
    },
    "frontend": {
        "name": "Frontend Technology",
        "max_layer": 4,
        "indicators": {
            1: [  # Rendering strategy
                ("spa", 1),
                ("single page", 1),
                ("client-side", 1),
                ("ssr", 1),
                ("server-side render", 1),
                ("server render", 1),
                ("ssg", 1),
                ("static site", 1),
                ("static generation", 1),
                ("hybrid", 1),
                ("no frontend", 4),
                ("api only", 4),
                ("headless", 4),
            ],
            2: [  # Framework
                ("react", 2),
                ("angular", 2),
                ("vue", 2),
                ("svelte", 2),
                ("vanilla", 2),
                ("no framework", 2),
            ],
            3: [  # Meta-framework
                ("next.js", 3),
                ("nextjs", 3),
                ("remix", 3),
                ("nuxt", 3),
                ("sveltekit", 3),
                ("create react app", 3),
                ("cra", 3),
                ("vite", 3),
                ("angular cli", 3),
            ],
            4: [  # Styling
                ("tailwind", 4),
                ("css modules", 4),
                ("styled-components", 4),
                ("scss", 4),
                ("sass", 4),
                ("emotion", 4),
                ("css-in-js", 4),
                ("bootstrap", 4),
                ("material ui", 4),
                ("mui", 4),
                ("chakra", 4),
            ],
        },
    },
    "backend": {
        "name": "Backend Technology",
        "max_layer": 4,
        "indicators": {
            1: [  # Architecture
                ("monolith", 1),
                ("microservice", 1),
                ("serverless function", 1),
                ("bff", 1),
                ("backend for frontend", 1),
            ],
            2: [  # Language
                ("python", 2),
                ("node", 2),
                ("nodejs", 2),
                ("typescript", 2),
                ("java", 2),
                ("kotlin", 2),
                ("go", 2),
                ("golang", 2),
                ("rust", 2),
                ("c#", 2),
                (".net", 2),
                ("ruby", 2),
            ],
            3: [  # Framework
                ("fastapi", 3),
                ("django", 3),
                ("flask", 3),
                ("express", 3),
                ("nestjs", 3),
                ("fastify", 3),
                ("koa", 3),
                ("spring", 3),
                ("spring boot", 3),
                ("quarkus", 3),
                ("gin", 3),
                ("echo", 3),
                ("fiber", 3),
                ("actix", 3),
                ("axum", 3),
                ("rocket", 3),
                ("rails", 3),
                ("ruby on rails", 3),
                ("asp.net", 3),
            ],
            4: [  # API style
                ("rest", 4),
                ("restful", 4),
                ("graphql", 4),
                ("grpc", 4),
                ("trpc", 4),
                ("websocket", 4),
            ],
        },
    },
    "database": {
        "name": "Database",
        "max_layer": 4,
        "indicators": {
            1: [  # Type
                ("relational", 1),
                ("sql", 1),
                ("document", 1),
                ("nosql", 1),
                ("key-value", 1),
                ("graph", 1),
                ("time-series", 1),
            ],
            2: [  # Hosting model
                ("rds", 2),
                ("cloud sql", 2),
                ("planetscale", 2),
                ("managed", 2),
                ("self-managed", 2),
                ("self-hosted", 2),
                ("serverless", 2),
                ("aurora serverless", 2),
                ("neon", 2),
            ],
            3: [  # Specific database
                ("postgresql", 3),
                ("postgres", 3),
                ("mysql", 3),
                ("mariadb", 3),
                ("mongodb", 3),
                ("dynamodb", 3),
                ("firestore", 3),
                ("redis", 3),
                ("cassandra", 3),
                ("neo4j", 3),
                ("sqlite", 3),
                ("supabase", 3),
            ],
            4: [  # Additional stores
                ("redis", 4),
                ("memcached", 4),
                ("caching", 4),
                ("elasticsearch", 4),
                ("opensearch", 4),
                ("algolia", 4),
                ("rabbitmq", 4),
                ("sqs", 4),
                ("kafka", 4),
                ("message queue", 4),
            ],
        },
    },
    "auth": {
        "name": "Authentication",
        "max_layer": 4,
        "indicators": {
            1: [  # Strategy
                ("third-party auth", 1),
                ("auth provider", 1),
                ("external auth", 1),
                ("custom auth", 1),
                ("self-built auth", 1),
                ("hybrid auth", 1),
            ],
            2: [  # Provider
                ("auth0", 2),
                ("clerk", 2),
                ("firebase auth", 2),
                ("cognito", 2),
                ("aws cognito", 2),
                ("supabase auth", 2),
                ("keycloak", 2),
                ("okta", 2),
                ("fusionauth", 2),
            ],
            3: [  # Methods
                ("oauth", 3),
                ("oidc", 3),
                ("openid", 3),
                ("email/password", 3),
                ("email password", 3),
                ("passwordless", 3),
                ("magic link", 3),
                ("otp", 3),
                ("sso", 3),
                ("saml", 3),
                ("ldap", 3),
                ("api key", 3),
            ],
            4: [  # Security features
                ("mfa", 4),
                ("2fa", 4),
                ("two-factor", 4),
                ("multi-factor", 4),
                ("session", 4),
                ("jwt", 4),
                ("token refresh", 4),
                ("rbac", 4),
                ("role-based", 4),
                ("permissions", 4),
            ],
        },
    },
}


def _word_boundary_match(indicator: str, text: str) -> bool:
    """Check if indicator appears as a whole word/phrase in text.

    Uses regex word boundaries to avoid false positives like 'gin' in 'login'.

    Preconditions: ``indicator`` and ``text`` are strings.
    Postconditions: returns ``True`` iff ``indicator`` occurs in ``text`` on
        word boundaries; never raises.
    """
    pattern = r"\b" + re.escape(indicator) + r"\b"
    return bool(re.search(pattern, text))


def analyze_constraint_status(
    spec_content: str,
    answered_questions: List[AnsweredQuestion],
) -> Dict[str, int]:
    """Analyze which constraint domains are resolved and to what layer.

    Scans the spec content and answered questions to determine the current
    resolution level for each constraint domain.

    Args:
        spec_content: The current specification content.
        answered_questions: List of questions that have been answered.

    Returns:
        Dict mapping domain name to resolved layer (0 = unresolved, 1-4 = layer resolved).

    Preconditions: ``spec_content`` is a string; ``answered_questions`` is a list
        of :class:`AnsweredQuestion`.
    Postconditions: the returned dict has exactly one key per domain in
        ``CONSTRAINT_DOMAINS_CONFIG``, each value clamped to ``[0, max_layer]``.
    """
    status: Dict[str, int] = {domain: 0 for domain in CONSTRAINT_DOMAINS_CONFIG}

    spec_lower = spec_content.lower()

    # Also include answered questions in the analysis
    answers_text = ""
    for aq in answered_questions:
        answers_text += f" {aq.question_text} {aq.selected_answer} "
    answers_lower = answers_text.lower()

    combined_text = spec_lower + " " + answers_lower

    for domain, config in CONSTRAINT_DOMAINS_CONFIG.items():
        max_resolved = 0
        indicators = config.get("indicators", {})

        # Check each layer's indicators using word boundary matching
        for layer in range(1, config["max_layer"] + 1):
            layer_indicators = indicators.get(layer, [])
            for indicator, resolves_to in layer_indicators:
                if _word_boundary_match(indicator, combined_text):
                    max_resolved = max(max_resolved, resolves_to)

        status[domain] = min(max_resolved, config["max_layer"])

    return status


def generate_constraint_hints(constraint_status: Dict[str, int]) -> str:
    """Generate hints for the LLM about which constraint layers need questions.

    Args:
        constraint_status: Dict mapping domain to resolved layer.

    Returns:
        Formatted string with hints about which domains need attention.

    Preconditions: ``constraint_status`` maps domain keys to resolved layers.
    Postconditions: returns an empty string when there are no domains, otherwise a
        Markdown hint block; never raises.
    """
    hints = []

    for domain, resolved_layer in constraint_status.items():
        config = CONSTRAINT_DOMAINS_CONFIG.get(domain, {})
        max_layer = config.get("max_layer", 4)
        domain_name = config.get("name", domain)

        if resolved_layer >= max_layer:
            hints.append(
                f"- {domain_name}: FULLY RESOLVED (Layer {max_layer}/{max_layer}) - No questions needed"
            )
        elif resolved_layer == 0:
            hints.append(
                f"- {domain_name}: UNRESOLVED - Ask Layer 1 question (start from the beginning)"
            )
        else:
            next_layer = resolved_layer + 1
            hints.append(
                f"- {domain_name}: Resolved to Layer {resolved_layer}/{max_layer} - Ask Layer {next_layer} question"
            )

    if not hints:
        return ""

    return (
        """## CONSTRAINT STATUS (from previous answers)

Based on analysis of the specification and previous answers, here is the current constraint resolution status:

"""
        + "\n".join(hints)
        + """

Focus your questions on domains that are NOT fully resolved. Ask ONLY the next layer question for each domain.
"""
    )
