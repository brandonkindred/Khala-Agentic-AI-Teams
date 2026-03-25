"""
Setup phase: ensure repo exists, README, main branch, development branch,
and linting/testing are configured.

Runs as the first phase of the Backend Tech Lead Agent.
Uses shared.git_utils only (plus lightweight file checks for lint/test config).
"""

from __future__ import annotations

import logging
from pathlib import Path

from software_engineering_team.shared.git_utils import (
    ensure_development_branch,
    initialize_new_repo,
)

from ..models import SetupResult

logger = logging.getLogger(__name__)


def _ensure_linting_configured(path: Path) -> bool:
    """Verify that a Python linter is configured in the project.

    Checks for ruff.toml, [tool.ruff] in pyproject.toml, .flake8, or [flake8]
    in setup.cfg. If none are found, creates a minimal pyproject.toml with ruff
    configuration so linting never silently skips.
    """
    ruff_toml = path / "ruff.toml"
    pyproject = path / "pyproject.toml"
    flake8_cfg = path / ".flake8"
    setup_cfg = path / "setup.cfg"

    # Check existing config files
    if ruff_toml.exists():
        logger.info("Setup: linting already configured via ruff.toml")
        return True
    if pyproject.exists():
        try:
            text = pyproject.read_text(encoding="utf-8", errors="replace")
            if "[tool.ruff]" in text:
                logger.info("Setup: linting already configured via pyproject.toml [tool.ruff]")
                return True
        except Exception:
            pass
    if flake8_cfg.exists():
        logger.info("Setup: linting already configured via .flake8")
        return True
    if setup_cfg.exists():
        try:
            text = setup_cfg.read_text(encoding="utf-8", errors="replace")
            if "[flake8]" in text:
                logger.info("Setup: linting already configured via setup.cfg [flake8]")
                return True
        except Exception:
            pass

    # No linting config found — create minimal ruff config in pyproject.toml
    logger.info("Setup: no linting configuration found; creating pyproject.toml with ruff config")
    from software_engineering_team.shared.command_runner import _MINIMAL_PYPROJECT_TOML

    if pyproject.exists():
        # Append ruff config to existing pyproject.toml
        existing = pyproject.read_text(encoding="utf-8", errors="replace")
        if "[tool.ruff]" not in existing:
            ruff_section = (
                "\n[tool.ruff]\n"
                'target-version = "py310"\n'
                "line-length = 120\n\n"
                "[tool.ruff.lint]\n"
                'select = ["E", "F", "I", "N", "W", "UP", "B", "SIM"]\n'
                'ignore = ["E501"]\n'
            )
            pyproject.write_text(existing + ruff_section, encoding="utf-8")
    else:
        pyproject.write_text(_MINIMAL_PYPROJECT_TOML, encoding="utf-8")
    return True


def _ensure_testing_configured(path: Path) -> bool:
    """Verify that a Python test framework is configured in the project.

    Checks for pytest.ini, [tool.pytest] in pyproject.toml, or a tests/
    directory. If missing, creates a minimal pytest configuration and test
    directory so tests never silently skip.
    """
    pytest_ini = path / "pytest.ini"
    pyproject = path / "pyproject.toml"
    tests_dir = path / "tests"

    has_pytest_config = pytest_ini.exists()
    if not has_pytest_config and pyproject.exists():
        try:
            text = pyproject.read_text(encoding="utf-8", errors="replace")
            has_pytest_config = "[tool.pytest" in text
        except Exception:
            pass

    if has_pytest_config and tests_dir.exists():
        logger.info("Setup: testing already configured")
        return True

    # Ensure tests directory exists
    if not tests_dir.exists():
        logger.info("Setup: creating tests/ directory")
        tests_dir.mkdir(parents=True, exist_ok=True)
        init_file = tests_dir / "__init__.py"
        if not init_file.exists():
            init_file.write_text("", encoding="utf-8")
        test_file = tests_dir / "test_main.py"
        if not test_file.exists():
            test_file.write_text(
                '"""Minimal test so pytest runs."""\n\ndef test_health():\n    assert True\n',
                encoding="utf-8",
            )

    # Ensure pytest config exists
    if not has_pytest_config:
        logger.info("Setup: no pytest configuration found; adding to pyproject.toml")
        if pyproject.exists():
            existing = pyproject.read_text(encoding="utf-8", errors="replace")
            if "[tool.pytest" not in existing:
                pytest_section = (
                    '\n[tool.pytest.ini_options]\naddopts = "-v"\ntestpaths = ["tests"]\n'
                )
                pyproject.write_text(existing + pytest_section, encoding="utf-8")
        else:
            # If pyproject.toml doesn't exist yet (unlikely after linting setup), use pytest.ini
            pytest_ini.write_text(
                "[pytest]\naddopts = -v\ntestpaths = tests\n",
                encoding="utf-8",
            )

    return True


def run_setup(
    *,
    repo_path: Path,
    task_title: str = "",
) -> SetupResult:
    """
    Ensure the repository is initialized and ready for development.

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
        result.readme_created = True  # initialize_new_repo creates README.md
        # Update README with project title if provided
        if task_title:
            _ensure_readme_with_title(path, task_title)

        # Ensure linting and testing are configured before any coding begins
        result.linting_configured = _ensure_linting_configured(path)
        result.testing_configured = _ensure_testing_configured(path)

        result.summary = f"Initialized repo: {msg}"
        logger.info("Setup: %s", result.summary)
        return result

    # Already a git repo: ensure development branch and README
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
    # Commit if we have git and changes
    try:
        from software_engineering_team.shared.git_utils import write_files_and_commit

        write_files_and_commit(path, {"README.md": content}, "docs: add README with project title")
    except Exception as e:
        logger.warning("Could not commit README: %s", e)
