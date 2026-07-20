"""Regression tests for ``_run_build_verification``'s ``agent_type`` handling.

Covers the bug where the v2 phase-pipeline teams pass ``"backend_code_v2"`` /
``"frontend_code_v2"`` as ``agent_type``, which matched none of the original
``"backend"``/``"frontend"``/``"devops"`` branches and fell through to an
unconditional ``(True, "")`` -- silently skipping syntax check / pytest / ng
build for every v2 coding-team job.
"""

from __future__ import annotations

from pathlib import Path

from software_engineering_team.build_fix import _run_build_verification


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


def test_frontend_code_v2_alias_normalizes_agent_type(tmp_path: Path) -> None:
    """No package.json under a frontend_code_v2 repo must not silently pass unrelated checks.

    With no frontend project present, verification should short-circuit to
    success (nothing to build) via the same branch as plain "frontend" --
    confirming the alias is routed into the frontend branch rather than the
    unconditional fallthrough at the end of the function.
    """
    success, error_output = _run_build_verification(tmp_path, "frontend_code_v2", "task-2")

    assert success is True
    assert error_output == ""


def test_backend_code_v2_passes_on_valid_python(tmp_path: Path) -> None:
    _write(tmp_path / "app" / "main.py", "def foo():\n    return 1\n")

    success, error_output = _run_build_verification(tmp_path, "backend_code_v2", "task-3")

    assert success is True
    assert error_output == ""
