"""
Template-based output parsing for frontend_code_v2_team.

Uses section-delimited text; no JSON. Same format as backend v2 for
microtasks, files, review, problem-solving. Language values: angular, react, typescript, javascript.

The parsing machinery is shared with backend v2 in
``software_engineering_team.shared.v2_output_templates``; this module binds it
to the frontend's path normalization (strip ``frontend/``) and language config.
"""

from __future__ import annotations

from software_engineering_team.shared.v2_output_templates import (
    _section as _section,
)
from software_engineering_team.shared.v2_output_templates import (
    make_output_templates,
)

# Re-exported for callers that import these names from this module.
__all__ = [
    "parse_files_and_summary_template",
    "parse_planning_template",
    "parse_review_template",
    "parse_problem_solving_template",
    "parse_problem_solving_single_issue_template",
    "parse_batch_fix_template",
    "parse_documentation_self_review_template",
]

_ALLOWED_LANGUAGES = ("angular", "react", "vue", "typescript", "javascript")

_templates = make_output_templates(
    path_prefixes=("frontend/", "./frontend/"),
    allowed_languages=_ALLOWED_LANGUAGES,
    default_language="typescript",
    coerce_unknown=False,
)

parse_files_and_summary_template = _templates.parse_files_and_summary_template
parse_planning_template = _templates.parse_planning_template
parse_review_template = _templates.parse_review_template
parse_problem_solving_template = _templates.parse_problem_solving_template
parse_problem_solving_single_issue_template = _templates.parse_problem_solving_single_issue_template
parse_batch_fix_template = _templates.parse_batch_fix_template
parse_documentation_self_review_template = _templates.parse_documentation_self_review_template
