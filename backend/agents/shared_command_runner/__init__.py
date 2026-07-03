"""Neutral, team-agnostic command runner + build/test/lint error parsing.

This package is the single home for the subprocess command runner (frontend
build/serve, ``pytest``, linters, project scaffolding) and the build/test/lint
error parser that turns raw tool output into structured, agent-actionable
failures. It was promoted out of ``software_engineering_team.shared`` so that both
the software-engineering team and the coding team can depend on it without
importing one another.

Layout (mirrors the ``shared_*`` convention):
    - ``runner``        — the command runner (was ``shared/command_runner.py``)
    - ``error_parsing`` — the failure parser (was ``shared/error_parsing.py``)

Both submodules are stdlib-only at import time. ``runner`` lazily imports
``error_parsing`` (inside ``CommandResult.parsed_failures``); that is an
in-package import and carries no team dependency.

Preconditions:
    - ``backend/agents`` is on ``sys.path`` (the repo-wide convention for
      ``shared_*`` packages), so ``import shared_command_runner`` resolves.
Postconditions:
    - Importing this package has no side effects and starts no threads.
"""

from __future__ import annotations

from shared_command_runner.error_parsing import (
    FailureClass,
    ParsedFailure,
    build_agent_feedback,
    get_failure_class_tag,
    log_failure,
    normalize_error_signature,
    parse_command_failure,
    parse_devops_failure,
    parse_ng_build_failure,
    parse_pytest_failure,
)
from shared_command_runner.runner import (
    BUILD_TIMEOUT,
    SERVE_TIMEOUT,
    TEST_TIMEOUT,
    CommandResult,
    NvmInstallResult,
    detect_frontend_framework,
    ensure_backend_project_initialized,
    ensure_frontend_dependencies_installed,
    ensure_frontend_project_initialized,
    ensure_nvm_installed,
    is_ng_build_environment_failure,
    patch_json_file,
    patch_text_file,
    run_command,
    run_command_with_nvm,
    run_frontend_build,
    run_linter,
    run_ng_build,
    run_pytest,
    run_python_syntax_check,
)

__all__ = [
    # runner — subprocess primitives
    "CommandResult",
    "run_command",
    "run_command_with_nvm",
    "patch_json_file",
    "patch_text_file",
    # runner — frontend build/serve
    "detect_frontend_framework",
    "run_frontend_build",
    "run_ng_build",
    "is_ng_build_environment_failure",
    "NvmInstallResult",
    "ensure_nvm_installed",
    "ensure_frontend_dependencies_installed",
    # runner — backend test/lint/scaffold
    "run_pytest",
    "run_python_syntax_check",
    "run_linter",
    "ensure_frontend_project_initialized",
    "ensure_backend_project_initialized",
    # runner — timeouts
    "BUILD_TIMEOUT",
    "SERVE_TIMEOUT",
    "TEST_TIMEOUT",
    # error_parsing
    "FailureClass",
    "ParsedFailure",
    "parse_command_failure",
    "parse_pytest_failure",
    "parse_ng_build_failure",
    "parse_devops_failure",
    "build_agent_feedback",
    "normalize_error_signature",
    "get_failure_class_tag",
    "log_failure",
]
