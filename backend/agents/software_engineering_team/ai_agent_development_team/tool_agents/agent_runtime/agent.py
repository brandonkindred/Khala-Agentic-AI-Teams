"""Runtime integration tool agent."""

from __future__ import annotations

from strands import Agent  # noqa: F401  (kept so tests can monkeypatch this module's Agent)

from .._base import JsonGeneratorToolAgent

PROMPT = """You are an agent runtime integration specialist.
Design orchestrator runtime wiring, tool contracts, retries, and observability hooks.
Microtask: {microtask}
Spec context: {spec}
Return JSON with files/recommendations/summary.
"""


class AgentRuntimeToolAgent(JsonGeneratorToolAgent):
    """Generates orchestrator runtime wiring, tool contracts, and observability artifacts."""

    PROMPT = PROMPT
