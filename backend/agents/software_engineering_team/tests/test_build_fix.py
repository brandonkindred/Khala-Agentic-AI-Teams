"""Tests for ``_run_build_verification`` aliases and the extracted build-fix helpers.

Covers the v2 ``agent_type`` aliases that must not skip syntax check / pytest /
ng build, plus unit tests for ``_collect_project_files``,
``_execute_llm_repair_attempt``, and ``_run_post_fix_build_verification``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from shared.command_runner.executor import CommandResult
from software_engineering_team.build_fix import (
    _collect_project_files,
    _execute_llm_repair_attempt,
    _is_command_result,
    _run_build_verification,
    _run_post_fix_build_verification,
    _try_build_fix_one_at_a_time,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_backend_code_v2_catches_syntax_error(tmp_path: Path) -> None:
    """A syntactically broken .py file must fail verification under the v2 alias."""
    _write(
        tmp_path / "app" / "broken.py",
        "def foo(:\n    pass\n",  # unparsable
    )

    success, error_output = _run_build_verification(tmp_path, "backend_code_v2", "task-1")

    assert success is False
    assert error_output


def test_frontend_code_v2_alias_normalizes_agent_type(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No package.json under a frontend_code_v2 repo must not silently pass unrelated checks.

    With no frontend project present, verification should short-circuit to
    success (nothing to build) via the same branch as plain "frontend" --
    confirming the alias is routed into the frontend branch rather than the
    unconditional fallthrough at the end of the function. Asserting on the
    frontend-branch-only log line (not just the return value) proves the
    frontend branch was actually entered: an unconditional ``(True, "")``
    fallthrough would produce the same return value without logging it.
    """
    with caplog.at_level(logging.INFO):
        success, error_output = _run_build_verification(tmp_path, "frontend_code_v2", "task-2")

    assert success is True
    assert error_output == ""
    assert any("no frontend project found" in record.message for record in caplog.records), (
        "frontend branch was not actually entered"
    )


def test_backend_code_v2_passes_on_valid_python(tmp_path: Path) -> None:
    _write(tmp_path / "app" / "main.py", "def foo():\n    return 1\n")

    success, error_output = _run_build_verification(tmp_path, "backend_code_v2", "task-3")

    assert success is True
    assert error_output == ""


def test_collect_project_files_reads_backend_python_and_skips_excluded_dirs(tmp_path: Path) -> None:
    """Backend collection includes ``.py`` sources and never descends into ``__pycache__``."""
    _write(tmp_path / "app" / "main.py", "def foo():\n    return 1\n")
    _write(tmp_path / "__pycache__" / "main.cpython-311.py", "should_not_be_collected")
    _write(tmp_path / "app" / "notes.txt", "ignored")

    files = _collect_project_files(tmp_path, "backend")

    assert files == {"app/main.py": "def foo():\n    return 1\n"}


