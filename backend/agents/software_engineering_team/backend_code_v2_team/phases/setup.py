"""
Setup phase: ensure repo exists, README, main branch, development branch,
and linting/testing are configured.

Runs as the first phase of the Backend Tech Lead Agent.
Uses shared.git_utils and shared.command_runner.scaffolding (for the minimal
pyproject.toml template), plus lightweight file checks for lint/test config.
"""

from __future__ import annotations

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
    """Verify that a Python linter is configured in the project.

    Checks for ruff.toml, [tool.ruff] in pyproject.toml, .flake8, or [flake8]
    in setup.cfg. If none are found, creates a minimal pyproject.toml with ruff
    configuration so linting never silently skips.

    Side effect: every repo-relative path this call creates or modifies is added
    to ``written`` so the caller can commit exactly what setup touched.
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
        except Exception as e:
            logger.debug("Setup: could not read pyproject.toml for lint config check: %s", e)
    if flake8_cfg.exists():
        logger.info("Setup: linting already configured via .flake8")
        return True
    if setup_cfg.exists():
        try:
            text = setup_cfg.read_text(encoding="utf-8", errors="replace")
            if "[flake8]" in text:
                logger.info("Setup: linting already configured via setup.cfg [flake8]")
                return True
        except Exception as e:
            logger.debug("Setup: could not read setup.cfg for lint config check: %s", e)

    # No linting config found — create minimal ruff config in pyproject.toml
    logger.info("Setup: no linting configuration found; creating pyproject.toml with ruff config")
    from shared.command_runner.scaffolding import _MINIMAL_PYPROJECT_TOML

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
            written.add("pyproject.toml")
    else:
        pyproject.write_text(_MINIMAL_PYPROJECT_TOML, encoding="utf-8")
        written.add("pyproject.toml")
    return True


def _ensure_testing_configured(path: Path, written: set[str]) -> bool:
    """Verify that a Python test framework is configured in the project.

    Checks for both a pytest config (pytest.ini or [tool.pytest] in
    pyproject.toml) AND a tests/ directory — both must be present to count as
    "already configured". Creates whichever is missing so tests never
    silently skip.

    Side effect: every repo-relative path this call creates or modifies is added
    to ``written`` so the caller can commit exactly what setup touched.
    """
    pytest_ini = path / "pytest.ini"
    pyproject = path / "pyproject.toml"
    tests_dir = path / "tests"

    has_pytest_config = pytest_ini.exists()
    if not has_pytest_config and pyproject.exists():
        try:
            text = pyproject.read_text(encoding="utf-8", errors="replace")
            has_pytest_config = "[tool.pytest" in text
        except Exception as e:
            logger.debug("Setup: could not read pyproject.toml for pytest config check: %s", e)

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
            written.add("tests/__init__.py")
        test_file = tests_dir / "test_main.py"
        if not test_file.exists():
            test_file.write_text(
                '"""Minimal test so pytest runs."""\n\ndef test_health():\n    assert True\n',
                encoding="utf-8",
            )
            written.add("tests/test_main.py")

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
                written.add("pyproject.toml")
        else:
            # If pyproject.toml doesn't exist yet (unlikely after linting setup), use pytest.ini
            pytest_ini.write_text(
                "[pytest]\naddopts = -v\ntestpaths = tests\n",
                encoding="utf-8",
            )
            written.add("pytest.ini")

    return True


def configure_quality_tooling(repo_path: Path) -> tuple[bool, bool]:
    """Ensure lint/test scaffolding exists on the CURRENT branch and commit it.

    Delegates to the shared implementation, injecting this team's Python
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
    Ensure the repository is initialized and ready for development.

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
