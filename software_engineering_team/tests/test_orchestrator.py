"""Unit tests for the orchestrator."""

from pathlib import Path
from unittest.mock import patch

import pytest

from shared.command_runner import CommandResult
import orchestrator


def test_run_build_verification_appends_fix_line_when_pytest_fails_with_test_error_handlers(
    tmp_path: Path,
) -> None:
    """When pytest fails and summary contains test_error_handlers, returned error includes FIX line."""
    # Set up backend dir with Python files and tests so pytest path is taken.
    # backend_dir = tmp_path when repo has .py files; tests_dir = tmp_path / "tests"
    (tmp_path / "main.py").write_text("x = 1", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_foo.py").write_text("def test_foo(): pass", encoding="utf-8")

    failure_summary = (
        "= FAILURES =\n"
        "________________________ test_generic_exception_handler ________________________\n"
        "tests/test_error_handlers.py:108: in test_generic_exception_handler\n"
        "    response = client.get(\"/test-generic-error\")"
    )
    mock_result = CommandResult(
        success=False,
        exit_code=1,
        stdout=failure_summary,
        stderr="",
    )

    with patch(
        "shared.command_runner.run_python_syntax_check",
        return_value=CommandResult(True, 0, "", ""),
    ):
        with patch("shared.command_runner.run_pytest", return_value=mock_result):
            ok, error_output = orchestrator._run_build_verification(
                tmp_path, "backend", "task-1"
            )

    assert ok is False
    assert "FIX: Preserve the /test-generic-error route" in error_output
    assert "JSONResponse" in error_output
    assert "do not re-raise" in error_output
