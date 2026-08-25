"""Shared prompt builders for the code-v2 teams.

See ``shared/prompts/templates.py`` for the builder implementations.
"""

from software_engineering_team.shared.prompts.requirement_citation import (
    REQUIREMENT_CITATION_GUARDRAIL,
)
from software_engineering_team.shared.prompts.templates import (
    DELIVER_COMMIT_MSG_TEMPLATE,
    DOCUMENTATION_PROBLEM_SOLVE_PROMPT,
    FILES_OUTPUT_TEMPLATE_INSTRUCTIONS,
    build_batch_fix_prompt,
    build_code_review_prompt,
    build_document_rewrite_prompt,
    build_documentation_self_review_prompt,
    build_execution_prompt,
    build_json_output_prompt,
    build_planning_prompt,
    build_problem_solving_prompt,
    build_problem_solving_single_issue_prompt,
    build_qa_review_prompt,
    format_context_block,
)

__all__ = [
    "DELIVER_COMMIT_MSG_TEMPLATE",
    "DOCUMENTATION_PROBLEM_SOLVE_PROMPT",
    "FILES_OUTPUT_TEMPLATE_INSTRUCTIONS",
    "REQUIREMENT_CITATION_GUARDRAIL",
    "build_batch_fix_prompt",
    "build_code_review_prompt",
    "build_document_rewrite_prompt",
    "build_documentation_self_review_prompt",
    "build_execution_prompt",
    "build_json_output_prompt",
    "build_planning_prompt",
    "build_problem_solving_prompt",
    "build_problem_solving_single_issue_prompt",
    "build_qa_review_prompt",
    "format_context_block",
]
