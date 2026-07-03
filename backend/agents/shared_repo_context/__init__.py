"""Neutral, team-agnostic repository scanning / context utilities.

Single home for reading source files out of a repository (``read_repo_code``),
the shared extension / exclude-dir constants that keep every repo scanner
consistent, and the sensitive-path and file-dict helpers used when preparing repo
content for review. Promoted out of ``software_engineering_team.shared.repo_utils``
so both the software-engineering team and the coding team can depend on it without
importing one another.

Layout:
    - ``repo_utils`` — the scanner, constants, and helpers (was
      ``software_engineering_team/shared/repo_utils.py``).

Preconditions:
    - ``backend/agents`` is on ``sys.path`` (the ``shared_*`` convention).
Postconditions:
    - Importing this package has no side effects. ``truncate_for_context``
      lazily imports ``llm_service`` (a neutral module) only when called with an
      LLM handle; nothing here imports a specific team.
"""

from __future__ import annotations

from shared_repo_context.repo_utils import (
    BACKEND_EXTENSIONS,
    DOCUMENTATION_EXTENSIONS,
    FRONTEND_EXTENSIONS,
    FULL_STACK_EXTENSIONS,
    REPO_EXCLUDE_DIRS,
    REPO_INSPECT_EXCLUDE_DIRS,
    REPO_INSPECT_EXTRA_EXCLUDE_DIRS,
    int_env,
    is_secret_template_path,
    is_sensitive_path,
    read_files_as_dict,
    read_repo_code,
    read_repo_code_budgeted,
    read_repo_files_as_dict,
    sanitize_path_for_text,
    strip_surrogates,
    truncate_for_context,
)

__all__ = [
    # constants
    "REPO_EXCLUDE_DIRS",
    "REPO_INSPECT_EXTRA_EXCLUDE_DIRS",
    "REPO_INSPECT_EXCLUDE_DIRS",
    "BACKEND_EXTENSIONS",
    "FRONTEND_EXTENSIONS",
    "FULL_STACK_EXTENSIONS",
    "DOCUMENTATION_EXTENSIONS",
    # scanner + helpers
    "read_repo_code",
    "read_repo_code_budgeted",
    "read_files_as_dict",
    "read_repo_files_as_dict",
    "truncate_for_context",
    "is_sensitive_path",
    "is_secret_template_path",
    "strip_surrogates",
    "sanitize_path_for_text",
    "int_env",
]
