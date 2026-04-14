"""Shared utilities for SE team graphs.

Provides agent factories and helpers specific to the SE team's
needs (workspace management, task context injection, etc.).
"""

from __future__ import annotations

from strands import Agent

from shared_graph import build_agent


def make_se_agent(
    *,
    name: str,
    system_prompt: str,
    agent_key: str = "coding_team",
    description: str = "",
    tools: list | None = None,
) -> Agent:
    """Create a Strands Agent configured for the SE team.

    Uses the SE team's agent_key for model resolution and adds
    SE-specific configuration.
    """
    return build_agent(
        name=name,
        system_prompt=system_prompt,
        agent_key=agent_key,
        description=description,
        tools=tools,
    )
