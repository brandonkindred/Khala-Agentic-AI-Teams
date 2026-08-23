"""Security tool agent for backend-code-v2: finds security issues in review for the coding agent to fix."""

from __future__ import annotations

from strands import Agent  # noqa: F401  (kept so tests can monkeypatch this module's Agent)

from ...phases._profile import parse_review_template
from ...prompts import SECURITY_TOOL_AGENT_REVIEW_PROMPT
from ..base import BackendReviewToolAgent

MAX_RELEVANT_CODE_CHARS = 8_000


class SecurityToolAgent(BackendReviewToolAgent):
    """Security tool agent: finds security issues in review; reports them for the coding agent to fix."""

    name = "Security"
    empty_label = "security issues"
    issue_source = "security"
    review_prompt = SECURITY_TOOL_AGENT_REVIEW_PROMPT
    max_relevant_code_chars = MAX_RELEVANT_CODE_CHARS
    review_parse_mode = "text"
    plan_recommendations = ["Consider injection prevention, auth checks, and secure defaults."]
    plan_summary = "Security planning."
    _parse_review = staticmethod(parse_review_template)
