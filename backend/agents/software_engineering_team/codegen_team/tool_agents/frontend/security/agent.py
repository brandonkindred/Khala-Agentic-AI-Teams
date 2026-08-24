"""Security tool agent for frontend-code-v2: finds security issues in review for the coding agent to fix."""

from __future__ import annotations

from strands import Agent  # noqa: F401  (kept so tests can monkeypatch this module's Agent)

from software_engineering_team.codegen_team.stacks.frontend.profile import parse_review_template
from software_engineering_team.codegen_team.stacks.frontend.prompts import (
    SECURITY_TOOL_AGENT_REVIEW_PROMPT,
)
from software_engineering_team.shared.tool_agent_base import BaseReviewToolAgent

MAX_RELEVANT_CODE_CHARS = 8_000


class SecurityToolAgent(BaseReviewToolAgent):
    """Security tool agent: finds security issues in review; reports them for the coding agent to fix."""

    name = "Security"
    empty_label = "security issues"
    issue_source = "security"
    review_prompt = SECURITY_TOOL_AGENT_REVIEW_PROMPT
    max_relevant_code_chars = MAX_RELEVANT_CODE_CHARS
    review_parse_mode = "text"
    plan_recommendations = ["Consider XSS prevention, secure forms, and sensitive data handling."]
    plan_summary = "Security planning."
    _parse_review = staticmethod(parse_review_template)
