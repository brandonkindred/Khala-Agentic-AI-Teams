"""Unit tests for the Backend Expert agent."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend_agent.agent import (
    EXCEPTION_HANDLER_TEST_PATTERNS,
    BackendExpertAgent,
    _build_code_review_issues_for_build_failure,
    _build_code_review_issues_for_missing_test_routes,
    _build_error_signature,
    _extract_failing_test_file_from_build_errors,
    _is_pytest_assertion_failure,
    _test_routes_missing_from_main_py,
    _test_routes_referenced_in_tests,
)
from backend_agent.models import BackendInput


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


def test_build_code_review_issues_extracts_failing_test_file_from_feedback() -> None:
    """When build_errors contain 'Fix tests/...' from parsed feedback, file_path is set to that file."""
    build_errors = (
        "[pytest_assertion] test_toggle failed (expected 200, got 401)\n\n"
        "Suggestion:\nFix tests/test_task_endpoints.py (test_toggle_completion_updates_status).\n\n"
        "Failing tests:\n  - tests/test_task_endpoints.py::test_toggle_completion_updates_status"
    )
    issues = _build_code_review_issues_for_build_failure(build_errors)
    assert len(issues) == 1
    assert issues[0]["file_path"] == "tests/test_task_endpoints.py"
    assert "Build/test failed" in issues[0]["description"]


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


def test_extract_failing_test_file_from_build_errors() -> None:
    """_extract_failing_test_file_from_build_errors extracts tests/test_*.py path."""
    assert _extract_failing_test_file_from_build_errors(
        "[pytest_assertion] test_invalid_auth_header failed. Raw output: FAILED tests/test_auth_middleware.py::test_invalid"
    ) == "tests/test_auth_middleware.py"
    assert _extract_failing_test_file_from_build_errors("ImportError: no module") is None


def test_is_pytest_assertion_failure() -> None:
    """_is_pytest_assertion_failure detects pytest assertion failures."""
    assert _is_pytest_assertion_failure("[pytest_assertion] test failed")
    assert _is_pytest_assertion_failure("failure_class=pytest_assertion")
    assert not _is_pytest_assertion_failure("ImportError: cannot import")


def test_build_error_signature_uses_tail_for_assertion_failures() -> None:
    """_build_error_signature uses last 1200 chars for pytest_assertion, first 800 otherwise."""
    assertion_err = "[pytest_assertion] failed\n" + "x" * 1500
    sig = _build_error_signature(assertion_err)
    assert len(sig) == 1200  # last 1200 chars
    assert "x" in sig
    generic_err = "ImportError: foo\n" + "y" * 1000
    sig2 = _build_error_signature(generic_err)
    assert sig2.startswith("ImportError")
    assert len(sig2) <= 800


def test_build_code_review_issues_for_missing_test_routes_returns_targeted_issue() -> None:
    """_build_code_review_issues_for_missing_test_routes returns issue with file_path app/main.py."""
    issues = _build_code_review_issues_for_missing_test_routes()
    assert len(issues) == 1
    assert issues[0]["file_path"] == "app/main.py"
    assert "/test-generic-error" in issues[0]["suggestion"]


def test_backend_agent_includes_problem_solving_header_when_issues_present() -> None:
    """When code_review_issues are present, prompt includes PROBLEM-SOLVING MODE header."""
    mock_llm = MagicMock()
    mock_llm.complete_json.return_value = {
        "code": "",
        "language": "python",
        "summary": "Fixed",
        "files": {"app/main.py": "from fastapi import FastAPI\napp = FastAPI()"},
        "tests": "",
        "suggested_commit_message": "fix: resolve",
    }
    agent = BackendExpertAgent(llm_client=mock_llm)
    agent.run(
        BackendInput(
            task_description="Fix build",
            requirements="",
            code_review_issues=[
                {"severity": "critical", "category": "build", "description": "Test failed", "suggestion": "Fix it", "file_path": "app/main.py"},
            ],
        )
    )
    prompt = mock_llm.complete_json.call_args[0][0]
    assert "PROBLEM-SOLVING MODE" in prompt
    assert "Backend" in prompt
    assert "code review issues" in prompt
    assert "Identify the likely root cause" in prompt
    assert "Test failed" in prompt
    assert "Fix it" in prompt
    assert "error details above" not in prompt


def test_backend_agent_no_problem_solving_header_when_no_issues() -> None:
    """When no issues are present, prompt does not include PROBLEM-SOLVING MODE header."""
    mock_llm = MagicMock()
    mock_llm.complete_json.return_value = {
        "code": "",
        "language": "python",
        "summary": "Implemented",
        "files": {"app/main.py": "from fastapi import FastAPI\napp = FastAPI()"},
        "tests": "",
        "suggested_commit_message": "feat: add",
    }
    agent = BackendExpertAgent(llm_client=mock_llm)
    agent.run(
        BackendInput(
            task_description="Add endpoint",
            requirements="",
        )
    )
    prompt = mock_llm.complete_json.call_args[0][0]
    assert "PROBLEM-SOLVING MODE" not in prompt


def test_backend_agent_logs_llm_prompt(caplog: pytest.LogCaptureFixture) -> None:
    """Backend agent logs LLM call metadata before each LLM call."""
    import logging

    caplog.set_level(logging.INFO)
    mock_llm = MagicMock()
    mock_llm.complete_json.return_value = {
        "code": "",
        "language": "python",
        "summary": "Done",
        "files": {"app/main.py": "content"},
        "tests": "",
        "suggested_commit_message": "feat: add",
    }
    agent = BackendExpertAgent(llm_client=mock_llm)
    agent.run(BackendInput(task_description="Add API", requirements=""))
    assert any("LLM call" in rec.message and "agent=Backend" in rec.message for rec in caplog.records)
    assert any("mode=initial" in rec.message for rec in caplog.records)


def test_backend_agent_logs_problem_solving_context_and_header_when_issues_present(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Backend agent logs problem-solving context and header when issues are present."""
    import logging

    caplog.set_level(logging.INFO)
    mock_llm = MagicMock()
    mock_llm.complete_json.return_value = {
        "code": "",
        "language": "python",
        "summary": "Fixed",
        "files": {"app/main.py": "content"},
        "tests": "",
        "suggested_commit_message": "fix: resolve",
    }
    agent = BackendExpertAgent(llm_client=mock_llm)
    agent.run(
        BackendInput(
            task_description="Fix build",
            requirements="",
            code_review_issues=[
                {
                    "severity": "critical",
                    "category": "build",
                    "description": "Test failed",
                    "suggestion": "Fix it",
                    "file_path": "app/main.py",
                },
            ],
        )
    )
    assert any("Backend problem-solving context" in rec.message for rec in caplog.records)
    assert any("Backend problem-solving header for LLM" in rec.message for rec in caplog.records)
    assert any("mode=problem_solving" in rec.message for rec in caplog.records)


