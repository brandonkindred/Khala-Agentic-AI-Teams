"""Testing/QA tool agent for frontend-code-v2: finds QA issues in review and fixes them one at a time.

A thin declarative subclass of the shared
:class:`software_engineering_team.shared.testing_qa_tool_agent.SharedTestingQAToolAgent`.
Only the frontend-specific prompts, parsers, and plan recommendations ("e2e
tests" rather than the backend's "integration tests") live here.
"""

from __future__ import annotations

from strands import Agent  # noqa: F401  (kept so tests can monkeypatch this module's Agent)

from software_engineering_team.shared.testing_qa_tool_agent import SharedTestingQAToolAgent

from ...output_templates import parse_problem_solving_single_issue_template, parse_review_template
from ...prompts import PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT, QA_TOOL_AGENT_REVIEW_PROMPT


class TestingQAToolAgent(SharedTestingQAToolAgent):
    """QA tool agent: finds testing/quality issues in review and fixes them one at a time in problem_solve."""

    review_prompt = QA_TOOL_AGENT_REVIEW_PROMPT
    problem_solving_prompt = PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT
    plan_recommendations = ["Include unit and e2e tests in the plan."]
    _parse_review = staticmethod(parse_review_template)
    _parse_single_issue = staticmethod(parse_problem_solving_single_issue_template)
