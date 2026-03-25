"""
Setup phase: ensure repo exists, README, main branch, development branch,
and linting/testing are configured.

Runs as the first phase of the Frontend Tech Lead Agent.
Uses shared.git_utils only. No frontend_team code.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from software_engineering_team.shared.git_utils import (
    ensure_development_branch,
    initialize_new_repo,
)

from ..models import SetupResult

logger = logging.getLogger(__name__)


def _ensure_linting_configured(path: Path) -> bool:
    """Verify that a frontend linter is configured in the project.

    Checks for eslint config files (eslint.config.*, .eslintrc*) or ng lint
    availability (angular.json). If none are found, creates a minimal ESLint
    flat config so linting never silently skips.
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
        from software_engineering_team.shared.command_runner import _MINIMAL_ANGULAR_ESLINT_CONFIG

        config_file = path / "eslint.config.js"
        if not config_file.exists():
            config_file.write_text(_MINIMAL_ANGULAR_ESLINT_CONFIG, encoding="utf-8")
        return True

    # React/generic project — create ESLint flat config
    logger.info("Setup: no linting configuration found; creating eslint.config.mjs")
    from software_engineering_team.shared.command_runner import _MINIMAL_REACT_ESLINT_CONFIG

    config_file = path / "eslint.config.mjs"
    if not config_file.exists():
        config_file.write_text(_MINIMAL_REACT_ESLINT_CONFIG, encoding="utf-8")

    # Ensure lint script exists in package.json
    _ensure_package_script(path, "lint", "eslint .")
    return True


def _ensure_testing_configured(path: Path) -> bool:
    """Verify that a frontend test framework is configured in the project.

    Checks for vitest/jest config files or test scripts in package.json.
    If missing, creates a minimal vitest configuration so tests never skip.
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
        except Exception:
            pass

    # Create vitest config based on framework
    is_angular = (path / "angular.json").exists()
    if is_angular:
        logger.info("Setup: creating vitest.config.mts for Angular project")
        from software_engineering_team.shared.command_runner import (
            _MINIMAL_ANGULAR_TEST_SETUP,
            _MINIMAL_ANGULAR_VITEST_CONFIG,
        )

        config_file = path / "vitest.config.mts"
        if not config_file.exists():
            config_file.write_text(_MINIMAL_ANGULAR_VITEST_CONFIG, encoding="utf-8")
        # Ensure test setup file
        src = path / "src"
        src.mkdir(parents=True, exist_ok=True)
        setup_file = src / "test-setup.ts"
        if not setup_file.exists():
            setup_file.write_text(_MINIMAL_ANGULAR_TEST_SETUP, encoding="utf-8")
    else:
        logger.info("Setup: creating vitest.config.ts for React project")
        from software_engineering_team.shared.command_runner import _MINIMAL_REACT_VITEST_CONFIG

        config_file = path / "vitest.config.ts"
        if not config_file.exists():
            config_file.write_text(_MINIMAL_REACT_VITEST_CONFIG, encoding="utf-8")

    _ensure_package_script(path, "test", "vitest run")
    _ensure_package_script(path, "test:coverage", "vitest run --coverage")
    return True


def _ensure_package_script(path: Path, script_name: str, script_cmd: str) -> None:
    """Add a script to package.json if it doesn't already exist."""
    pkg_json = path / "package.json"
    if not pkg_json.exists():
        return
    try:
        pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
        scripts = pkg.setdefault("scripts", {})
        if script_name not in scripts or "exit 1" in scripts.get(script_name, ""):
            scripts[script_name] = script_cmd
            pkg_json.write_text(json.dumps(pkg, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("Could not update package.json script %s: %s", script_name, e)


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
    """
    result = SetupResult()
    path = Path(repo_path).resolve()

    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)

    if not (path / ".git").exists():
        ok, msg = initialize_new_repo(path)
        if not ok:
            result.summary = f"Setup failed: {msg}"
            logger.error("Setup: %s", result.summary)
            return result
        result.repo_initialized = True
        result.master_renamed_to_main = True
        result.branch_created = True
        result.readme_created = True
        if task_title:
            _ensure_readme_with_title(path, task_title)

        # Ensure linting and testing are configured before any coding begins
        result.linting_configured = _ensure_linting_configured(path)
        result.testing_configured = _ensure_testing_configured(path)

        result.summary = f"Initialized repo: {msg}"
        logger.info("Setup: %s", result.summary)
        return result

    ok, msg = ensure_development_branch(path)
    if not ok:
        result.summary = f"Setup failed: {msg}"
        logger.error("Setup: %s", result.summary)
        return result
    if "Created branch" in msg:
        result.branch_created = True
    if not (path / "README.md").exists() and task_title:
        _ensure_readme_with_title(path, task_title)
        result.readme_created = True

    # Ensure linting and testing are configured before any coding begins
    result.linting_configured = _ensure_linting_configured(path)
    result.testing_configured = _ensure_testing_configured(path)

    result.summary = msg or "Repo ready; on development branch."
    logger.info(
        "Setup: %s (lint=%s, test=%s)",
        result.summary,
        result.linting_configured,
        result.testing_configured,
    )
    return result


def _ensure_readme_with_title(path: Path, title: str) -> None:
    """Write or prepend project title to README.md and commit if possible."""
    readme = path / "README.md"
    content = f"# {title}\n\n"
    if readme.exists():
        existing = readme.read_text(encoding="utf-8")
        if existing.strip() and not existing.lstrip().startswith("#"):
            content = content + existing
        else:
            content = content + existing.lstrip()
    readme.write_text(content, encoding="utf-8")
    try:
        from software_engineering_team.shared.git_utils import write_files_and_commit

        write_files_and_commit(path, {"README.md": content}, "docs: add README with project title")
    except Exception as e:
        logger.warning("Could not commit README: %s", e)
