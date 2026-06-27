"""Testing/QA tool agent for frontend-code-v2: finds QA issues in review and fixes them one at a time."""

from __future__ import annotations

from typing import Dict

from strands import Agent  # noqa: F401  (kept so tests can monkeypatch this module's Agent)

from software_engineering_team.shared.tool_agent_base import (
    BaseReviewToolAgent,
    relevant_code_for_issue,
)

from ...models import ReviewIssue
from ...output_templates import parse_problem_solving_single_issue_template, parse_review_template
from ...prompts import PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT, QA_TOOL_AGENT_REVIEW_PROMPT

MAX_QA_CODE_CHARS = 12_000
MAX_RELEVANT_CODE_CHARS = 8_000


def _relevant_code_for_issue(issue: ReviewIssue, current_files: Dict[str, str]) -> str:
    """Return code context for a single issue: prefer issue's file, else first files."""
    return relevant_code_for_issue(issue, current_files, MAX_RELEVANT_CODE_CHARS)


class TestingQAToolAgent(BaseReviewToolAgent):
    """QA tool agent: finds testing/quality issues in review and fixes them one at a time in problem_solve."""

    name = "Testing/QA"
    empty_label = "QA issues"
    issue_source = "qa"
    problem_solve_sources = ("qa", "testing_qa", "tool_testing_qa")
    review_prompt = QA_TOOL_AGENT_REVIEW_PROMPT
    problem_solving_prompt = PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT
    max_code_chars = MAX_QA_CODE_CHARS
    max_relevant_code_chars = MAX_RELEVANT_CODE_CHARS
    review_parse_mode = "text"
    default_recommendation = "Fix the issue."
    plan_recommendations = ["Include unit and e2e tests in the plan."]
    plan_summary = "Testing/QA planning."
    _parse_review = staticmethod(parse_review_template)
    _parse_single_issue = staticmethod(parse_problem_solving_single_issue_template)
