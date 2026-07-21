"""Neutral, team-agnostic command runner + build/test/lint error parsing.

This package is the single home for the subprocess command runner (frontend
build/serve, ``pytest``, linters, project scaffolding) and the build/test/lint
error parser that turns raw tool output into structured, agent-actionable
failures. It was promoted out of ``software_engineering_team.shared`` so that both
the software-engineering team and the coding team can depend on it without
importing one another.

Layout:
    - ``runner``         — core: subprocess primitives, frontend framework
      detection, ``run_pytest``/``run_python_syntax_check``/``run_linter``
    - ``nvm``             — NVM (Node Version Manager) install/detect and
      running commands under a managed Node version
    - ``angular_repair``  — best-effort fixes applied to an Angular project
      before a build (package.json deps, tsconfig, app.config.ts, ...)
    - ``scaffolding``     — writes a minimal Angular/React/FastAPI project
      skeleton
    - ``smoke_test``      — starts a frontend dev server briefly to confirm
      it compiles and runs
    - ``error_parsing``   — the failure parser (was ``shared/error_parsing.py``)

``runner`` is the dependency-free base (stdlib-only at import time). The other
submodules import from sibling modules at load time along an acyclic graph:
``runner`` <- ``nvm`` <- ``angular_repair`` / ``scaffolding`` / ``smoke_test``.
``runner`` itself lazily imports ``error_parsing``
(inside ``CommandResult.parsed_failures``) and the two leaf modules it
dispatches to from ``run_frontend_build``/``run_linter``; ``scaffolding``
lazily imports ``shared.git`` (inside ``ensure_backend_project_initialized``).
All are in-package or in-platform imports and carry no team dependency.

Preconditions:
    - ``backend/`` is on ``sys.path`` (the repo-wide convention for
      ``shared.*`` packages), so ``import shared.command_runner`` resolves.
Postconditions:
    - Importing this package has no side effects and starts no threads.
"""

from __future__ import annotations

from shared.command_runner.angular_repair import (
    ensure_frontend_dependencies_installed,
    run_ng_build_with_nvm_fallback,
)
from shared.command_runner.error_parsing import (
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
from shared.command_runner.nvm import (
    NvmInstallResult,
    ensure_nvm_installed,
    run_command_with_nvm,
    run_npm_build_with_nvm,
)
from shared.command_runner.runner import (
    BUILD_TIMEOUT,
    SERVE_TIMEOUT,
    TEST_TIMEOUT,
    CommandResult,
    detect_frontend_framework,
    is_ng_build_environment_failure,
    patch_json_file,
    patch_text_file,
    run_command,
    run_frontend_build,
    run_linter,
    run_ng_build,
    run_pytest,
    run_python_syntax_check,
)
from shared.command_runner.scaffolding import (
    ensure_backend_project_initialized,
    ensure_frontend_project_initialized,
)
from shared.command_runner.smoke_test import (
    run_frontend_serve_smoke_test,
    run_ng_serve_smoke_test,
    run_npm_start_smoke_test,
)

__all__ = [
    # runner — subprocess primitives
    "CommandResult",
    "run_command",
    "patch_json_file",
    "patch_text_file",
    # runner — frontend build/serve
    "detect_frontend_framework",
    "run_frontend_build",
    "run_ng_build",
    "is_ng_build_environment_failure",
    # runner — backend test/lint
    "run_pytest",
    "run_python_syntax_check",
    "run_linter",
    # runner — timeouts
    "BUILD_TIMEOUT",
    "SERVE_TIMEOUT",
    "TEST_TIMEOUT",
    # nvm
    "NvmInstallResult",
    "ensure_nvm_installed",
    "run_command_with_nvm",
    "run_npm_build_with_nvm",
    # angular_repair
    "ensure_frontend_dependencies_installed",
    "run_ng_build_with_nvm_fallback",
    # scaffolding
    "ensure_frontend_project_initialized",
    "ensure_backend_project_initialized",
    # smoke_test
    "run_frontend_serve_smoke_test",
    "run_npm_start_smoke_test",
    "run_ng_serve_smoke_test",
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
