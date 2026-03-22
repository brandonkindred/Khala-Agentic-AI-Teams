"""
Synthesis phase: optional Market Research call, consolidate context and requirements.

Merges evidence (e.g. from Market Research) into client context.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from planning_v3_team.models import ClientContext


def run_synthesis(
    context: Dict[str, Any],
    market_research_evidence: Optional[Dict[str, Any]] = None,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Run synthesis: merge market research (or other) evidence into context.

    If market_research_evidence is provided (e.g. from adapters.market_research),
    attach it to context and optionally fold summary/insights into client_context.
    Returns (context_update, artifacts).
    """
    context_update: Dict[str, Any] = {}
    artifacts: Dict[str, Any] = {"evidence": market_research_evidence}

    if not market_research_evidence:
        return context_update, artifacts

    context_update["market_research_evidence"] = market_research_evidence
    client_context = context.get("client_context")
    if isinstance(client_context, dict):
        client_context = ClientContext(**client_context)
    if client_context is None:
        return context_update, artifacts

    summary = market_research_evidence.get("summary")
    insights = market_research_evidence.get("insights", [])
    if summary or insights:
        constraints = dict(client_context.constraints or {})
        constraints["market_research_summary"] = summary or ""
        constraints["market_research_insights"] = insights
        dump = client_context.model_dump()
        dump["constraints"] = constraints
        updated = ClientContext(**dump)
        context_update["client_context"] = updated

    return context_update, artifacts
