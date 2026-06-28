"""Security tool agent for frontend-code-v2: finds security issues in review and fixes them one at a time."""

from __future__ import annotations

from strands import Agent  # noqa: F401  (kept so tests can monkeypatch this module's Agent)

from software_engineering_team.shared.tool_agent_base import ReviewToolAgent

from ...output_templates import parse_problem_solving_single_issue_template, parse_review_template
from ...prompts import PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT, SECURITY_TOOL_AGENT_REVIEW_PROMPT

MAX_SECURITY_CODE_CHARS = 12_000
MAX_RELEVANT_CODE_CHARS = 8_000


class SecurityToolAgent(ReviewToolAgent):
    """Security tool agent: finds security issues in review and fixes them one at a time in problem_solve."""

    name = "Security"
    empty_label = "security issues"
    issue_source = "security"
    problem_solve_sources = ("security", "tool_security")
    review_prompt = SECURITY_TOOL_AGENT_REVIEW_PROMPT
    problem_solving_prompt = PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT
    max_code_chars = MAX_SECURITY_CODE_CHARS
    max_relevant_code_chars = MAX_RELEVANT_CODE_CHARS
    review_parse_mode = "text"
    default_recommendation = "Fix the security issue."
    plan_recommendations = ["Consider XSS prevention, secure forms, and sensitive data handling."]
    plan_summary = "Security planning."
    _parse_review = staticmethod(parse_review_template)
    _parse_single_issue = staticmethod(parse_problem_solving_single_issue_template)
