"""Tests for shared.error_parsing."""

from software_engineering_team.shared.error_parsing import (
    FailureClass,
    ParsedFailure,
    build_agent_feedback,
    get_failure_class_tag,
    parse_devops_failure,
    parse_ng_build_failure,
    parse_pytest_failure,
)


def test_parse_pytest_import_error():
    stdout = """
= ERRORS ====================================
____________________ ERROR collecting tests/test_models.py _____________________
ImportError while importing test module 'tests/test_models.py'.
tests/test_models.py:7: in <module>
    from app.database import Base
E   ImportError: cannot import name 'Base' from 'app.database'
"""
    failures = parse_pytest_failure(stdout, "")
    assert len(failures) == 1
    assert failures[0].failure_class == FailureClass.IMPORT_ERROR
    assert "Base" in failures[0].message
    assert "app.database" in failures[0].message


def test_parse_pytest_assertion_extracts_test_and_assertion():
    stdout = """
= FAILURES ====================================
FAILED tests/test_auth_middleware.py::test_invalid_auth_header - AssertionError: assert 200 == 401
    def test_invalid_auth_header():
        response = client.get("/api/tasks")
>       assert response.status_code == 401
E       AssertionError: assert 200 == 401
E         +200
E         -401
"""
    failures = parse_pytest_failure(stdout, "")
    assert len(failures) == 1
    assert failures[0].failure_class == FailureClass.PYTEST_ASSERTION
    assert "test_invalid_auth_header" in failures[0].message
    assert "200" in failures[0].message or "401" in failures[0].message
    assert failures[0].file_path == "tests/test_auth_middleware.py"
    assert "test_auth_middleware" in (failures[0].suggestion or "")


def test_parse_pytest_sql_no_such_table():
    stdout = """
ERROR root:auth.py:92 Unexpected error: (sqlite3.OperationalError) no such table: api_tokens
[SQL: SELECT api_tokens.id FROM api_tokens]
"""
    failures = parse_pytest_failure(stdout, "")
    assert len(failures) == 1
    assert failures[0].failure_class == FailureClass.SQL_NO_SUCH_TABLE
    assert "api_tokens" in failures[0].message


def test_parse_ng_build_unresolved():
    stderr = """
Could not resolve "./components/task-form/task-form.component"
    src/app/app.routes.ts:10:53:
      10 | loadComponent: () => import('./components/task-form/task-form.component')
"""
    failures = parse_ng_build_failure("", stderr)
    assert len(failures) == 1
    assert failures[0].failure_class == FailureClass.FRONTEND_UNRESOLVED_IMPORT
    assert "task-form" in failures[0].message


def test_build_agent_feedback_frontend_unresolved_includes_path_fix():
    """build_agent_feedback includes Path fix hint when failure is FRONTEND_UNRESOLVED_IMPORT."""
    failures = [
        ParsedFailure(
            failure_class=FailureClass.FRONTEND_UNRESOLVED_IMPORT,
            message="Could not resolve './components/create-task/create-task.component'",
            file_path="src/app/app.routes.ts",
            line=10,
            suggestion="Create the missing file or fix the import path.",
            playbook_hint="Create the missing component file or fix the import path.",
            raw_excerpt="Could not resolve...",
        )
    ]
    feedback = build_agent_feedback(failures, max_chars=500)
    assert "[frontend_unresolved_import]" in feedback
    assert "Suggestion:" in feedback
    assert "Playbook:" in feedback
    assert "Path fix:" in feedback
    assert "task-form" in feedback or "create-task" in feedback
    assert "verb" in feedback or "allowed name" in feedback


def test_build_agent_feedback():
    failures = [
        ParsedFailure(
            failure_class=FailureClass.IMPORT_ERROR,
            message="cannot import name 'Base' from 'app.database'",
            suggestion="Ensure app.database exports Base.",
            playbook_hint="Add the missing export.",
            raw_excerpt="ImportError: cannot import name 'Base'",
        )
    ]
    feedback = build_agent_feedback(failures, max_chars=500)
    assert "[import_error]" in feedback
    assert "Suggestion:" in feedback
    assert "Playbook:" in feedback
    assert "Base" in feedback


def test_get_failure_class_tag():
    assert get_failure_class_tag(FailureClass.IMPORT_ERROR) == "failure_class=import_error"
    assert get_failure_class_tag(FailureClass.SQL_NO_SUCH_TABLE) == "failure_class=sql_no_such_table"


def test_parse_pytest_assertion_401_includes_playbook():
    """When assertion shows got 401, playbook_hint includes 401 and auth/test client guidance."""
    stdout = """
= FAILURES ====================================
FAILED tests/test_task_endpoints.py::test_toggle_completion_updates_status
    tests/test_task_endpoints.py:265: in test_toggle_completion_updates_status
        response = client.patch("/api/tasks/1/complete")
>       assert response.status_code == 200
E       AssertionError: assert 401 == 200
E         +200
E         -401
"""
    failures = parse_pytest_failure(stdout, "")
    assert len(failures) == 1
    assert failures[0].failure_class == FailureClass.PYTEST_ASSERTION
    assert "401" in failures[0].playbook_hint
    assert "auth" in failures[0].playbook_hint.lower()
    assert "test client" in failures[0].playbook_hint.lower() or "test " in failures[0].playbook_hint


