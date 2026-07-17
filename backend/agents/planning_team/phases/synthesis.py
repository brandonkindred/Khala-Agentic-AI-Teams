"""
Synthesis phase: optional Market Research call, consolidate context and requirements.

Thin backward-compatible adapter over ``planning_team.agents.synthesis.SynthesisAgent``:
maps the ``context`` dict + evidence to the agent's typed Input and reconstructs the exact
``(context_update, artifacts)`` tuple (conditional keys preserved) from the typed Output.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


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
    from planning_team.agents.synthesis import SynthesisAgent, SynthesisInput

    out = SynthesisAgent().run(
        SynthesisInput(
            client_context=context.get("client_context"),
            market_research_evidence=market_research_evidence,
        )
    )
    context_update: Dict[str, Any] = {}
    if out.evidence_attached:
        context_update["market_research_evidence"] = out.evidence
        if out.client_context is not None:
            context_update["client_context"] = out.client_context
    artifacts: Dict[str, Any] = {"evidence": out.evidence}
    return context_update, artifacts
