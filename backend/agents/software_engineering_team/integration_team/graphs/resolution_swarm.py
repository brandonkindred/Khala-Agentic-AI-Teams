"""Integration conflict resolution swarm.

Upgrades the integration phase from detection-only to self-healing.
The current IntegrationAgent detects API contract mismatches but its
fix_task_suggestions are advisory and never consumed.

This swarm enables agents to reason about conflicts and apply fixes:

    integration_triage (entry) ←→ contract_arbiter ←→ backend_fixer
                                                    ←→ frontend_fixer
                                                    ←→ contract_updater
                                                    ←→ integration_verifier

The triage agent classifies conflicts. The arbiter determines the
canonical contract. Fixers apply changes. The verifier confirms
resolution.
"""

from __future__ import annotations

from strands.multiagent.swarm import Swarm

from shared_graph import build_agent


def build_resolution_swarm(*, max_handoffs: int = 8) -> Swarm:
    """Build the integration conflict resolution swarm.

    Parameters
    ----------
    max_handoffs:
        Maximum agent-to-agent handoffs (prevents infinite loops).
    """
    triage = build_agent(
        name="integration_triage",
        system_prompt=(
            "You are an integration triage specialist. Analyze backend and frontend code "
            "for API contract mismatches: missing endpoints, payload schema differences, "
            "HTTP method mismatches, and authentication inconsistencies.\n\n"
            "Classify each conflict by severity (breaking/degraded/cosmetic) and type "
            "(endpoint_missing, schema_mismatch, auth_inconsistency, etc.).\n"
            "Hand off to contract_arbiter for resolution of breaking conflicts.\n"
            "Return JSON with: conflicts array, severity_summary."
        ),
        agent_key="coding_team",
        description="Triages integration conflicts by severity",
    )

    arbiter = build_agent(
        name="contract_arbiter",
        system_prompt=(
            "You are an API contract arbiter. For each conflict, determine the canonical "
            "contract based on: spec requirements, existing tests, and common patterns.\n"
            "Decide which side (backend or frontend) needs to change.\n"
            "Hand off to backend_fixer or frontend_fixer as appropriate.\n"
            "Return JSON with: resolutions array, each with target (backend/frontend) "
            "and fix_description."
        ),
        agent_key="coding_team",
        description="Determines canonical API contract",
    )

    backend_fixer = build_agent(
        name="backend_fixer",
        system_prompt=(
            "You are a backend integration fixer. Apply the arbiter's resolution to "
            "backend code: update endpoints, adjust schemas, fix response formats.\n"
            "After applying fixes, hand off to integration_verifier.\n"
            "Return JSON with: files_changed, summary."
        ),
        agent_key="coding_team",
        description="Applies fixes to backend API code",
    )

    frontend_fixer = build_agent(
        name="frontend_fixer",
        system_prompt=(
            "You are a frontend integration fixer. Apply the arbiter's resolution to "
            "frontend code: update API client calls, adjust TypeScript interfaces, "
            "fix request/response handling.\n"
            "After applying fixes, hand off to integration_verifier.\n"
            "Return JSON with: files_changed, summary."
        ),
        agent_key="coding_team",
        description="Applies fixes to frontend API code",
    )

    contract_updater = build_agent(
        name="contract_updater",
        system_prompt=(
            "You are an API contract documentation specialist. Update OpenAPI specs, "
            "TypeScript interfaces, and Pydantic models to reflect the resolved contract.\n"
            "Return JSON with: updated_contracts, files_changed."
        ),
        agent_key="coding_team",
        description="Updates API contract documentation",
    )

    verifier = build_agent(
        name="integration_verifier",
        system_prompt=(
            "You are an integration verifier. After fixes are applied, verify that "
            "backend and frontend contracts now align. Check all previously identified "
            "conflicts are resolved.\n"
            "If issues remain, hand back to contract_arbiter.\n"
            "If all clear, declare RESOLVED.\n"
            "Return JSON with: verified (bool), remaining_issues array."
        ),
        agent_key="coding_team",
        description="Verifies integration conflicts are resolved",
    )

    return Swarm(
        nodes=[triage, arbiter, backend_fixer, frontend_fixer, contract_updater, verifier],
        entry_point=triage,
        max_handoffs=max_handoffs,
        execution_timeout=300.0,
    )
