"""User Agent Founder lifecycle graph.

Topology::

    generate_spec → submit_analysis → execute_build → review

The FounderAgent autonomously generates specs, submits them for
analysis, answers questions, and triggers builds. HTTP polling loops
for external service completion remain outside the graph (they don't
map to Graph nodes — they're I/O waits, not agent decisions).
"""

from __future__ import annotations

from strands.multiagent.graph import Graph

from shared_graph import build_agent, build_sequential


def build_lifecycle_graph() -> Graph:
    """Build the founder lifecycle graph."""
    return build_sequential(
        stages=[
            ("generate_spec", build_agent(
                name="founder_spec_generator",
                system_prompt=(
                    "You are an autonomous founder agent. Generate a complete product "
                    "specification from the project vision. Include: problem statement, "
                    "target users, core features, technical requirements, and acceptance "
                    "criteria. Return structured JSON with the specification."
                ),
                description="Generates product specification from vision",
            )),
            ("submit_analysis", build_agent(
                name="founder_analyst",
                system_prompt=(
                    "You are a product analysis specialist. Review the generated spec "
                    "for completeness, feasibility, and clarity. Answer any clarification "
                    "questions that arise. Return JSON with: analysis_complete (bool), "
                    "answers to questions, and refined spec if needed."
                ),
                description="Analyzes and refines product spec",
            )),
            ("execute_build", build_agent(
                name="founder_executor",
                system_prompt=(
                    "You are a build execution coordinator. Submit the validated spec "
                    "to the software engineering team. Monitor progress and answer any "
                    "technical questions that arise during implementation. "
                    "Return JSON with: build_status, decisions_made."
                ),
                description="Coordinates build execution",
            )),
            ("review", build_agent(
                name="founder_reviewer",
                system_prompt=(
                    "You are a product review specialist. Review the build output against "
                    "the original specification. Verify all acceptance criteria are met. "
                    "Return JSON with: review_result, criteria_met, gaps."
                ),
                description="Reviews build output against spec",
            )),
        ],
        graph_id="founder_lifecycle",
        execution_timeout=600.0,
        node_timeout=180.0,
    )
