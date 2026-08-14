"""Tests for ``_run_build_verification`` aliases and the extracted build-fix helpers.

Covers the v2 ``agent_type`` aliases that must not skip syntax check / pytest /
ng build, plus unit tests for ``_collect_project_files``,
``_execute_llm_repair_attempt``, and ``_run_post_fix_build_verification``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from software_engineering_team.build_fix import (
    _collect_project_files,
    _execute_llm_repair_attempt,
    _run_build_verification,
    _run_post_fix_build_verification,
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
