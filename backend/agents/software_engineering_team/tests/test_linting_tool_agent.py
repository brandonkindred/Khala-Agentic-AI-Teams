"""Unit tests for the Linting Tool Agent."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from linting_tool_agent import LintingToolAgent, LintIssue, LintToolInput, LintToolOutput
from linting_tool_agent.linter_runner import detect_linter, parse_lint_output
from linting_tool_agent.models import LintExecutionResult, LintPlan

# ---------------------------------------------------------------------------
# Model construction and serialization
# ---------------------------------------------------------------------------


def test_lint_issue_construction() -> None:
    issue = LintIssue(
        file_path="app/main.py", line=10, column=1, rule="E501", message="Line too long"
    )
    assert issue.file_path == "app/main.py"
    assert issue.line == 10
    assert issue.severity == "warning"
    d = issue.model_dump()
    assert d["rule"] == "E501"


def test_lint_plan_defaults() -> None:
    plan = LintPlan(linter_name="ruff", linter_command=["ruff", "check", "."])
    assert plan.scope_paths == ["."]
    assert plan.config_file is None


def test_lint_execution_result_success() -> None:
    result = LintExecutionResult(success=True)
    assert result.issues == []
    assert result.issue_count == 0


def test_lint_tool_input_construction() -> None:
    inp = LintToolInput(repo_path="/tmp/repo", agent_type="backend")
    assert inp.task_id == ""
    assert inp.agent_type == "backend"


def test_lint_tool_output_construction() -> None:
    plan = LintPlan(linter_name="ruff", linter_command=["ruff", "check", "."])
    exec_result = LintExecutionResult(success=True)
    out = LintToolOutput(plan=plan, execution_result=exec_result, summary="ok")
    assert out.edits == []
    assert out.linter_issues == []
    d = out.model_dump()
    assert d["plan"]["linter_name"] == "ruff"


# ---------------------------------------------------------------------------
# Linter detection
# ---------------------------------------------------------------------------


def test_detect_linter_defaults_to_ruff(tmp_path: Path) -> None:
    """When no config files exist, default to ruff for backend."""
    with patch("linting_tool_agent.linter_runner._is_command_available", return_value=True):
        plan = detect_linter(tmp_path, "backend")
    assert plan.linter_name == "ruff"
    assert plan.linter_command == ["ruff", "check", "."]
    assert plan.config_file is None


def test_detect_linter_ruff_toml(tmp_path: Path) -> None:
    (tmp_path / "ruff.toml").write_text("[lint]\nselect = ['E']\n")
    with patch("linting_tool_agent.linter_runner._is_command_available", return_value=True):
        plan = detect_linter(tmp_path, "backend")
    assert plan.linter_name == "ruff"
    assert plan.config_file == "ruff.toml"


def test_detect_linter_pyproject_ruff(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 120\n")
    with patch("linting_tool_agent.linter_runner._is_command_available", return_value=True):
        plan = detect_linter(tmp_path, "backend")
    assert plan.linter_name == "ruff"
    assert plan.config_file == "pyproject.toml"


def test_detect_linter_flake8(tmp_path: Path) -> None:
    (tmp_path / ".flake8").write_text("[flake8]\nmax-line-length = 120\n")
    with patch(
        "linting_tool_agent.linter_runner._is_command_available",
        side_effect=lambda cmd: cmd == "flake8",
    ):
        plan = detect_linter(tmp_path, "backend")
    assert plan.linter_name == "flake8"
    assert plan.linter_command == ["flake8", "."]


def test_detect_linter_angular(tmp_path: Path) -> None:
    (tmp_path / "angular.json").write_text("{}")
    plan = detect_linter(tmp_path, "frontend")
    assert plan.linter_name == "ng_lint"
    assert "ng" in plan.linter_command


def test_detect_linter_eslint_config(tmp_path: Path) -> None:
    (tmp_path / ".eslintrc.json").write_text("{}")
    plan = detect_linter(tmp_path, "frontend")
    assert plan.linter_name == "eslint"


def test_detect_linter_frontend_defaults_eslint(tmp_path: Path) -> None:
    plan = detect_linter(tmp_path, "frontend")
    assert plan.linter_name == "eslint"


# ---------------------------------------------------------------------------
# Lint output parsing
# ---------------------------------------------------------------------------


def test_parse_ruff_output() -> None:
    raw = (
        "app/main.py:10:1: E501 Line too long (120 > 88)\n"
        "app/main.py:15:5: F401 `os` imported but unused\n"
        "tests/test_foo.py:3:1: W291 trailing whitespace\n"
    )
    issues = parse_lint_output(raw, "ruff")
    assert len(issues) == 3
    assert issues[0].file_path == "app/main.py"
    assert issues[0].line == 10
    assert issues[0].rule == "E501"
    assert issues[1].severity == "error"  # F-rules are errors
    assert issues[2].severity == "warning"  # W-rules are warnings


def test_parse_flake8_output() -> None:
    raw = "app/models.py:5:1: E302 expected 2 blank lines, found 1\n"
    issues = parse_lint_output(raw, "flake8")
    assert len(issues) == 1
    assert issues[0].rule == "E302"


def test_parse_empty_output_returns_no_issues() -> None:
    issues = parse_lint_output("", "ruff")
    assert issues == []


def test_parse_eslint_output() -> None:
    raw = (
        "/home/user/project/src/app.ts\n"
        "  10:1  error  Unexpected var, use let or const  no-var\n"
        "  20:5  warning  Missing return type  @typescript-eslint/explicit-function-return-type\n"
    )
    issues = parse_lint_output(raw, "eslint")
    assert len(issues) == 2
    assert issues[0].file_path == "/home/user/project/src/app.ts"
    assert issues[0].rule == "no-var"
    assert issues[0].severity == "error"
    assert issues[1].severity == "warning"


# ---------------------------------------------------------------------------
# Agent run (mocked LLM + subprocess)
# ---------------------------------------------------------------------------


def test_agent_run_lint_passes(tmp_path: Path) -> None:
    """When lint passes, agent returns success with no edits."""
    mock_llm = MagicMock()
    agent = LintingToolAgent(mock_llm)

    with (
        patch("linting_tool_agent.linter_runner._is_command_available", return_value=True),
        patch("linting_tool_agent.linter_runner.run_command") as mock_cmd,
    ):
        mock_cmd.return_value = MagicMock(success=True, output="", stdout="", stderr="")
        result = agent.run(LintToolInput(repo_path=str(tmp_path), agent_type="backend"))

    assert result.execution_result.success is True
    assert result.edits == []
    assert "passed" in result.summary.lower()
    mock_llm.complete_json.assert_not_called()


def test_agent_run_lint_fails_and_produces_edits(tmp_path: Path) -> None:
    """When lint fails, agent calls LLM to produce edits."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("import os\nprint('hello')\n")

    mock_llm = MagicMock()
    mock_llm.complete_json.return_value = {
        "edits": [
            {
                "file_path": "app/main.py",
                "old_text": "import os\n",
                "new_text": "",
            }
        ],
        "summary": "Removed unused import",
    }

    agent = LintingToolAgent(mock_llm)

    lint_output = "app/main.py:1:1: F401 `os` imported but unused\n"
    with (
        patch("linting_tool_agent.linter_runner._is_command_available", return_value=True),
        patch("linting_tool_agent.linter_runner.run_command") as mock_cmd,
    ):
        mock_cmd.return_value = MagicMock(
            success=False, output=lint_output, stdout=lint_output, stderr="", exit_code=1
        )
        result = agent.run(LintToolInput(repo_path=str(tmp_path), agent_type="backend"))

    assert result.execution_result.success is False
    assert len(result.edits) == 1
    assert result.edits[0].file_path == "app/main.py"
    assert len(result.linter_issues) == 1
    mock_llm.complete_json.assert_called_once()


