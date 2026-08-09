"""
Template-based output parsing for backend_code_v2_team.

Avoids reliance on JSON so that model output can be parsed reliably across
different providers and models. Uses section-delimited text that can be
parsed with simple string/regex extraction; partial or truncated output
can still yield useful results.

The parsing machinery is shared with frontend v2 in
``software_engineering_team.shared.v2_output_templates``; this module binds it
to the backend's path normalization (strip ``backend/``) and language config.
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

_ALLOWED_LANGUAGES = ("python", "java")

_templates = make_output_templates(
    path_prefixes=("backend/", "./backend/"),
    allowed_languages=_ALLOWED_LANGUAGES,
    default_language="python",
    coerce_unknown=True,
)

parse_files_and_summary_template = _templates.parse_files_and_summary_template
parse_planning_template = _templates.parse_planning_template
parse_review_template = _templates.parse_review_template
parse_problem_solving_template = _templates.parse_problem_solving_template
parse_problem_solving_single_issue_template = _templates.parse_problem_solving_single_issue_template
parse_batch_fix_template = _templates.parse_batch_fix_template
parse_documentation_self_review_template = _templates.parse_documentation_self_review_template
