"""Unit tests for the shared build-specialist runners and base.

These exercise ``software_engineering_team.shared.tool_agent_build_specialist``
directly so the moved build-runner logic is covered independently of the thin
per-stack profiles. The real runners spawn build subprocesses; here we
monkeypatch the ``command_runner`` entry points (imported inside the runners at
call time) and feed crafted :class:`CommandResult` objects.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from shared.command_runner import executor as cr
from shared.command_runner.executor import CommandResult
from software_engineering_team.shared.tool_agent_build_specialist import (
    BuildSpecialistToolAgentBase,
    run_backend_build_and_parse,
    run_frontend_build_and_parse,
)

# ---------------------------------------------------------------------------
# Backend runner
# ---------------------------------------------------------------------------


def test_backend_no_python_project_returns_empty(tmp_path: Path):
    assert run_backend_build_and_parse(tmp_path) == []


def test_backend_syntax_errors_parsed_per_file(tmp_path: Path, monkeypatch):
    (tmp_path / "a.py").write_text("x = (\n")
    monkeypatch.setattr(
        cr,
        "run_python_syntax_check",
        lambda p: CommandResult(
            success=False,
            exit_code=1,
            stdout="",
            # blank + colon-less lines are skipped; only the real "path: msg" line maps
            stderr="Syntax errors found:\n\ngarbage-no-colon\na.py: invalid syntax",
        ),
    )
    issues = run_backend_build_and_parse(tmp_path)
    assert len(issues) == 1
    assert issues[0].file_path == "a.py"
    assert issues[0].severity == "critical"
    assert "Fix the syntax error" in issues[0].recommendation


def test_backend_syntax_error_preserves_windows_path_and_colon_message(tmp_path: Path, monkeypatch):
    """A Windows drive-letter path and a colon-bearing message must survive parsing.

    The producer joins ``"<path>: <message>"`` with a colon-space delimiter. Splitting
    on the first bare colon would truncate a Windows path (``C:\\...``) to its drive
    letter; splitting on the last colon would fold part of the message into the path.
    The colon-space split preserves both.
    """
    (tmp_path / "a.py").write_text("x = (\n")
    monkeypatch.setattr(
        cr,
        "run_python_syntax_check",
        lambda p: CommandResult(
            success=False,
            exit_code=1,
            stdout="",
            stderr=("Syntax errors found:\nC:\\proj\\a.py: SyntaxError: invalid syntax"),
        ),
    )
    issues = run_backend_build_and_parse(tmp_path)
    assert len(issues) == 1
    assert issues[0].file_path == "C:\\proj\\a.py"
    assert issues[0].description == "SyntaxError: invalid syntax"


def test_backend_syntax_error_generic_when_no_marker(tmp_path: Path, monkeypatch):
    (tmp_path / "a.py").write_text("print('ok')\n")
    monkeypatch.setattr(
        cr,
        "run_python_syntax_check",
        lambda p: CommandResult(success=False, exit_code=1, stdout="", stderr="boom"),
    )
    issues = run_backend_build_and_parse(tmp_path)
    assert len(issues) == 1
    assert "boom" in issues[0].description


def test_backend_pytest_failure_reports_issue(tmp_path: Path, monkeypatch):
    (tmp_path / "a.py").write_text("print('ok')\n")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text("def test_a():\n    assert False\n")
    monkeypatch.setattr(
        cr,
        "run_python_syntax_check",
        lambda p: CommandResult(success=True, exit_code=0, stdout="ok", stderr=""),
    )
    monkeypatch.setattr(
        cr,
        "run_pytest",
        lambda p, python_exe=None: CommandResult(
            success=False,
            exit_code=1,
            stdout="= FAILURES =\nFAILED tests/test_x.py::test_a - assert False",
            stderr="",
        ),
    )
    issues = run_backend_build_and_parse(tmp_path)
    assert issues
    assert all(i.source == "build_specialist" for i in issues)


def test_backend_pytest_failure_maps_parsed_failures(tmp_path: Path, monkeypatch):
    """When pytest output parses into structured failures, each becomes one issue."""
    from shared.command_runner import error_parsing

    (tmp_path / "a.py").write_text("print('ok')\n")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text("def test_a():\n    assert False\n")
    monkeypatch.setattr(
        cr,
        "run_python_syntax_check",
        lambda p: CommandResult(success=True, exit_code=0, stdout="ok", stderr=""),
    )
    monkeypatch.setattr(
        cr,
        "run_pytest",
        lambda p, python_exe=None: CommandResult(
            success=False, exit_code=1, stdout="boom", stderr=""
        ),
    )
    failure = SimpleNamespace(
        message="assertion failed",
        raw_excerpt="",
        file_path="tests/test_x.py",
        suggestion="check the assert",
        playbook_hint="",
    )
    monkeypatch.setattr(error_parsing, "parse_command_failure", lambda *a, **kw: [failure])
    issues = run_backend_build_and_parse(tmp_path)
    assert len(issues) == 1
    assert issues[0].file_path == "tests/test_x.py"
    assert issues[0].recommendation == "check the assert"


def _passing_syntax(monkeypatch):
    monkeypatch.setattr(
        cr,
        "run_python_syntax_check",
        lambda p: CommandResult(success=True, exit_code=0, stdout="ok", stderr=""),
    )


def test_backend_installs_requirements_before_pytest(tmp_path: Path, monkeypatch):
    """A requirements.txt triggers a (best-effort) pip install before pytest runs."""
    (tmp_path / "a.py").write_text("print('ok')\n")
    (tmp_path / "requirements.txt").write_text("pytest\n")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text("def test_a():\n    assert True\n")
    _passing_syntax(monkeypatch)
    calls = []
    monkeypatch.setattr(cr, "run_command", lambda *a, **kw: calls.append(a) or None)
    monkeypatch.setattr(
        cr,
        "run_pytest",
        lambda p, python_exe=None: CommandResult(success=True, exit_code=0, stdout="ok", stderr=""),
    )
    assert run_backend_build_and_parse(tmp_path) == []
    assert calls  # pip install was attempted


def test_backend_pip_install_failure_is_non_fatal(tmp_path: Path, monkeypatch):
    """A failing pip install is swallowed; pytest still runs and reports failures."""
    (tmp_path / "a.py").write_text("print('ok')\n")
    (tmp_path / "requirements.txt").write_text("pytest\n")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text("def test_a():\n    assert False\n")
    _passing_syntax(monkeypatch)

    def _boom(*a, **kw):
        raise RuntimeError("pip exploded")

    monkeypatch.setattr(cr, "run_command", _boom)
    monkeypatch.setattr(
        cr,
        "run_pytest",
        lambda p, python_exe=None: CommandResult(
            success=False, exit_code=1, stdout="boom", stderr=""
        ),
    )
    from shared.command_runner import error_parsing

    monkeypatch.setattr(error_parsing, "parse_command_failure", lambda *a, **kw: [])
    issues = run_backend_build_and_parse(tmp_path)
    assert issues  # generic pytest failure issue still produced
    assert issues[0].source == "build_specialist"


def test_backend_pytest_success_returns_empty(tmp_path: Path, monkeypatch):
    (tmp_path / "a.py").write_text("print('ok')\n")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text("def test_a():\n    assert True\n")
    monkeypatch.setattr(
        cr,
        "run_python_syntax_check",
        lambda p: CommandResult(success=True, exit_code=0, stdout="ok", stderr=""),
    )
    monkeypatch.setattr(
        cr,
        "run_pytest",
        lambda p, python_exe=None: CommandResult(success=True, exit_code=0, stdout="ok", stderr=""),
    )
    assert run_backend_build_and_parse(tmp_path) == []


# ---------------------------------------------------------------------------
# Frontend runner
# ---------------------------------------------------------------------------


def test_frontend_no_project_returns_empty(tmp_path: Path):
    assert run_frontend_build_and_parse(tmp_path) == []


def test_frontend_build_failure_generic_issue(tmp_path: Path, monkeypatch):
    from shared.command_runner import error_parsing

    (tmp_path / "package.json").write_text('{"name": "x"}\n')
    monkeypatch.setattr(cr, "detect_frontend_framework", lambda p: "react")
    monkeypatch.setattr(
        cr,
        "run_frontend_build",
        lambda p: CommandResult(success=False, exit_code=1, stdout="", stderr="build error"),
    )
    monkeypatch.setattr(error_parsing, "parse_command_failure", lambda *a, **kw: [])
    issues = run_frontend_build_and_parse(tmp_path)
    assert len(issues) == 1
    assert issues[0].source == "build_specialist"
    assert "build error" in issues[0].description


def test_frontend_build_failure_maps_parsed_failures(tmp_path: Path, monkeypatch):
    """When the build output parses into structured failures, each becomes one issue."""
    from shared.command_runner import error_parsing

    (tmp_path / "package.json").write_text('{"name": "x"}\n')
    monkeypatch.setattr(cr, "detect_frontend_framework", lambda p: "angular")
    monkeypatch.setattr(
        cr,
        "run_frontend_build",
        lambda p: CommandResult(success=False, exit_code=1, stdout="err", stderr=""),
    )
    failure = SimpleNamespace(
        message="TS2304: cannot find name",
        raw_excerpt="",
        file_path="src/app.ts",
        suggestion="",
        playbook_hint="import the symbol",
    )
    monkeypatch.setattr(error_parsing, "parse_command_failure", lambda *a, **kw: [failure])
    issues = run_frontend_build_and_parse(tmp_path)
    assert len(issues) == 1
    assert issues[0].file_path == "src/app.ts"
    assert issues[0].recommendation == "import the symbol"


def _capture_parse_kind(tmp_path: Path, monkeypatch, framework: str) -> str:
    """Run the frontend runner for ``framework`` and return the parse kind it used."""
    from shared.command_runner import error_parsing

    (tmp_path / "package.json").write_text('{"name": "x"}\n')
    monkeypatch.setattr(cr, "detect_frontend_framework", lambda p: framework)
    monkeypatch.setattr(
        cr,
        "run_frontend_build",
        lambda p: CommandResult(success=False, exit_code=1, stdout="", stderr="build error"),
    )
    captured = {}

    def _capture(command_kind, stdout, stderr):
        captured["kind"] = command_kind
        return []

    monkeypatch.setattr(error_parsing, "parse_command_failure", _capture)
    run_frontend_build_and_parse(tmp_path)
    return captured["kind"]


def test_frontend_angular_uses_ng_build_parser(tmp_path: Path, monkeypatch):
    assert _capture_parse_kind(tmp_path, monkeypatch, "angular") == "ng_build"


def test_frontend_react_uses_generic_parser_not_ng_build(tmp_path: Path, monkeypatch):
    """React/Vue must NOT be parsed with the Angular parser (regression guard)."""
    kind = _capture_parse_kind(tmp_path, monkeypatch, "react")
    assert kind == "generic"
    assert kind != "ng_build"


def test_frontend_build_success_returns_empty(tmp_path: Path, monkeypatch):
    (tmp_path / "package.json").write_text('{"name": "x"}\n')
    monkeypatch.setattr(cr, "detect_frontend_framework", lambda p: "react")
    monkeypatch.setattr(
        cr,
        "run_frontend_build",
        lambda p: CommandResult(success=True, exit_code=0, stdout="ok", stderr=""),
    )
    assert run_frontend_build_and_parse(tmp_path) == []


# ---------------------------------------------------------------------------
# Shared base review dispatch
# ---------------------------------------------------------------------------


class _StubBuildAgent(BuildSpecialistToolAgentBase):
    build_review_noun = "build issue(s)"


def _agent(runner):
    a = _StubBuildAgent.__new__(_StubBuildAgent)
    a._model = None
    a.llm = None
    a.build_runner = runner  # instance attr: plain callable, no descriptor binding
    return a


def test_base_review_no_repo_path():
    a = _agent(lambda path: [])
    out = a.review(SimpleNamespace(repo_path=""))
    assert "no repo_path" in out.summary


def test_base_review_missing_repo_path(tmp_path: Path):
    a = _agent(lambda path: [])
    out = a.review(SimpleNamespace(repo_path=str(tmp_path / "missing")))
    assert "repo path missing" in out.summary


def test_base_review_runs_build_runner(tmp_path: Path):
    from software_engineering_team.shared.v2_models import ReviewIssue

    issue = ReviewIssue(source="build_specialist", severity="critical", description="d")
    a = _agent(lambda path: [issue])
    out = a.review(SimpleNamespace(repo_path=str(tmp_path)))
    assert out.issues == [issue]
    assert "1 build issue(s) found" in out.summary
