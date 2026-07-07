"""Unit tests for the Linting Tool Agent."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from linting_tool_agent import LintingToolAgent, LintIssue, LintToolInput, LintToolOutput
from linting_tool_agent.linter_runner import detect_linter, parse_lint_output
from linting_tool_agent.models import LintExecutionResult, LintPlan

from software_engineering_team.tests.conftest import ConfigurableLLM

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
    mock_llm = ConfigurableLLM()
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
    mock_llm.complete_json_mock.assert_not_called()


def test_agent_run_lint_fails_and_produces_edits(tmp_path: Path) -> None:
    """When lint fails, agent calls LLM to produce edits."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("import os\nprint('hello')\n")

    mock_llm = ConfigurableLLM()
    mock_llm.complete_json_mock.return_value = {
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
    mock_llm.complete_json_mock.assert_called_once()


def test_agent_run_llm_failure_is_non_blocking(tmp_path: Path) -> None:
    """When LLM fails, agent returns issues but no edits (non-blocking)."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("import os\n")

    mock_llm = ConfigurableLLM()
    mock_llm.complete_json_mock.side_effect = Exception("LLM unavailable")

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
