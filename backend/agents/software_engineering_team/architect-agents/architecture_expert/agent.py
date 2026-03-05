"""Architecture Expert agent: designs system architecture from requirements."""

from __future__ import annotations

import logging
from typing import Any, Dict

from software_engineering_team.shared.llm import LLMClient, LLMPermanentError
from software_engineering_team.shared.models import ArchitectureComponent, ProductRequirements, SystemArchitecture

from .models import ArchitectureInput, ArchitectureOutput
from .prompts import ARCHITECTURE_PROMPT

logger = logging.getLogger(__name__)

REQUIRED_DIAGRAM_KEYS = [
    "client_server_architecture",
    "frontend_code_structure",
    "backend_code_structure",
    "backend_infrastructure",
    "infrastructure",
    "security_architecture",
]


def _default_diagram(name: str, reqs: ProductRequirements) -> str:
    """Return a minimal but valid Mermaid diagram for the given key."""
    base_title = (reqs.title or "App").replace(" ", "")
    if name == "client_server_architecture":
        return (
            "flowchart TD\n"
            f"  browserClient[{base_title}Client] --> appApi[{base_title}API]\n"
            "  appApi --> primaryDb[(PrimaryDB)]"
        )
    if name == "frontend_code_structure":
        return (
            "flowchart TD\n"
            "  appShell[AppShell] --> pages[Pages]\n"
            "  pages --> sharedComponents[SharedComponents]\n"
            "  sharedComponents --> services[UiServices]"
        )
    if name == "backend_code_structure":
        return (
            "flowchart TD\n"
            "  apiEntry[ApiEntry] --> routers[Routers]\n"
            "  routers --> services[Services]\n"
            "  services --> repositories[Repositories]\n"
            "  repositories --> models[OrmModels]"
        )
    if name == "backend_infrastructure":
        return (
            "flowchart TD\n"
            "  clientApp[Client] --> apiGateway[ApiGateway]\n"
            "  apiGateway --> appServer[AppServer]\n"
            "  appServer --> dbPrimary[(DBPrimary)]\n"
            "  appServer --> cacheLayer[(CacheLayer)]"
        )
    if name == "infrastructure":
        return (
            "flowchart TD\n"
            "  devEnv[DevEnv] --> cicd[CiCd]\n"
            "  cicd --> stagingEnv[StagingEnv]\n"
            "  stagingEnv --> prodEnv[ProdEnv]"
        )
    if name == "security_architecture":
        return (
            "flowchart TD\n"
            "  userAgent[UserAgent] --> authService[AuthService]\n"
            "  authService --> tokenStore[TokenStore]\n"
            "  authService --> policyEngine[PolicyEngine]\n"
            "  policyEngine --> securedApis[SecuredApis]"
        )
    return "flowchart TD\n  start[Start] --> end[End]"


def _build_synthetic_architecture_data(reqs: ProductRequirements) -> Dict[str, Any]:
    """Build a fully synthetic architecture dict from requirements when LLM parse fails."""
    overview = (
        f"System for {reqs.title} with focus on: "
        f"{'; '.join(reqs.acceptance_criteria[:3]) or reqs.description[:120]}"
    )
    components = [
        {"name": "Backend API", "type": "backend", "description": "API layer", "technology": "fastapi"},
        {"name": "Frontend App", "type": "frontend", "description": "UI layer", "technology": "react"},
        {"name": "Database", "type": "database", "description": "Primary data store", "technology": "postgresql"},
    ]
    bullets = ["- Components: Backend API, Frontend App, Database"]
    if reqs.constraints:
        bullets.append("- Key constraints: " + ", ".join(reqs.constraints[:5]))
    architecture_document = "\n".join(
        [
            f"# Architecture for {reqs.title}",
            "",
            overview,
            "",
            "## High-level notes",
            "",
            *bullets,
        ]
    )
    diagrams = {k: _default_diagram(k, reqs) for k in REQUIRED_DIAGRAM_KEYS}
    return {
        "overview": overview,
        "components": components,
        "architecture_document": architecture_document,
        "diagrams": diagrams,
        "decisions": [],
        "summary": f"Synthetic architecture for {reqs.title}",
    }


