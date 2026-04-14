"""Market research analysis graph.

Topology (split mode)::

    ux_research ──┬──▶ psychology ──┬──▶ viability_synthesis
                  └──▶ consistency ─┘

    scripts (independent entry, no downstream)

Topology (unified mode)::

    ux_research ──▶ psychology ──▶ viability_synthesis
    scripts (independent entry, no downstream)

UX research and scripts run in parallel as entry points.
Psychology and consistency (when enabled) run in parallel after UX research.
Viability synthesis fans in from both psychology and consistency.
"""

from __future__ import annotations

from strands.multiagent.graph import Graph, GraphBuilder

from shared_graph import build_agent

from ..agents import (
    _RESEARCH_SCRIPT_SYSTEM_PROMPT,
    _USER_PSYCHOLOGY_SYSTEM_PROMPT,
    _UX_RESEARCH_SYSTEM_PROMPT,
)
from ..prompts import CONSISTENCY_SYSTEM_PROMPT

_VIABILITY_SYNTHESIS_PROMPT = """\
You are a Business Viability Strategist who evaluates product concept viability based on \
market signals and interview evidence.

You will receive analysis from upstream agents: UX research insights, psychology signals, \
and optionally cross-interview consistency analysis.

Synthesize all upstream findings into a viability assessment.

## Verdict Rules
Use exactly one of these verdict strings:
- "insufficient_evidence" — not enough data to form a judgment
- "needs_more_validation" — some signal exists but not enough confidence
- "promising_with_risks" — strong signal with identified risks

## Output Format
Return ONLY a valid JSON object with:
- "verdict": string — one of the three verdict strings
- "confidence": float 0.0-1.0
- "rationale": array of strings — key reasons supporting the verdict
- "suggested_next_experiments": array of strings — prioritized next steps"""


def build_research_graph(*, include_consistency: bool = True) -> Graph:
    """Build the market research analysis graph.

    Parameters
    ----------
    include_consistency:
        When True (split mode), includes a consistency analyst node.
        When False (unified mode), psychology feeds directly to viability.

    Returns
    -------
    Graph
    """
    builder = GraphBuilder()
    builder.set_graph_id("market_research")
    builder.set_execution_timeout(600.0)
    builder.set_node_timeout(180.0)

    # UX Research: processes all transcripts, extracts insights
    ux = builder.add_node(
        build_agent(
            name="ux_research_analyst",
            system_prompt=_UX_RESEARCH_SYSTEM_PROMPT,
            description="Extracts user jobs, pain points, and desired outcomes from transcripts",
        ),
        node_id="ux_research",
    )
    builder.set_entry_point("ux_research")

    # Psychology: derives adoption/behavior signals from insights
    psych = builder.add_node(
        build_agent(
            name="psychology_analyst",
            system_prompt=_USER_PSYCHOLOGY_SYSTEM_PROMPT,
            description="Derives adoption and behavior-change signals",
        ),
        node_id="psychology",
    )
    builder.add_edge(ux, psych)

    # Viability synthesis: produces verdict and next experiments
    viability = builder.add_node(
        build_agent(
            name="viability_synthesis",
            system_prompt=_VIABILITY_SYNTHESIS_PROMPT,
            description="Synthesizes all findings into viability assessment",
        ),
        node_id="viability_synthesis",
    )
    builder.add_edge(psych, viability)

    if include_consistency:
        # Consistency: cross-interview theme analysis (parallel with psychology)
        consistency = builder.add_node(
            build_agent(
                name="consistency_analyst",
                system_prompt=CONSISTENCY_SYSTEM_PROMPT,
                description="Identifies recurring themes across interviews",
            ),
            node_id="consistency",
        )
        builder.add_edge(ux, consistency)
        builder.add_edge(consistency, viability)

    # Scripts: independent agent producing research artifacts
    builder.add_node(
        build_agent(
            name="research_scripts",
            system_prompt=_RESEARCH_SCRIPT_SYSTEM_PROMPT,
            description="Creates interview scripts and research templates",
        ),
        node_id="scripts",
    )
    builder.set_entry_point("scripts")

    return builder.build()
