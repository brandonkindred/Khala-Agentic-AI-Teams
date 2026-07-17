"""
Discovery phase: problem statement, opportunity, personas, success criteria.

Thin backward-compatible adapter over ``planning_team.agents.discovery.DiscoveryAgent``:
maps the ``context`` dict to the agent's typed Input, injects the ``llm`` tool, and maps
the typed Output back to the ``(context_update, artifacts)`` tuple. The real extraction
logic (map-reduce, prompt split, normalization) lives in the agent package.
"""

from __future__ import annotations

from typing import Any, Dict


def run_discovery(
    context: Dict[str, Any],
    llm: Any,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Run discovery phase using LLM to extract problem, opportunity, personas, success
    criteria, and any explicitly-required technology constraints.

    The whole brief+spec is digested via section-aware map-reduce (see
    ``planning_team.spec_digest``); no input is truncated.

    context should contain client_context, initial_brief, spec_content, and optionally evidence.
    Returns (context_update, artifacts).
    """
    from planning_team.agents.discovery import DiscoveryAgent, DiscoveryInput

    out = DiscoveryAgent().run(
        DiscoveryInput(
            client_context=context.get("client_context"),
            initial_brief=context.get("initial_brief"),
            spec_content=context.get("spec_content"),
        ),
        llm,
    )
    return {"client_context": out.client_context}, {"discovery": out.discovery}