class ArchitectureExpertAgent:
    """
    Staff-level Software Architecture Expert. Uses product requirements to design
    a system architecture that DevOps, Security, Backend, Frontend, and QA agents
    reference when implementing or validating changes.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, input_data: ArchitectureInput) -> ArchitectureOutput:
        """Design system architecture from requirements."""
        logger.info("Architecture Expert: starting design for %s", input_data.requirements.title)
        reqs = input_data.requirements
        context_parts = [
            f"**Product Title:** {reqs.title}",
            f"**Description:** {reqs.description}",
            "**Acceptance Criteria:**",
            *[f"- {c}" for c in reqs.acceptance_criteria],
            "**Constraints:**",
            *[f"- {c}" for c in reqs.constraints],
            f"**Priority:** {reqs.priority}",
        ]
        if input_data.project_overview:
            po = input_data.project_overview
            context_parts.extend([
                "",
                "**Project Overview (use to align architecture with delivery strategy):**",
                f"- Primary goal: {po.get('primary_goal', '')}",
                f"- Delivery strategy: {po.get('delivery_strategy', '')}",
                "- Milestones: " + ", ".join(m.get("name", "") for m in po.get("milestones", [])),
            ])
        if input_data.features_and_functionality_doc and input_data.features_and_functionality_doc.strip():
            context_parts.extend([
                "",
                "**Features and Functionality (architecture must support all of these):**",
                "---",
                input_data.features_and_functionality_doc.strip()[:12000]
                + ("..." if len(input_data.features_and_functionality_doc) > 12000 else ""),
                "---",
            ])
        if input_data.planning_feedback:
            context_parts.extend([
                "",
                "**Planning review feedback – adjust architecture to address these:**",
                *[f"- {f}" for f in input_data.planning_feedback],
            ])
        if input_data.existing_architecture:
            context_parts.extend(["", "**Existing Architecture to extend:**", input_data.existing_architecture])
        if input_data.technology_preferences:
            context_parts.extend(["", "**Technology Preferences:**", ", ".join(input_data.technology_preferences)])

        prompt = ARCHITECTURE_PROMPT + "\n\n---\n\n" + "\n".join(context_parts)

        try:
            data: Dict[str, Any] = self.llm.complete_json(prompt, temperature=0.2) or {}
        except LLMPermanentError:
            logger.warning(
                "Architecture Expert: LLM returned non-JSON response, falling back to synthetic architecture"
            )
            data = {}

        # Detect raw wrapper from JSON parse failure (only "content" key, or no "overview")
        is_parse_failure = (
            not data.get("overview")
            or (len(data) == 1 and "content" in data)
        )
        if is_parse_failure:
            logger.debug(
                "Architecture Expert: LLM response unparseable or missing structure, building synthetic architecture from requirements"
            )
            data = _build_synthetic_architecture_data(reqs)

        # Validate required top-level keys and log if any are missing (DEBUG when we have fallbacks)
        required_keys = ["overview", "components", "architecture_document", "diagrams", "decisions", "summary"]
        missing = [k for k in required_keys if k not in data]
        if missing:
            logger.debug("Architecture Expert: LLM response missing keys: %s", ", ".join(missing))

        raw_components = data.get("components") or []
        components = []
        for idx, c in enumerate(raw_components):
            if not isinstance(c, dict):
                continue
            # Backfill defaults so every component is usable by planners
            name = c.get("name") or c.get("id") or c.get("label")
            if not name:
                fallback_base = c.get("type") or "component"
                name = f"{fallback_base}-{idx + 1}"
            c_type = c.get("type") or "unknown"
            description = c.get("description") or f"{c_type} component {name}"
            raw_interfaces = c.get("interfaces") or []
            interfaces: list[str] = []
            for iface in raw_interfaces:
                if isinstance(iface, str):
                    interfaces.append(iface)
                elif isinstance(iface, dict):
                    interfaces.append(iface.get("name") or str(iface))
            raw_deps = c.get("dependencies") or []
            deps = [d if isinstance(d, str) else (d.get("name") or str(d)) for d in raw_deps]

            components.append(
                ArchitectureComponent(
                    name=name,
                    type=c_type,
                    description=description,
                    technology=c.get("technology"),
                    dependencies=deps,
                    interfaces=interfaces,
                )
            )

        # Synthetic overview / document fallbacks when the model returns blanks
        overview = (data.get("overview") or "").strip()
        if not overview:
            overview = (
                f"System for {reqs.title} with focus on: "
                f"{'; '.join(reqs.acceptance_criteria[:3]) or reqs.description[:120]}"
            )

        architecture_document = (data.get("architecture_document") or "").strip()
        if not architecture_document:
            bullets = []
            if components:
                bullets.append(
                    "- Components: "
                    + ", ".join(f"{c.name} ({c.type})" for c in components[:8])
                )
            if reqs.constraints:
                bullets.append("- Key constraints: " + ", ".join(reqs.constraints[:5]))
            architecture_document = "\n".join(
                [
                    f"# Architecture for {reqs.title}",
                    "",
                    overview,
                    "",
                    "## High-level notes",
                    "",
                    *bullets,
                ]
            )

        # Ensure diagrams cover the required keys with valid Mermaid defaults
        diagrams = data.get("diagrams") or {}
        if not isinstance(diagrams, dict):
            diagrams = {}

        missing_diagrams = [k for k in REQUIRED_DIAGRAM_KEYS if not str(diagrams.get(k) or "").strip()]
        if missing_diagrams:
            logger.info("Architecture Expert: backfilling %d missing diagram(s)", len(missing_diagrams))
        for key in REQUIRED_DIAGRAM_KEYS:
            value = str(diagrams.get(key) or "").strip()
            if not value:
                diagrams[key] = _default_diagram(key, reqs)

        # Derive lightweight planning hints from components for downstream planners
        backend_components = [c.name for c in components if c.type in ("backend", "api", "api_gateway", "database")]
        frontend_components = [c.name for c in components if c.type in ("frontend", "ui", "client")]
        infra_components = [c.name for c in components if c.type in ("infrastructure", "queue", "cache", "cdn")]

        planning_hints = {
            "backend": {
                "components": backend_components,
            },
            "frontend": {
                "components": frontend_components,
            },
            "infra": {
                "components": infra_components,
            },
        }

        tenancy_model = (data.get("tenancy_model") or "").strip()
        reliability_model = (data.get("reliability_model") or "").strip()

        architecture = SystemArchitecture(
            overview=overview,
            components=components,
            architecture_document=architecture_document,
            diagrams=diagrams,
            decisions=data.get("decisions", []),
            tenancy_model=tenancy_model,
            reliability_model=reliability_model,
            planning_hints=planning_hints,
        )

        logger.info("Architecture Expert: done, %s components", len(architecture.components))
        return ArchitectureOutput(
            architecture=architecture,
            summary=data.get("summary", ""),
        )