def test_backend_agent_no_problem_solving_logs_when_no_issues(caplog: pytest.LogCaptureFixture) -> None:
    """Backend agent does not log problem-solving context/header when no issues."""

    import logging

    caplog.set_level(logging.INFO)
    mock_llm = MagicMock()
    mock_llm.complete_json.return_value = {
        "code": "",
        "language": "python",
        "summary": "Done",
        "files": {"app/main.py": "content"},
        "tests": "",
        "suggested_commit_message": "feat: add",
    }
    agent = BackendExpertAgent(llm_client=mock_llm)
    agent.run(BackendInput(task_description="Add API", requirements=""))
    assert not any("Backend problem-solving context" in rec.message for rec in caplog.records)
    assert not any("Backend problem-solving header for LLM" in rec.message for rec in caplog.records)


def test_backend_agent_content_fallback_extracts_file_from_code_block() -> None:
    """When LLM returns only content with a markdown code block, backend extracts at least one file."""
    mock_llm = MagicMock()
    mock_llm.complete_json.return_value = {
        "content": "```\napp/main.py\nprint(1)\n```",
    }
    agent = BackendExpertAgent(llm_client=mock_llm)
    result = agent.run(BackendInput(task_description="Add", requirements=""))
    assert len(result.files) >= 1
    assert any("main" in path or "app" in path for path in result.files)


def test_backend_agent_content_fallback_no_code_blocks_does_not_crash() -> None:
    """When LLM returns only content with no code blocks, agent completes without crash (empty_completion path)."""
    mock_llm = MagicMock()
    mock_llm.complete_json.return_value = {"content": "no code blocks here at all"}
    agent = BackendExpertAgent(llm_client=mock_llm)
    result = agent.run(BackendInput(task_description="Add", requirements=""))
    assert result.files == {}
    assert result.summary == "" or result.summary is not None