def test_collect_project_files_caps_total_chars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Collection stops once the briefing budget is exceeded."""
    monkeypatch.setattr(
        "software_engineering_team.build_fix._BUILD_FIX_MAX_CODE_CHARS",
        20,
    )
    _write(tmp_path / "a.py", "x" * 30)
    _write(tmp_path / "b.py", "y" * 30)

    files = _collect_project_files(tmp_path, "backend")

    assert len(files) == 1


def test_collect_project_files_skips_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = tmp_path / "ok.py"
    bad = tmp_path / "bad.py"
    _write(good, "ok")
    _write(bad, "nope")

    original_read = Path.read_text

    def _maybe_fail(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == "bad.py":
            raise OSError("permission denied")
        return original_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _maybe_fail)

    files = _collect_project_files(tmp_path, "backend")

    assert files == {"ok.py": "ok"}


def test_run_post_fix_build_verification_frontend_delegates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sentinel = object()
    called: list[Path] = []

    def _fake_ng(project_dir: Path) -> object:
        called.append(project_dir)
        return sentinel

    monkeypatch.setattr(
        "shared.command_runner.angular_repair.run_ng_build_with_nvm_fallback",
        _fake_ng,
    )

    result = _run_post_fix_build_verification(tmp_path, "frontend")

    assert result is sentinel
    assert called == [tmp_path]


def test_run_post_fix_build_verification_backend_runs_pytest_when_tests_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write(tmp_path / "app.py", "x = 1\n")
    _write(tmp_path / "tests" / "test_app.py", "def test_ok():\n    assert True\n")

    syntax_ok = type("R", (), {"success": True})()
    pytest_result = type("R", (), {"success": False, "error_summary": "fail"})()
    calls: list[str] = []

    monkeypatch.setattr(
        "shared.command_runner.executor.run_python_syntax_check",
        lambda project_dir: calls.append("syntax") or syntax_ok,
    )
    monkeypatch.setattr(
        "shared.command_runner.executor.run_pytest",
        lambda project_dir, python_exe=None: calls.append("pytest") or pytest_result,
    )

    result = _run_post_fix_build_verification(tmp_path, "backend")

    assert result is pytest_result
    assert calls == ["syntax", "pytest"]


def test_execute_llm_repair_attempt_writes_parsed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current_files = {"app.py": "old"}
    issue = {
        "description": "broken",
        "file_path": "app.py",
        "recommendation": "Fix it.",
    }

    class _FakeAgent:
        def __init__(self, model: object) -> None:
            self.model = model

        def __call__(self, prompt: str) -> str:
            assert "broken" in prompt
            return "TEMPLATE"

    monkeypatch.setattr("software_engineering_team.build_fix.Agent", _FakeAgent)

    applied = _execute_llm_repair_attempt(
        issue=issue,
        current_files=current_files,
        project_dir=tmp_path,
        model=object(),
        parse_fn=lambda raw: {"files": {"app.py": "new"}},
        fix_prompt="{source} {severity} {description} {file_path} {recommendation} {current_code}",
        is_frontend=True,
        language_conventions="",
        task_id="t1",
        attempt=0,
        max_attempts=3,
    )

    assert applied is True
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "new"
    assert current_files["app.py"] == "new"


def test_execute_llm_repair_attempt_returns_false_on_llm_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Boom:
        def __init__(self, model: object) -> None:
            pass

        def __call__(self, prompt: str) -> str:
            raise RuntimeError("llm down")

    monkeypatch.setattr("software_engineering_team.build_fix.Agent", _Boom)

    applied = _execute_llm_repair_attempt(
        issue={"description": "x", "file_path": "", "recommendation": "Fix."},
        current_files={},
        project_dir=tmp_path,
        model=object(),
        parse_fn=lambda raw: {"files": {"app.py": "new"}},
        fix_prompt="{source} {severity} {description} {file_path} {recommendation} {current_code}",
        is_frontend=True,
        language_conventions="",
        task_id="t1",
        attempt=0,
        max_attempts=3,
    )

    assert applied is False
    assert not (tmp_path / "app.py").exists()


def test_try_build_fix_survives_pytest_runner_exception(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pytest-runner crash during issue collection must not abort the build-fix loop.

    Preconditions:
        ``tmp_path`` contains at least one ``.py`` file and a ``tests/test_*.py`` file
        so the backend branch invokes ``run_pytest``.
    Postconditions:
        ``_try_build_fix_one_at_a_time`` returns ``(False, summary)`` instead of raising
        when ``run_pytest`` raises.
    """
    _write(tmp_path / "app.py", "def foo():\n    return 1\n")
    _write(tmp_path / "tests" / "test_app.py", "def test_foo():\n    assert True\n")

    syntax_ok = CommandResult(success=True, exit_code=0, stdout="", stderr="")
    monkeypatch.setattr(
        "shared.command_runner.executor.run_python_syntax_check",
        lambda *args, **kwargs: syntax_ok,
    )
    monkeypatch.setattr(
        "shared.command_runner.executor.run_pytest",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("pytest env exploded")),
    )

    with caplog.at_level(logging.WARNING):
        success, error_output = _try_build_fix_one_at_a_time(
            tmp_path, "backend", "task-pytest-crash"
        )

    assert success is False
    assert "pytest env exploded" in error_output
    assert any("pytest failed to run" in record.message for record in caplog.records)


