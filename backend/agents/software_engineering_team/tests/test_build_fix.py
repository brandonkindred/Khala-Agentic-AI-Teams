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


def test_safe_repair_write_path_rejects_traversal(tmp_path: Path) -> None:
    from software_engineering_team.build_fix import _safe_repair_write_path

    root = tmp_path.resolve()
    assert _safe_repair_write_path(root, "../escape.py") is None
    assert _safe_repair_write_path(root, "") is None
    assert _safe_repair_write_path(root, "/tmp/fix.py") is None


def test_safe_repair_write_path_rejects_venv(tmp_path: Path) -> None:
    from software_engineering_team.build_fix import _safe_repair_write_path

    root = tmp_path.resolve()
    assert _safe_repair_write_path(root, "venv/lib/site.py") is None
    assert _safe_repair_write_path(root, ".venv/lib/site.py") is None


def test_safe_repair_write_path_rejects_unsnapshotted_dirs(tmp_path: Path) -> None:
    from software_engineering_team.build_fix import _safe_repair_write_path

    root = tmp_path.resolve()
    assert _safe_repair_write_path(root, "build/out.js") is None
    assert _safe_repair_write_path(root, "dist/app.js") is None
    assert _safe_repair_write_path(root, "node_modules/pkg/index.js") is None
    assert _safe_repair_write_path(root, ".pytest_cache/README.md") is None
    assert _safe_repair_write_path(root, "__pycache__/a.pyc") is None
    assert _safe_repair_write_path(root, ".angular/cache") is None
    assert _safe_repair_write_path(root, ".git/config") is None


def test_safe_repair_write_path_accepts_in_repo_source(tmp_path: Path) -> None:
    from software_engineering_team.build_fix import _safe_repair_write_path

    root = tmp_path.resolve()
    out = _safe_repair_write_path(root, "app/main.py")
    assert out == root / "app" / "main.py"
