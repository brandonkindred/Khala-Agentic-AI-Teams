"""Testing/QA tool agent for backend-code-v2: finds QA issues in review for the coding agent to fix.

A thin declarative subclass of the shared
:class:`software_engineering_team.shared.testing_qa_tool_agent.SharedTestingQAToolAgent`.
Only the backend-specific prompt, parser, and plan recommendations live here.
"""

from __future__ import annotations

from strands import Agent  # noqa: F401  (kept so tests can monkeypatch this module's Agent)

from software_engineering_team.codegen_team.stacks.backend.profile import parse_review_template
from software_engineering_team.codegen_team.stacks.backend.prompts import (
    QA_TOOL_AGENT_REVIEW_PROMPT,
)
from software_engineering_team.shared.testing_qa_tool_agent import SharedTestingQAToolAgent

from ..base import BackendReviewToolAgent


class TestingQAToolAgent(SharedTestingQAToolAgent, BackendReviewToolAgent):
    """QA tool agent: finds testing/quality issues in review; reports them for the coding agent to fix."""

    review_prompt = QA_TOOL_AGENT_REVIEW_PROMPT
    plan_recommendations = ["Include unit and integration tests in the plan."]
    _parse_review = staticmethod(parse_review_template)