def test_try_build_fix_loop_survives_pytest_crash_after_patch(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pytest-runner crash after applying a patch must not abort the remaining loop.

    Preconditions:
        Initial syntax check succeeds and the first ``run_pytest`` reports a test failure
        so the LLM repair loop is entered; the second ``run_pytest`` (post-patch) raises.
    Postconditions:
        The function returns ``(False, summary)`` rather than propagating the exception.
    """
    _write(tmp_path / "app.py", "def foo():\n    return 1\n")
    _write(tmp_path / "tests" / "test_app.py", "def test_foo():\n    assert True\n")

    syntax_ok = CommandResult(success=True, exit_code=0, stdout="", stderr="")
    pytest_fail = CommandResult(
        success=False,
        exit_code=1,
        stdout="FAILED tests/test_app.py::test_foo",
        stderr="",
    )
    calls = {"pytest": 0}

    def fake_pytest(*args, **kwargs):
        calls["pytest"] += 1
        if calls["pytest"] == 1:
            return pytest_fail
        raise RuntimeError("pytest env exploded after patch")

    monkeypatch.setattr(
        "shared.command_runner.executor.run_python_syntax_check",
        lambda *args, **kwargs: syntax_ok,
    )
    monkeypatch.setattr("shared.command_runner.executor.run_pytest", fake_pytest)
    monkeypatch.setattr(
        "software_engineering_team.build_fix.get_strands_model",
        lambda *args, **kwargs: object(),
    )

    class _FakeAgent:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __call__(self, prompt: str) -> str:
            return "## FILE app.py ##\ndef foo():\n    return 2\n"

    monkeypatch.setattr("software_engineering_team.build_fix.Agent", _FakeAgent)

    with caplog.at_level(logging.WARNING):
        success, error_output = _try_build_fix_one_at_a_time(
            tmp_path, "backend", "task-pytest-crash-loop"
        )

    assert success is False
    assert "pytest env exploded after patch" in error_output
    assert any("pytest failed to run" in record.message for record in caplog.records)


def test_try_build_fix_backend_syntax_parser_preserves_windows_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backend syntax-error parser must preserve Windows paths and colon messages.

    Preconditions:
        ``run_python_syntax_check`` reports a failure whose stderr line uses a
        Windows drive-letter path and a colon-bearing message
        (``"C:\\proj\\a.py: SyntaxError: invalid syntax"``).
    Postconditions:
        The issue handed to the repair loop carries the full path as ``file_path``
        and the full text after ``": "`` as ``description`` — the ": " delimiter is
        used so the drive-letter colon and the message colons are not split on.
    """
    _write(tmp_path / "app.py", "def foo():\n    return 1\n")

    syntax_fail = CommandResult(
        success=False,
        exit_code=1,
        stdout="",
        stderr="Syntax errors found:\nC:\\proj\\a.py: SyntaxError: invalid syntax",
    )
    monkeypatch.setattr(
        "shared.command_runner.executor.run_python_syntax_check",
        lambda *args, **kwargs: syntax_fail,
    )
    monkeypatch.setattr(
        "software_engineering_team.build_fix.get_strands_model",
        lambda *args, **kwargs: object(),
    )

    captured: list[dict] = []

    def _capture(*, issue, **kwargs):
        captured.append(issue)
        return False  # do not apply; loop moves on and exhausts

    monkeypatch.setattr("software_engineering_team.build_fix._execute_llm_repair_attempt", _capture)

    success, _ = _try_build_fix_one_at_a_time(tmp_path, "backend", "task-win-path")

    assert success is False
    assert len(captured) == 1
    assert captured[0]["file_path"] == "C:\\proj\\a.py"
    assert captured[0]["description"] == "SyntaxError: invalid syntax"


def test_is_command_result_rejects_none_and_incomplete_objects() -> None:
    """``_is_command_result`` guards the CommandResult interface the repair loop relies on.

    Preconditions:
        None.
    Postconditions:
        Returns ``False`` for ``None`` and for objects missing ``success`` or a
        callable ``parsed_failures``; returns ``True`` for a real ``CommandResult``.
    """

    class _NoParsedFailures:
        success = False

    class _NonCallableParsedFailures:
        success = False
        parsed_failures = "not callable"

    assert _is_command_result(None) is False
    assert _is_command_result(object()) is False
    assert _is_command_result(_NoParsedFailures()) is False
    assert _is_command_result(_NonCallableParsedFailures()) is False
    assert (
        _is_command_result(CommandResult(success=True, exit_code=0, stdout="", stderr="")) is True
    )


def test_try_build_fix_aborts_on_unusable_verification_result(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``None`` post-fix verification result must abort the loop gracefully, not raise.

    Preconditions:
        Initial syntax check succeeds and ``run_pytest`` reports a test failure so the
        LLM repair loop is entered and applies a patch; the post-fix verification then
        returns ``None`` (an unusable result).
    Postconditions:
        ``_try_build_fix_one_at_a_time`` returns ``(False, summary)`` and logs an
        "unusable result" warning instead of raising ``AttributeError``.
    """
    _write(tmp_path / "app.py", "def foo():\n    return 1\n")
    _write(tmp_path / "tests" / "test_app.py", "def test_foo():\n    assert True\n")

    syntax_ok = CommandResult(success=True, exit_code=0, stdout="", stderr="")
    pytest_fail = CommandResult(
        success=False,
        exit_code=1,
        stdout="FAILED tests/test_app.py::test_foo",
        stderr="",
    )

    monkeypatch.setattr(
        "shared.command_runner.executor.run_python_syntax_check",
        lambda *args, **kwargs: syntax_ok,
    )
    monkeypatch.setattr(
        "shared.command_runner.executor.run_pytest",
        lambda *args, **kwargs: pytest_fail,
    )
    # Post-fix verification yields an unusable result to exercise the loop guard.
    monkeypatch.setattr(
        "software_engineering_team.build_fix._run_post_fix_build_verification",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "software_engineering_team.build_fix.get_strands_model",
        lambda *args, **kwargs: object(),
    )

    class _FakeAgent:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __call__(self, prompt: str) -> str:
            return "## FILE app.py ##\ndef foo():\n    return 2\n"

    monkeypatch.setattr("software_engineering_team.build_fix.Agent", _FakeAgent)

    with caplog.at_level(logging.WARNING):
        success, error_output = _try_build_fix_one_at_a_time(
            tmp_path, "backend", "task-unusable-verification"
        )

    assert success is False
    assert "no usable result" in error_output
    assert any("unusable result" in record.message for record in caplog.records)


def test_try_build_fix_survives_repair_attempt_exception(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected error inside a single repair attempt must not abort the loop.

    Preconditions:
        ``tmp_path`` contains a Python file with a real syntax error so the
        backend branch populates ``issues`` and enters the repair loop, and
        ``_execute_llm_repair_attempt`` raises when called.
    Postconditions:
        ``_try_build_fix_one_at_a_time`` logs the failure and returns
        ``(False, summary)`` instead of propagating the exception.
    """
    _write(tmp_path / "app.py", "def foo(:\n    return 1\n")

    monkeypatch.setattr(
        "software_engineering_team.build_fix.get_strands_model",
        lambda *args, **kwargs: object(),
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("repair boom")

    monkeypatch.setattr("software_engineering_team.build_fix._execute_llm_repair_attempt", _boom)

    with caplog.at_level(logging.WARNING):
        success, error_output = _try_build_fix_one_at_a_time(
            tmp_path, "backend", "task-repair-boom"
        )

    assert success is False
    assert error_output
    assert any("unexpected error during repair" in record.message for record in caplog.records)
