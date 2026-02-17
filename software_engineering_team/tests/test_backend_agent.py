"""Unit tests for the Backend Expert agent."""

from pathlib import Path

import pytest

from backend_agent.agent import (
    EXCEPTION_HANDLER_TEST_PATTERNS,
    _build_code_review_issues_for_build_failure,
    _build_code_review_issues_for_missing_test_routes,
    _test_routes_missing_from_main_py,
    _test_routes_referenced_in_tests,
)


def test_build_code_review_issues_exception_handler_failure_returns_targeted_suggestion() -> None:
    """When build_errors contain test_generic_exception_handler, returns issue with file_path and specific suggestion."""
    build_errors = (
        "= FAILURES =\n"
        "________________________ test_generic_exception_handler ________________________\n"
        "tests/test_error_handlers.py:108: in test_generic_exception_handler\n"
        "    response = client.get(\"/test-generic-error\")"
    )
    issues = _build_code_review_issues_for_build_failure(build_errors)
    assert len(issues) == 1
    assert issues[0]["file_path"] == "app/main.py"
    assert "/test-generic-error" in issues[0]["suggestion"]
    assert "JSONResponse" in issues[0]["suggestion"]
    assert "re-raise" in issues[0]["suggestion"]


def test_build_code_review_issues_exception_handler_failure_matches_test_error_handlers() -> None:
    """When build_errors contain test_error_handlers, returns targeted suggestion."""
    build_errors = "FAILED tests/test_error_handlers.py::test_something"
    issues = _build_code_review_issues_for_build_failure(build_errors)
    assert issues[0]["file_path"] == "app/main.py"
    assert "/test-generic-error" in issues[0]["suggestion"]


def test_build_code_review_issues_generic_failure_returns_generic_suggestion() -> None:
    """When build_errors do not match exception-handler patterns, returns generic suggestion."""
    build_errors = "ImportError: No module named 'foo'"
    issues = _build_code_review_issues_for_build_failure(build_errors)
    assert len(issues) == 1
    assert issues[0]["file_path"] == ""
    assert issues[0]["suggestion"] == "Fix the compilation/test errors"
    assert "ImportError" in issues[0]["description"]


def test_test_routes_referenced_in_tests_finds_test_generic_error(tmp_path: Path) -> None:
    """_test_routes_referenced_in_tests finds /test-generic-error when tests reference it."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_error_handlers.py").write_text(
        'response = client.get("/test-generic-error")',
        encoding="utf-8",
    )
    found = _test_routes_referenced_in_tests(tmp_path)
    assert "/test-generic-error" in found


def test_test_routes_referenced_in_tests_returns_empty_when_no_tests(tmp_path: Path) -> None:
    """_test_routes_referenced_in_tests returns empty when no tests dir."""
    assert _test_routes_referenced_in_tests(tmp_path) == []


def test_test_routes_missing_from_main_py_returns_missing_when_route_absent(
    tmp_path: Path,
) -> None:
    """_test_routes_missing_from_main_py returns /test-generic-error when tests reference it but main.py does not."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_foo.py").write_text('client.get("/test-generic-error")', encoding="utf-8")
    files = {"app/main.py": "from fastapi import FastAPI\napp = FastAPI()\n# no test route"}
    missing = _test_routes_missing_from_main_py(tmp_path, files)
    assert "/test-generic-error" in missing


def test_test_routes_missing_from_main_py_returns_empty_when_route_present(
    tmp_path: Path,
) -> None:
    """_test_routes_missing_from_main_py returns empty when main.py includes the route."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_foo.py").write_text('client.get("/test-generic-error")', encoding="utf-8")
    files = {"app/main.py": '@app.get("/test-generic-error")\ndef test_route(): raise Exception()'}
    missing = _test_routes_missing_from_main_py(tmp_path, files)
    assert missing == []


def test_build_code_review_issues_for_missing_test_routes_returns_targeted_issue() -> None:
    """_build_code_review_issues_for_missing_test_routes returns issue with file_path app/main.py."""
    issues = _build_code_review_issues_for_missing_test_routes()
    assert len(issues) == 1
    assert issues[0]["file_path"] == "app/main.py"
    assert "/test-generic-error" in issues[0]["suggestion"]
