"""Safety and governance tool agent."""

from __future__ import annotations

from strands import Agent  # noqa: F401  (kept so tests can monkeypatch this module's Agent)

from .._base import JsonGeneratorToolAgent

PROMPT = """You are an expert AI safety and governance specialist.
Generate policy guards, approval gates, and risk controls.
Microtask: {microtask}
Spec context: {spec}
Return JSON with files/recommendations/summary.
"""


class SafetyGovernanceToolAgent(JsonGeneratorToolAgent):
    """Generates policy guards, approval gates, and risk-control artifacts."""

    PROMPT = PROMPT
