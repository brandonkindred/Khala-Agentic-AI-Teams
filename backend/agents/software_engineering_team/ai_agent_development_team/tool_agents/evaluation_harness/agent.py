"""Evaluation harness tool agent."""

from __future__ import annotations

from strands import Agent  # noqa: F401  (kept so tests can monkeypatch this module's Agent)

from .._base import JsonGeneratorToolAgent

PROMPT = """You are an expert evaluation specialist for AI agent systems.
Create acceptance tests, adversarial tests, and KPI measurement artifacts.
Microtask: {microtask}
Spec context: {spec}
Return JSON with files/recommendations/summary.
"""


class EvaluationHarnessToolAgent(JsonGeneratorToolAgent):
    """Generates acceptance/adversarial tests and KPI measurement artifacts."""

    PROMPT = PROMPT
