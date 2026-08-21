"""Documentation tool agent for backend-code-v2: reviews and updates documentation."""

from __future__ import annotations

from typing import Dict

from strands import Agent  # noqa: F401  (kept so tests can monkeypatch this module's Agent)

from software_engineering_team.shared.tool_agent_base import relevant_code_for_issue
from software_engineering_team.shared.tool_agent_documentation import (
    DocumentationToolAgentBase,
    extract_doc_files,
)

from ...models import ReviewIssue
from ...output_templates import parse_problem_solving_single_issue_template, parse_review_template
from ...prompts import (
    DOCUMENTATION_MICROTASK_PROMPT,
    DOCUMENTATION_PROBLEM_SOLVE_PROMPT,
    DOCUMENTATION_REVIEW_PROMPT,
)
from ..base import BackendReviewToolAgent

MAX_DOC_CODE_CHARS = 15_000
MAX_RELEVANT_CODE_CHARS = 10_000

DOC_PATTERNS = (
    "readme",
    "contributing",
    "changelog",
    "license",
    "docs/",
    "documentation",
    ".md",
    "api.md",
    "usage.md",
)


def _relevant_code_for_issue(issue: ReviewIssue, current_files: Dict[str, str]) -> str:
    """Return code context for a single issue: prefer issue's file, else first files."""
    return relevant_code_for_issue(issue, current_files, MAX_RELEVANT_CODE_CHARS)


def _extract_doc_files(files: Dict[str, str]) -> Dict[str, str]:
    """Extract documentation-related files (README, docs, etc.)."""
    return extract_doc_files(files, DOC_PATTERNS)


class DocumentationToolAgent(BackendReviewToolAgent, DocumentationToolAgentBase):
    """Documentation tool agent: reviews documentation completeness and updates docs.

    Inherits ``conventions_by_language`` from :class:`BackendReviewToolAgent`
    (the team's shared conventions profile) and documentation-specific behavior
    from :class:`DocumentationToolAgentBase`.
    """

    doc_patterns = DOC_PATTERNS
    max_doc_code_chars = MAX_DOC_CODE_CHARS
    max_relevant_code_chars = MAX_RELEVANT_CODE_CHARS
    plan_recommendations = [
        "Include README updates for new features.",
        "Document API changes and new endpoints.",
        "Add docstrings for all public functions, classes, and methods.",
        "Update CONTRIBUTORS.md if applicable.",
    ]
    microtask_prompt = DOCUMENTATION_MICROTASK_PROMPT
    doc_review_prompt = DOCUMENTATION_REVIEW_PROMPT
    doc_problem_solve_prompt = DOCUMENTATION_PROBLEM_SOLVE_PROMPT
    _parse_review = staticmethod(parse_review_template)
    _parse_single_issue = staticmethod(parse_problem_solving_single_issue_template)
