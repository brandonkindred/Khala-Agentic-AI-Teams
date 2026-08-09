"""
Setup phase: ensure repo exists, README, main branch, development branch,
and linting/testing are configured.

Runs as the first phase of the Frontend Tech Lead Agent.
Uses shared.git_utils and shared.command_runner.scaffolding (for ESLint/Vitest
config templates). No frontend_team code.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from shared.git.git_utils import commit_paths
from software_engineering_team.shared.phases.setup import (
    configure_quality_tooling_impl,
    run_setup_impl,
)

from ..models import SetupResult

logger = logging.getLogger(__name__)


def _ensure_linting_configured(path: Path, written: set[str]) -> bool:
    """Verify that a frontend linter is configured in the project.

    Checks for eslint config files (eslint.config.*, .eslintrc*) or ng lint
    availability (angular.json). If none are found, creates a minimal ESLint
    flat config so linting never silently skips.

    Side effect: every repo-relative path this call creates or modifies is added
    to ``written`` so the caller can commit exactly what setup touched.
    """
    # Check for existing eslint config
    eslint_patterns = ("eslint.config.*", ".eslintrc*", ".eslintrc.json", ".eslintrc.js")
    for pattern in eslint_patterns:
        if list(path.glob(pattern)):
            logger.info("Setup: linting already configured via %s", pattern)
            return True

    # Angular projects can use ng lint
    if (path / "angular.json").exists():
        logger.info("Setup: Angular project detected; creating eslint.config.js for ng lint")
        from shared.command_runner.scaffolding import MINIMAL_ANGULAR_ESLINT_CONFIG

        config_file = path / "eslint.config.js"
        if not config_file.exists():
            config_file.write_text(MINIMAL_ANGULAR_ESLINT_CONFIG, encoding="utf-8")
            written.add("eslint.config.js")
        return True

    # React/generic project — create ESLint flat config
    logger.info("Setup: no linting configuration found; creating eslint.config.mjs")
    from shared.command_runner.scaffolding import MINIMAL_REACT_ESLINT_CONFIG

    config_file = path / "eslint.config.mjs"
    if not config_file.exists():
        config_file.write_text(MINIMAL_REACT_ESLINT_CONFIG, encoding="utf-8")
        written.add("eslint.config.mjs")

    # Ensure lint script exists in package.json
    if _ensure_package_script(path, "lint", "eslint ."):
        written.add("package.json")
    return True


def _ensure_testing_configured(path: Path, written: set[str]) -> bool:
    """Verify that a frontend test framework is configured in the project.

    Checks for vitest/jest config files or test scripts in package.json.
    If missing, creates a minimal vitest configuration so tests never skip.

    Side effect: every repo-relative path this call creates or modifies is added
    to ``written`` so the caller can commit exactly what setup touched.
    """
    # Check for existing test config
    test_configs = (
        "vitest.config.*",
        "jest.config.*",
        "karma.conf.js",
    )
    for pattern in test_configs:
        if list(path.glob(pattern)):
            logger.info("Setup: testing already configured via %s", pattern)
            return True

    # Check if package.json has a meaningful test script
    pkg_json = path / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
            test_script = pkg.get("scripts", {}).get("test", "")
            if test_script and "no test" not in test_script and "exit 1" not in test_script:
                logger.info("Setup: testing already configured via package.json test script")
                return True
        except Exception as e:
            logger.debug("Setup: could not parse package.json for test script check: %s", e)

    # Create vitest config based on framework
    is_angular = (path / "angular.json").exists()
    if is_angular:
        logger.info("Setup: creating vitest.config.mts for Angular project")
        from shared.command_runner.scaffolding import (
            MINIMAL_ANGULAR_TEST_SETUP,
            MINIMAL_ANGULAR_VITEST_CONFIG,
        )

        config_file = path / "vitest.config.mts"
        if not config_file.exists():
            config_file.write_text(MINIMAL_ANGULAR_VITEST_CONFIG, encoding="utf-8")
            written.add("vitest.config.mts")
        # Ensure test setup file
        src = path / "src"
        src.mkdir(parents=True, exist_ok=True)
        setup_file = src / "test-setup.ts"
        if not setup_file.exists():
            setup_file.write_text(MINIMAL_ANGULAR_TEST_SETUP, encoding="utf-8")
            written.add("src/test-setup.ts")
    else:
        logger.info("Setup: creating vitest.config.ts for React project")
        from shared.command_runner.scaffolding import MINIMAL_REACT_VITEST_CONFIG

        config_file = path / "vitest.config.ts"
        if not config_file.exists():
            config_file.write_text(MINIMAL_REACT_VITEST_CONFIG, encoding="utf-8")
            written.add("vitest.config.ts")

    if _ensure_package_script(path, "test", "vitest run"):
        written.add("package.json")
    if _ensure_package_script(path, "test:coverage", "vitest run --coverage"):
        written.add("package.json")
    return True


def _ensure_package_script(path: Path, script_name: str, script_cmd: str) -> bool:
    """Add a script to package.json if it doesn't already exist, or overwrite it
    if the existing script is a placeholder that just fails (contains "exit 1").

    Returns:
        True when package.json was modified, False otherwise (missing file,
        a real script already present, or a read/parse error).
    """
    pkg_json = path / "package.json"
    if not pkg_json.exists():
        return False
    try:
        pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
        scripts = pkg.setdefault("scripts", {})
        if script_name not in scripts or "exit 1" in scripts.get(script_name, ""):
            scripts[script_name] = script_cmd
            pkg_json.write_text(json.dumps(pkg, indent=2), encoding="utf-8")
            return True
    except Exception as e:
        logger.warning("Could not update package.json script %s: %s", script_name, e)
    return False


def configure_quality_tooling(repo_path: Path) -> tuple[bool, bool]:
    """Ensure lint/test scaffolding exists on the CURRENT branch and commit it.

    Delegates to the shared implementation, injecting this team's frontend
    lint/test hooks and the module-level ``commit_paths`` (kept importable here
    as the monkeypatch boundary for the scaffolding-commit tests).

    Preconditions:
        - ``repo_path`` is a git repository checked out on the branch to configure.
    Postconditions:
        - Linting and testing are configured on the current branch and any newly
          written scaffolding is committed to it. Returns ``(lint_ok, test_ok)``.
    """
    return configure_quality_tooling_impl(
        repo_path,
        ensure_linting=_ensure_linting_configured,
        ensure_testing=_ensure_testing_configured,
        commit_paths=commit_paths,
    )


def run_setup(
    *,
    repo_path: Path,
    task_title: str = "",
) -> SetupResult:
    """
    Ensure the repository is initialized and ready for frontend development.

    - If the path is not a git repo: git init, create README.md with project title,
      initial commit, rename master to main if needed, create development branch.
    - If already a repo: ensure development branch exists and is checked out;
      optionally ensure README exists (create minimal one if missing).
    - Always verifies linting and testing are configured before returning.

    Preconditions:
        ``repo_path`` is a filesystem path (created if missing).
    Postconditions:
        Returns a ``SetupResult``; delegates to the shared implementation with
        this team's ``configure_quality_tooling``.
    """
    return run_setup_impl(
        repo_path=repo_path,
        task_title=task_title,
        configure_quality_tooling=configure_quality_tooling,
    )
