"""Accessibility audit verification subgraph — three-lane parallel check.

Topology::

    assistive_tech_simulator ───┬──▶ verification_merger
    screen_layout_semantic ─────┤
    remediation_effort_estimator ┘

ATS, SLMS, and REE run in parallel, then their results are merged.
"""

from __future__ import annotations

from strands.multiagent.graph import Graph

from shared_graph import build_agent, build_fan_out_fan_in


def build_verification_subgraph() -> Graph:
    """Build the verification phase fan-out/fan-in graph."""
    return build_fan_out_fan_in(
        agents=[
            ("assistive_tech", build_agent(
                name="assistive_tech_simulator",
                system_prompt=(
                    "You are an assistive technology simulator (ATS). Test the application "
                    "with simulated screen readers (NVDA, JAWS, VoiceOver), switch access, "
                    "and voice control. Verify all interactive elements are accessible. "
                    "Return structured JSON with test results."
                ),
                description="Simulates assistive technology interaction",
            )),
            ("semantic_layout", build_agent(
                name="semantic_layout_checker",
                system_prompt=(
                    "You are a semantic layout and markup specialist (SLMS). Analyze HTML "
                    "structure, heading hierarchy, landmark regions, form labels, and table "
                    "markup for semantic correctness. Return structured JSON with issues."
                ),
                description="Checks semantic HTML and layout structure",
            )),
            ("remediation_estimator", build_agent(
                name="remediation_effort_estimator",
                system_prompt=(
                    "You are a remediation effort estimator (REE). For each accessibility "
                    "finding, estimate the engineering effort required to fix it: story points, "
                    "complexity level, affected components, and suggested fix approach. "
                    "Return structured JSON with effort estimates."
                ),
                description="Estimates remediation effort per finding",
            )),
        ],
        compositor=("verification_merger", build_agent(
            name="verification_merger",
            system_prompt=(
                "You are a verification results merger. Combine assistive tech test results, "
                "semantic analysis, and effort estimates into a unified verification report. "
                "Cross-reference findings to identify root causes. "
                "Return structured JSON with merged verification results."
            ),
            description="Merges all verification lane results",
        )),
        graph_id="a11y_verification",
        execution_timeout=300.0,
        node_timeout=120.0,
    )
