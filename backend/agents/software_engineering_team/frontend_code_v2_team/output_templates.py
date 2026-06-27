"""
Template-based output parsing for frontend_code_v2_team.

Uses section-delimited text; no JSON. Same format as backend v2 for
microtasks, files, review, problem-solving. Language values: angular, react, typescript, javascript.

The parsing machinery is shared with backend v2 in
``software_engineering_team.shared.v2_output_templates``; this module binds it
to the frontend's path normalization (strip ``frontend/``) and language config.
"""

from __future__ import annotations

import functools
from typing import Any, Dict

from software_engineering_team.shared import v2_output_templates as _shared
from software_engineering_team.shared.v2_output_templates import (
    _section as _section,
)
from software_engineering_team.shared.v2_output_templates import (
    parse_review_template as parse_review_template,
)

# Re-exported for callers that import these names from this module.
__all__ = [
    "parse_files_and_summary_template",
    "parse_files_with_validation",
    "parse_planning_template",
    "parse_review_template",
    "parse_problem_solving_template",
    "parse_problem_solving_single_issue_template",
    "parse_batch_fix_template",
    "parse_documentation_self_review_template",
]

_ALLOWED_LANGUAGES = ("angular", "react", "vue", "typescript", "javascript")


def _normalize_file_path(path: str) -> str:
    """Strip redundant frontend/ prefix from path.

    The frontend team operates within the frontend directory, so LLM output
    paths like 'frontend/src/app/...' should be normalized to 'src/app/...'.
    """
    prefixes_to_strip = ("frontend/", "./frontend/")
    for prefix in prefixes_to_strip:
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path


def parse_files_and_summary_template(text: str) -> Dict[str, Any]:
    return _shared.parse_files_and_summary_template(text, normalize=_normalize_file_path)


def parse_files_with_validation(text: str):
    return _shared.parse_files_with_validation(text, normalize=_normalize_file_path)


def parse_planning_template(text: str) -> Dict[str, Any]:
    return _shared.parse_planning_template(
        text,
        default_language="typescript",
        allowed_languages=_ALLOWED_LANGUAGES,
        coerce_unknown=False,
    )


parse_problem_solving_template = functools.partial(
    _shared.parse_problem_solving_template, normalize=_normalize_file_path
)
parse_problem_solving_single_issue_template = functools.partial(
    _shared.parse_problem_solving_single_issue_template, normalize=_normalize_file_path
)
parse_batch_fix_template = functools.partial(
    _shared.parse_batch_fix_template, normalize=_normalize_file_path
)
parse_documentation_self_review_template = functools.partial(
    _shared.parse_documentation_self_review_template, normalize=_normalize_file_path
)