def test_agent_run_llm_failure_is_non_blocking(tmp_path: Path) -> None:
    """When LLM fails, agent returns issues but no edits (non-blocking)."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("import os\n")

    mock_llm = MagicMock()
    mock_llm.complete_json.side_effect = Exception("LLM unavailable")

    agent = LintingToolAgent(mock_llm)

    lint_output = "app/main.py:1:1: F401 `os` imported but unused\n"
    with (
        patch("linting_tool_agent.linter_runner._is_command_available", return_value=True),
        patch("linting_tool_agent.linter_runner.run_command") as mock_cmd,
    ):
        mock_cmd.return_value = MagicMock(
            success=False, output=lint_output, stdout=lint_output, stderr="", exit_code=1
        )
        result = agent.run(LintToolInput(repo_path=str(tmp_path), agent_type="backend"))

    assert result.execution_result.success is False
    assert result.edits == []
    assert len(result.linter_issues) == 1


# ---------------------------------------------------------------------------
# Backend workflow integration
# ---------------------------------------------------------------------------


def test_backend_workflow_calls_linting_tool_agent(tmp_path: Path) -> None:
    """When linting_tool_agent is provided, run_workflow invokes it."""
    import subprocess

    from backend_agent import BackendExpertAgent

    # Set up a minimal git repo
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=tmp_path, check=True, capture_output=True
    )
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "checkout", "-b", "development"], cwd=tmp_path, check=True, capture_output=True
    )

    mock_llm = MagicMock()
    mock_llm.get_max_context_tokens.return_value = 16384
    mock_llm.complete_json.side_effect = [
        # Planning step
        {
            "feature_intent": "Test",
            "what_changes": ["app/main.py"],
            "algorithms_data_structures": "",
            "tests_needed": "",
        },
        # Code generation step
        {
            "code": "",
            "language": "python",
            "summary": "test",
            "files": {"app/main.py": "print('hello')\n"},
            "tests": "",
            "suggested_commit_message": "feat: test",
        },
    ] + [
        # Additional calls for fix loops
        {
            "code": "",
            "language": "python",
            "summary": "fix",
            "files": {"app/main.py": "print('hello')\n"},
            "tests": "",
            "suggested_commit_message": "fix: test",
        }
        for _ in range(10)
    ]

    agent = BackendExpertAgent(mock_llm)

    mock_lint_agent = MagicMock()
    lint_plan = LintPlan(linter_name="ruff", linter_command=["ruff", "check", "."])
    lint_exec = LintExecutionResult(success=True)
    mock_lint_agent.run.return_value = LintToolOutput(
        plan=lint_plan, execution_result=lint_exec, summary="Lint passed"
    )

    from qa_agent.models import QAOutput

    mock_qa = MagicMock()
    mock_qa.run.return_value = QAOutput(
        bugs_found=[],
        approved=True,
        readme_content="",
        unit_tests="",
        integration_tests="",
    )

    mock_security = MagicMock()
    mock_security.run.return_value = MagicMock(vulnerabilities=[], approved=True, issues=[])
    mock_dbc = MagicMock()
    mock_dbc.run.return_value = MagicMock(
        already_compliant=True, comments_added=0, comments_updated=0, updated_code=""
    )
    mock_code_review = MagicMock()
    mock_code_review.run.return_value = MagicMock(approved=True, issues=[])
    mock_tech_lead = MagicMock()
    mock_tech_lead.review_progress.return_value = []

    def build_verifier(_repo_path, _agent_type, _task_id):
        return True, ""

    from software_engineering_team.shared.models import Task, TaskType

    task = Task(
        id="test-lint-1",
        title="Test lint integration",
        description="A test task",
        type=TaskType.BACKEND,
        assignee="backend",
        requirements="test with input validation",
        acceptance_criteria=["passes lint"],
        metadata={
            "goal": "Test lint integration",
            "scope": "unit test scope",
            "constraints": "none",
            "non_functional_requirements": "none",
            "inputs_outputs": "none",
        },
    )

    agent.run_workflow(
        repo_path=tmp_path,
        task=task,
        spec_content="test spec",
        architecture=None,
        qa_agent=mock_qa,
        security_agent=mock_security,
        dbc_agent=mock_dbc,
        code_review_agent=mock_code_review,
        tech_lead=mock_tech_lead,
        build_verifier=build_verifier,
        linting_tool_agent=mock_lint_agent,
    )

    mock_lint_agent.run.assert_called()
    lint_call_args = mock_lint_agent.run.call_args[0][0]
    assert lint_call_args.agent_type == "backend"
    assert lint_call_args.task_id == "test-lint-1"