def test_build_agent_feedback_includes_interpretation_for_401():
    """build_agent_feedback includes Interpretation section when 401 playbook is present."""
    from software_engineering_team.shared.error_parsing import PLAYBOOK_401_UNAUTHORIZED

    failures = [
        ParsedFailure(
            failure_class=FailureClass.PYTEST_ASSERTION,
            message="test_toggle failed (expected 200, got 401)",
            file_path="tests/test_task_endpoints.py",
            suggestion="Fix tests/test_task_endpoints.py (test_toggle_completion_updates_status).",
            playbook_hint="Fix the failing assertion. " + PLAYBOOK_401_UNAUTHORIZED,
            raw_excerpt="assert 401 == 200",
        )
    ]
    feedback = build_agent_feedback(failures, max_chars=500)
    assert "Interpretation:" in feedback
    assert "401" in feedback
    assert "auth" in feedback.lower()


def test_parse_pytest_multiple_failed_lines_lists_all_and_prefers_traceback_file():
    """Multiple FAILED lines produce one ParsedFailure listing all; file_path prefers traceback file."""
    stdout = """
= FAILURES ====================================
FAILED tests/test_auth_middleware.py::test_something
FAILED tests/test_task_endpoints.py::test_toggle_completion_updates_status
FAILED tests/test_task_endpoints.py::test_delete_task_removes_only_tenant_owned_tasks
    tests/test_task_endpoints.py:277: in test_delete_task_removes_only_tenant_owned_tasks
        response = client.delete("/api/tasks/999")
>       assert response.status_code == 404
E       AssertionError: assert 401 == 404
E         +404
E         -401
"""
    failures = parse_pytest_failure(stdout, "")
    assert len(failures) == 1
    assert failures[0].failure_class == FailureClass.PYTEST_ASSERTION
    # Traceback file in excerpt is test_task_endpoints.py:277, so file_path should be that file
    assert failures[0].file_path == "tests/test_task_endpoints.py"
    # All failing tests listed in message or suggestion
    assert "test_task_endpoints" in (failures[0].message or "")
    assert "test_auth_middleware" in (failures[0].message or "") or "test_auth_middleware" in (failures[0].suggestion or "")
    assert failures[0].failing_tests is not None
    assert len(failures[0].failing_tests) >= 2
    assert any("test_task_endpoints" in ft for ft in failures[0].failing_tests)
    assert any("test_auth_middleware" in ft for ft in failures[0].failing_tests)


def test_build_agent_feedback_includes_failing_tests_section():
    """build_agent_feedback includes Failing tests: section when failing_tests is present."""
    failures = [
        ParsedFailure(
            failure_class=FailureClass.PYTEST_ASSERTION,
            message="2 tests failed",
            file_path="tests/test_task_endpoints.py",
            suggestion="Fix the following failing tests.",
            playbook_hint="Fix the assertion.",
            failing_tests=[
                "tests/test_task_endpoints.py::test_toggle_completion",
                "tests/test_task_endpoints.py::test_delete_task",
            ],
        )
    ]
    feedback = build_agent_feedback(failures, max_chars=500)
    assert "Failing tests:" in feedback
    assert "tests/test_task_endpoints.py::test_toggle_completion" in feedback
    assert "tests/test_task_endpoints.py::test_delete_task" in feedback


def test_parse_pytest_single_failed_line_backward_compatible():
    """Single FAILED line still produces one ParsedFailure with same shape as before."""
    stdout = """
= FAILURES ====================================
FAILED tests/test_auth_middleware.py::test_invalid_auth_header - AssertionError: assert 200 == 401
    def test_invalid_auth_header():
        response = client.get("/api/tasks")
>       assert response.status_code == 401
E       AssertionError: assert 200 == 401
E         +200
E         -401
"""
    failures = parse_pytest_failure(stdout, "")
    assert len(failures) == 1
    assert failures[0].file_path == "tests/test_auth_middleware.py"
    assert "test_invalid_auth_header" in failures[0].message
    assert failures[0].failing_tests is not None
    assert len(failures[0].failing_tests) == 1
    assert "test_auth_middleware" in failures[0].failing_tests[0]


def test_parse_devops_docker_copy_failed():
    """parse_devops_failure detects COPY failed errors."""
    err = "COPY failed: requirements.txt: No such file or directory"
    failures = parse_devops_failure(err)
    assert len(failures) == 1
    assert failures[0].failure_class == FailureClass.DOCKER_BUILD_ERROR
    assert "COPY failed" in failures[0].message
    assert "requirements.txt" in failures[0].suggestion


def test_parse_devops_yaml_error():
    """parse_devops_failure detects YAML parse errors."""
    err = "YAML parse error in .github/workflows/ci.yml: mapping values are not allowed here"
    failures = parse_devops_failure(err)
    assert len(failures) == 1
    assert failures[0].failure_class == FailureClass.YAML_PARSE_ERROR
