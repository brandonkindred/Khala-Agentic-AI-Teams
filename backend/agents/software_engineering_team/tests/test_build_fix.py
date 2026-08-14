"""Regression tests for ``_run_build_verification``'s ``agent_type`` handling.

Covers the bug where the v2 phase-pipeline teams pass ``"backend_code_v2"`` /
``"frontend_code_v2"`` as ``agent_type``, which matched none of the original
``"backend"``/``"frontend"``/``"devops"`` branches and fell through to an
unconditional ``(True, "")`` -- silently skipping syntax check / pytest / ng
build for every v2 coding-team job.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from shared.command_runner.executor import CommandResult
from software_engineering_team.build_fix import (
    _run_build_verification,
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
    assert any(
        "no frontend project found" in record.message for record in caplog.records
    ), "frontend branch was not actually entered"


def test_backend_code_v2_passes_on_valid_python(tmp_path: Path) -> None:
    _write(tmp_path / "app" / "main.py", "def foo():\n    return 1\n")

    success, error_output = _run_build_verification(tmp_path, "backend_code_v2", "task-3")

    assert success is True
    assert error_output == ""


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
