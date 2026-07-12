"""Prompt engineering tool agent."""

from __future__ import annotations

from strands import Agent  # noqa: F401  (kept so tests can monkeypatch this module's Agent)

from .._base import JsonGeneratorToolAgent

PROMPT = """You are an expert prompt engineering specialist for multi-agent systems.
Create prompt artifacts for this microtask.
Microtask: {microtask}
Spec context: {spec}
Return JSON with files/recommendations/summary.
"""


class PromptEngineeringToolAgent(JsonGeneratorToolAgent):
    """Generates prompt artifacts for multi-agent systems."""

    PROMPT = PROMPT
