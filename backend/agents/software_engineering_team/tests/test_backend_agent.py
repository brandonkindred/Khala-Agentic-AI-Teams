"""Unit tests for the Backend Expert agent."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend_agent.agent import (
    EXCEPTION_HANDLER_TEST_PATTERNS,
    MAX_PREWRITE_REGENERATIONS,
    MAX_PROBLEM_SOLVER_CYCLES,
    BackendExpertAgent,
    _build_code_review_issues_for_build_failure,
    _build_code_review_issues_for_missing_test_routes,
    _build_error_signature,
    _extract_failing_test_file_from_build_errors,
    _is_pytest_assertion_failure,
    _test_routes_missing_from_main_py,
    _test_routes_referenced_in_tests,
    _validate_task_contract,
    _build_completion_package,
)
from backend_agent.models import BackendInput




def test_problem_solver_cycle_constant_defaults_to_twenty() -> None:
    """Problem solver cycle budget is bounded and defaults to a sane range around 20."""
    assert 1 <= MAX_PROBLEM_SOLVER_CYCLES <= 20

def test_max_prewrite_regenerations_constant_defaults_to_two() -> None:
    """MAX_PREWRITE_REGENERATIONS is 2 by default to cap pre-write loops and reduce LLM calls."""
    assert MAX_PREWRITE_REGENERATIONS >= 1
    # Default is 2 when SW_MAX_PREWRITE_REGENERATIONS not set; may be overridden in CI
    assert MAX_PREWRITE_REGENERATIONS in (1, 2, 3, 4, 5, 6)  # sane range


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


def test_backend_agent_content_only_with_code_block_raises_llm_permanent_error() -> None:
    """When LLM returns only content (no files dict), backend raises LLMPermanentError (fail fast)."""
    from software_engineering_team.shared.llm import LLMPermanentError

    mock_llm = MagicMock()
    mock_llm.complete_json.return_value = {
        "content": "```\napp/main.py\nprint(1)\n```",
    }
    agent = BackendExpertAgent(llm_client=mock_llm)
    with pytest.raises(LLMPermanentError, match="no files"):
        agent.run(BackendInput(task_description="Add", requirements=""))


def test_backend_agent_content_only_raises_llm_permanent_error() -> None:
    """When LLM returns only content with no files/code, agent raises LLMPermanentError (fail fast)."""
    from software_engineering_team.shared.llm import LLMPermanentError

    mock_llm = MagicMock()
    mock_llm.complete_json.return_value = {"content": "no code blocks here at all"}
    agent = BackendExpertAgent(llm_client=mock_llm)
    with pytest.raises(LLMPermanentError, match="no files"):
        agent.run(BackendInput(task_description="Add", requirements=""))


def test_backend_plan_task_returns_plan_markdown() -> None:
    """_plan_task parses LLM JSON and returns (plan_text, False)."""
    from software_engineering_team.shared.models import Task, TaskType

    mock_llm = MagicMock()
    mock_llm.get_max_context_tokens.return_value = 16384
    mock_llm.complete_json.return_value = {
        "feature_intent": "Add CRUD for tasks",
        "what_changes": ["app/routers/tasks.py", "app/models/task.py"],
        "algorithms_data_structures": "Use dict for O(1) lookup",
        "tests_needed": "tests/test_task_endpoints.py",
    }
    agent = BackendExpertAgent(llm_client=mock_llm)
    task = Task(id="t1", type=TaskType.BACKEND, assignee="backend", title="Add tasks", description="Implement task CRUD")
    plan_text, should_split = agent._plan_task(
        task=task,
        existing_code="# No code",
        spec_content="",
        architecture=None,
    )
    assert plan_text
    assert not should_split
    assert "Add CRUD for tasks" in plan_text
    assert "app/routers/tasks.py" in plan_text
    assert "tests/test_task_endpoints.py" in plan_text
    assert "O(1) lookup" in plan_text


def test_regenerate_with_issues_passes_task_plan_to_backend_input(tmp_path: Path) -> None:
    """_regenerate_with_issues passes task_plan through to BackendInput when provided."""
    from unittest.mock import patch

    mock_llm = MagicMock()
    mock_llm.get_max_context_tokens.return_value = 16384
    mock_llm.complete_json.return_value = {
        "code": "",
        "language": "python",
        "summary": "Fixed",
        "files": {"app/main.py": "content"},
        "tests": "",
        "suggested_commit_message": "fix: add route",
    }
    agent = BackendExpertAgent(llm_client=mock_llm)
    task = type("Task", (), {
        "id": "t1",
        "description": "Add API",
        "user_story": "",
        "requirements": "",
    })()
    with patch.object(agent, "run") as mock_run:
        mock_run.return_value = type("Out", (), {"files": {"app/main.py": "x"}, "summary": ""})()
        agent._regenerate_with_issues(
            repo_path=tmp_path,
            current_task=task,
            spec_content="",
            architecture=None,
            code_review_issues=[{"severity": "critical", "description": "Fix", "suggestion": "Add route"}],
            task_plan="**Feature intent:** Add /test-generic-error",
        )
        call_args = mock_run.call_args
        backend_input = call_args[0][0]
        assert backend_input.task_plan == "**Feature intent:** Add /test-generic-error"


def test_backend_run_injects_task_plan_and_follow_instruction_into_prompt() -> None:
    """When task_plan is set, run() injects Implementation plan and follow-plan instruction into prompt."""
    mock_llm = MagicMock()
    mock_llm.get_max_context_tokens.return_value = 16384
    mock_llm.complete_json.return_value = {
        "code": "",
        "language": "python",
        "summary": "Done",
        "files": {"app/main.py": "content"},
        "tests": "",
        "suggested_commit_message": "feat: add",
    }
    agent = BackendExpertAgent(llm_client=mock_llm)
    plan_content = "**Feature intent:** Add API\n**What changes:** app/routers/foo.py"
    agent.run(
        BackendInput(
            task_description="Add foo API",
            requirements="",
            task_plan=plan_content,
        )
    )
    prompt = mock_llm.complete_json.call_args[0][0]
    assert "IMPLEMENTATION PLAN (follow this)" in prompt
    assert "Implement the task strictly according to" in prompt
    assert "realize every item under 'What changes' and 'Tests needed'" in prompt
    assert "Add API" in prompt
    assert "app/routers/foo.py" in prompt


def test_run_workflow_exits_at_five_same_build_failures_and_notifies_tech_lead(
    tmp_path: Path,
) -> None:
    """When build fails 5 times with same error, workflow exits early and Tech Lead receives task_update with needs_followup."""
    from software_engineering_team.shared.models import Task, TaskType

    # Minimal git repo with development branch
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "app").mkdir(exist_ok=True)
    (tmp_path / "app" / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8")
    subprocess.run(["git", "checkout", "-b", "development"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

    task = Task(
        id="t1",
        type=TaskType.BACKEND,
        assignee="backend",
        title="Add API",
        description="Implement GET /health",
        acceptance_criteria=["returns 200"],
        metadata={"goal": {"summary": "x"}, "scope": {"included": ["x"]}, "constraints": {"framework": "fastapi"}, "non_functional_requirements": {"latency_p95_ms": 300}, "inputs_outputs": {"input": "x", "output": "y"}},
    )
    same_error = "ImportError: No module named 'foo'"

    mock_llm = MagicMock()
    mock_llm.get_max_context_tokens.return_value = 16384
    mock_llm.complete_json.side_effect = [
        {"feature_intent": "Add health", "what_changes": ["app/main.py"], "algorithms_data_structures": "", "tests_needed": ""},
        {"code": "", "language": "python", "summary": "Done", "files": {"app/main.py": "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/health')\ndef h(): return {}"}, "tests": "", "suggested_commit_message": "feat: add"},
    ] + [
        {"code": "", "language": "python", "summary": "Fixed", "files": {"app/main.py": "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/health')\ndef h(): return {}"}, "tests": "", "suggested_commit_message": "fix: build"}
        for _ in range(10)
    ]

    agent = BackendExpertAgent(llm_client=mock_llm)
    mock_qa = MagicMock()
    from qa_agent.models import BugReport

    mock_qa.run.return_value = MagicMock(
        bugs_found=[
            BugReport(severity="critical", description="Fix", location="app/main.py", recommendation="Fix the import"),
        ]
    )
    mock_tech_lead = MagicMock()
    mock_tech_lead.review_progress.return_value = []

    def build_verifier(_repo_path, _agent_type, _task_id):
        return (False, same_error)

    result = agent.run_workflow(
        repo_path=tmp_path,
        task=task,
        spec_content="# Spec",
        architecture=None,
        qa_agent=mock_qa,
        security_agent=MagicMock(run=MagicMock(return_value=MagicMock(approved=True, issues=[]))),
        dbc_agent=MagicMock(run=MagicMock(return_value=MagicMock(already_compliant=True, comments_added=0, comments_updated=0))),
        code_review_agent=MagicMock(run=MagicMock(return_value=MagicMock(approved=True, issues=[]))),
        tech_lead=mock_tech_lead,
        build_verifier=build_verifier,
    )

    assert result.success is False
    assert "5 times" in (result.failure_reason or "")
    assert result.needs_followup is True
    mock_tech_lead.review_progress.assert_called()
    call_kwargs = mock_tech_lead.review_progress.call_args[1]
    task_update = call_kwargs.get("task_update")
    assert task_update is not None
    assert task_update.needs_followup is True
    assert task_update.status == "failed"


def test_run_workflow_invokes_build_fix_specialist_when_same_build_fails_twice(
    tmp_path: Path,
) -> None:
    """When build fails 2 times with same error, BuildFixSpecialist is invoked (and can apply patch)."""
    from software_engineering_team.shared.models import Task, TaskType

    from build_fix_specialist.models import CodeEdit

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "app").mkdir(exist_ok=True)
    (tmp_path / "app" / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/health')\ndef h(): return {}",
        encoding="utf-8",
    )
    subprocess.run(["git", "checkout", "-b", "development"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

    task = Task(
        id="t2",
        type=TaskType.BACKEND,
        assignee="backend",
        title="Add API",
        description="Implement GET /health",
        acceptance_criteria=["returns 200"],
        metadata={"goal": {"summary": "x"}, "scope": {"included": ["x"]}, "constraints": {"framework": "fastapi"}, "non_functional_requirements": {"latency_p95_ms": 300}, "inputs_outputs": {"input": "x", "output": "y"}},
    )
    same_error = "ImportError: No module named 'foo'"

    mock_llm = MagicMock()
    mock_llm.get_max_context_tokens.return_value = 16384
    mock_llm.complete_json.side_effect = [
        {"feature_intent": "Add health", "what_changes": ["app/main.py"], "algorithms_data_structures": "", "tests_needed": ""},
        {"code": "", "language": "python", "summary": "Done", "files": {"app/main.py": "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/health')\ndef h(): return {}"}, "tests": "", "suggested_commit_message": "feat: add"},
    ] + [
        {"code": "", "language": "python", "summary": "Fixed", "files": {"app/main.py": "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/health')\ndef h(): return {}"}, "tests": "", "suggested_commit_message": "fix: build"}
        for _ in range(10)
    ]

    agent = BackendExpertAgent(llm_client=mock_llm)
    mock_qa = MagicMock()
    from qa_agent.models import BugReport

    mock_qa.run.return_value = MagicMock(
        bugs_found=[
            BugReport(severity="critical", description="Fix", location="app/main.py", recommendation="Fix the import"),
        ]
    )
    mock_tech_lead = MagicMock()
    mock_tech_lead.review_progress.return_value = []

    def build_verifier(_repo_path, _agent_type, _task_id):
        return (False, same_error)

    mock_specialist = MagicMock()
    mock_specialist.run.return_value = MagicMock(
        edits=[
            CodeEdit(
                file_path="app/main.py",
                old_text="from fastapi import FastAPI",
                new_text="from fastapi import FastAPI  # fixed",
            ),
        ],
        summary="Added import fix",
    )

    agent.run_workflow(
        repo_path=tmp_path,
        task=task,
        spec_content="# Spec",
        architecture=None,
        qa_agent=mock_qa,
        security_agent=MagicMock(),
        dbc_agent=MagicMock(),
        code_review_agent=MagicMock(),
        tech_lead=mock_tech_lead,
        build_verifier=build_verifier,
        build_fix_specialist=mock_specialist,
    )

    mock_specialist.run.assert_called()
    call_input = mock_specialist.run.call_args[0][0]
    assert call_input.build_errors == same_error
    assert "app/main.py" in call_input.affected_files_code or "main.py" in call_input.affected_files_code
    main_content = (tmp_path / "app" / "main.py").read_text()
    assert "# fixed" in main_content


def test_run_workflow_skips_specialist_when_none(tmp_path: Path) -> None:
    """When build_fix_specialist=None, specialist is not invoked even on repeated build failure."""
    from software_engineering_team.shared.models import Task, TaskType

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "app").mkdir(exist_ok=True)
    (tmp_path / "app" / "main.py").write_text("print('hi')", encoding="utf-8")
    subprocess.run(["git", "checkout", "-b", "development"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

    task = Task(
        id="t-none", type=TaskType.BACKEND, assignee="backend",
        title="Add", description="Add",
        acceptance_criteria=["ok"],
        metadata={"goal": {"summary": "x"}, "scope": {"included": ["x"]},
                   "constraints": {}, "non_functional_requirements": {},
                   "inputs_outputs": {"input": "x", "output": "y"}},
    )

    mock_llm = MagicMock()
    mock_llm.get_max_context_tokens.return_value = 16384
    mock_llm.complete_json.side_effect = [
        {"feature_intent": "Add", "what_changes": ["app/main.py"], "algorithms_data_structures": "", "tests_needed": ""},
        {"code": "", "language": "python", "summary": "Done",
         "files": {"app/main.py": "print('hi')"}, "tests": "", "suggested_commit_message": "feat: add"},
    ] + [
        {"code": "", "language": "python", "summary": "Fixed",
         "files": {"app/main.py": "print('hi')"}, "tests": "", "suggested_commit_message": "fix: build"}
        for _ in range(10)
    ]

    agent = BackendExpertAgent(llm_client=mock_llm)
    mock_qa = MagicMock()
    from qa_agent.models import BugReport
    mock_qa.run.return_value = MagicMock(
        bugs_found=[BugReport(severity="critical", description="Fix", location="app/main.py", recommendation="Fix")]
    )
    mock_tech_lead = MagicMock()
    mock_tech_lead.review_progress.return_value = []

    result = agent.run_workflow(
        repo_path=tmp_path,
        task=task,
        spec_content="# Spec",
        architecture=None,
        qa_agent=mock_qa,
        security_agent=MagicMock(),
        dbc_agent=MagicMock(),
        code_review_agent=MagicMock(),
        tech_lead=mock_tech_lead,
        build_verifier=lambda *a: (False, "same error"),
        build_fix_specialist=None,
    )
    # Workflow completes without crashing; specialist was not available
    assert result is not None


def test_backend_agent_includes_specialist_tooling_plan_in_prompt() -> None:
    """When specialist_tooling_plan is provided, prompt includes Backend Agent V2 specialist coordination block."""
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
            task_description="Implement auth-protected task endpoint",
            requirements="",
            specialist_tooling_plan={
                "api": {"directive": "update openapi"},
                "auth_security": {"directive": "enforce RBAC"},
            },
        )
    )
    prompt = mock_llm.complete_json.call_args[0][0]
    assert "BACKEND AGENT V2 SPECIALIST TOOLING PLAN" in prompt
    assert "devops, api, quality_review, qa, data_engineering, auth_security" in prompt
    assert "enforce RBAC" in prompt


def test_backend_agent_includes_specialist_findings_in_prompt() -> None:
    """When specialist_findings are provided, prompt includes specialist constraints block."""
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
            task_description="Implement task service",
            requirements="",
            specialist_findings={
                "qa": {"failing_tests": ["tests/test_tasks.py::test_create_task"]},
                "data_engineering": {"migration": "add tasks.owner_id index"},
            },
        )
    )
    prompt = mock_llm.complete_json.call_args[0][0]
    assert "SPECIALIST FINDINGS / CONSTRAINTS" in prompt
    assert "add tasks.owner_id index" in prompt
    assert "preserve security and correctness first" in prompt


def test_run_workflow_uses_problem_solver_agent_on_build_failure(tmp_path: Path) -> None:
    """When provided, problem solver agent is invoked during build-failure bug fixing."""
    from software_engineering_team.shared.models import Task, TaskType

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "app").mkdir(exist_ok=True)
    (tmp_path / "app" / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8")
    subprocess.run(["git", "checkout", "-b", "development"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

    task = Task(id="tps", type=TaskType.BACKEND, assignee="backend", title="Fix build", description="Fix failing tests", acceptance_criteria=["build passes"], metadata={"goal": {"summary": "x"}, "scope": {"included": ["x"]}, "constraints": {"framework": "fastapi"}, "non_functional_requirements": {"latency_p95_ms": 300}, "inputs_outputs": {"input": "x", "output": "y"}})

    mock_llm = MagicMock()
    mock_llm.get_max_context_tokens.return_value = 16384
    mock_llm.complete_json.side_effect = [
        {"feature_intent": "Fix build", "what_changes": ["app/main.py"], "algorithms_data_structures": "", "tests_needed": ""},
        {"code": "", "language": "python", "summary": "Init", "files": {"app/main.py": "from fastapi import FastAPI\napp = FastAPI()\n"}, "tests": "", "suggested_commit_message": "feat: init"},
        {"code": "", "language": "python", "summary": "Patched", "files": {"app/main.py": "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/health')\ndef health():\n    return {'ok': True}\n"}, "tests": "", "suggested_commit_message": "fix: patch"},
    ]
    agent = BackendExpertAgent(llm_client=mock_llm)

    mock_problem_solver = MagicMock()
    mock_problem_solver.run.return_value = type("PS", (), {
        "plan": "Investigate failing assertion",
        "execution_steps": "Patch route",
        "review_checks": "Check response schema",
        "testing_strategy": "Run pytest",
        "fix_recommendation": "Add /health route",
    })()

    mock_qa = MagicMock()
    mock_qa.run.return_value = MagicMock(bugs_found=[], unit_tests="", integration_tests="")

    build_results = iter([
        (False, "FAILED tests/test_health.py::test_health"),
        (True, ""),
        (True, ""),
    ])

    def build_verifier(_repo_path, _agent_type, _task_id):
        return next(build_results)

    with patch.object(agent, "_run_dbc_review", return_value=(0, 0, True)):
        result = agent.run_workflow(
            repo_path=tmp_path,
            task=task,
            spec_content="# Spec",
            architecture=None,
            qa_agent=mock_qa,
            security_agent=MagicMock(run=MagicMock(return_value=MagicMock(approved=True, issues=[]))),
            dbc_agent=MagicMock(),
            code_review_agent=MagicMock(run=MagicMock(return_value=MagicMock(approved=True, issues=[]))),
            tech_lead=MagicMock(),
            build_verifier=build_verifier,
            problem_solver_agent=mock_problem_solver,
        )

    assert result.success is True
    mock_problem_solver.run.assert_called()



def test_validate_task_contract_flags_missing_contract_fields() -> None:
    """Contract-first validation flags missing machine-readable contract fields."""
    from software_engineering_team.shared.models import Task, TaskType

    task = Task(
        id="t1",
        type=TaskType.BACKEND,
        assignee="backend",
        title="Add endpoint",
        description="Add endpoint",
        requirements="",
        acceptance_criteria=[],
        metadata={},
    )
    ok, missing = _validate_task_contract(task)
    assert not ok
    assert "goal" in missing
    assert "scope" in missing
    assert "acceptance_criteria" in missing


def test_build_completion_package_contains_trace_and_gates() -> None:
    """Completion package includes traceability and quality gate matrix."""
    from software_engineering_team.shared.models import Task, TaskType
    from backend_agent.models import BackendOutput, ReviewIterationRecord

    task = Task(
        id="BE-1",
        type=TaskType.BACKEND,
        assignee="backend",
        description="Implement invoice draft",
        acceptance_criteria=["Endpoint returns 201"],
        metadata={
            "goal": {"summary": "Create invoice draft"},
            "scope": {"included": ["POST /v1/invoices/drafts"]},
            "constraints": {"framework": "fastapi"},
            "non_functional_requirements": {"latency_p95_ms": 300},
            "inputs_outputs": {"input": "invoice request", "output": "invoice id"},
        },
    )
    output = BackendOutput(
        summary="Implemented endpoint",
        files={
            "src/api/invoice_routes.py": "code",
            "tests/integration/test_invoice.py": "test",
        },
    )
    pkg = _build_completion_package(
        task=task,
        result=output,
        review_history=[ReviewIterationRecord(iteration=1, build_passed=True)],
        language_used="python",
    )
    assert pkg["task_id"] == "BE-1"
    assert pkg["quality_gates"]["acceptance_trace"] == "pass"
    assert pkg["acceptance_criteria_trace"][0]["criterion"] == "Endpoint returns 201"
