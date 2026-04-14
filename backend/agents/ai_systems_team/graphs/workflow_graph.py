"""AI Systems workflow graph — sequential 6-phase pipeline with skip-resume.

Wraps the existing procedural phase functions in a sequential Strands Graph
for consistent orchestration patterns. Each node is a lightweight Strands
Agent whose system prompt describes the phase output, but the actual work is
done by the existing phase functions injected via the task context.

Topology::

    spec_intake → architecture → capabilities → evaluation → safety → build

Skip-resume is handled at graph build time: completed phases are omitted
from the graph, and their results are pre-injected into the task context.
"""

from __future__ import annotations

from strands.multiagent.graph import Graph

from shared_graph import build_agent, build_sequential


def _make_phase_agent(phase_name: str, description: str) -> object:
    """Create a lightweight Strands Agent for an AI Systems phase."""
    system_prompt = (
        f"You are the {phase_name} specialist in an AI agent system design workflow.\n"
        f"Your role: {description}\n\n"
        f"Analyze the input from previous phases and produce the {phase_name} output.\n"
        f"Return your analysis as structured JSON."
    )
    return build_agent(
        name=f"ai_systems_{phase_name}",
        system_prompt=system_prompt,
        description=description,
    )


def build_workflow_graph(*, skip_phases: set | None = None) -> Graph:
    """Build the AI Systems workflow graph, optionally skipping completed phases.

    Parameters
    ----------
    skip_phases:
        Set of phase names to skip (already completed in a prior run).

    Returns
    -------
    Graph
        Sequential pipeline of remaining phases.
    """
    skip = skip_phases or set()

    all_stages = [
        ("spec_intake", "Parse specification and extract goals, constraints, and requirements"),
        ("architecture", "Design agent topology, orchestration strategy, and communication patterns"),
        ("capabilities", "Define agent capabilities, tools, and memory requirements"),
        ("evaluation", "Create evaluation framework with metrics and test scenarios"),
        ("safety", "Design safety guardrails, content policies, and ethical constraints"),
        ("build", "Generate implementation artifacts and deployment configuration"),
    ]

    stages = [
        (name, _make_phase_agent(name, desc))
        for name, desc in all_stages
        if name not in skip
    ]

    if not stages:
        stages = [all_stages[-1]]
        stages = [(stages[0][0], _make_phase_agent(stages[0][0], stages[0][1]))]

    return build_sequential(
        stages=stages,
        graph_id="ai_systems_workflow",
        execution_timeout=600.0,
        node_timeout=180.0,
    )
