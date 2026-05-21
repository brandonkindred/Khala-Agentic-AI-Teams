"""Tests for helper functions in the deprecated frontend_team module.

The full orchestrator pipeline is too expensive to exercise end-to-end, but
the small helpers are easy wins.
"""

from __future__ import annotations

from pathlib import Path

from software_engineering_team.frontend_team_deprecated import orchestrator as orch_mod
from software_engineering_team.frontend_team_deprecated.orchestrator import (
    _is_lightweight_task,
    _read_repo_code,
    _task_requirements_with_route_expectations,
)
from software_engineering_team.shared.models import Task, TaskStatus, TaskType


def _task(description="", task_id="t1"):
    return Task(
        id=task_id,
        type=TaskType.FRONTEND,
        title="t",
        description=description,
        assignee="frontend",
        status=TaskStatus.PENDING,
    )


def test_is_lightweight_task_with_keyword():
    assert _is_lightweight_task(_task(description="fix the button color"))
    assert _is_lightweight_task(_task(description="update copy on the page"))
    assert _is_lightweight_task(_task(description="refactor the helper"))


def test_is_lightweight_task_long_desc_not_lightweight():
    long = "implement " + ("x" * 500)
    assert not _is_lightweight_task(_task(description=long))


def test_is_lightweight_task_no_keyword():
    assert not _is_lightweight_task(_task(description="build a new dashboard"))


def test_is_lightweight_task_empty():
    assert not _is_lightweight_task(_task(description=""))


def test_read_repo_code_empty(tmp_path: Path):
    out = _read_repo_code(tmp_path)
    # Should return empty or near-empty for empty dir
    assert isinstance(out, str)


def test_read_repo_code_with_files(tmp_path: Path):
    (tmp_path / "main.ts").write_text("const x = 1;")
    (tmp_path / "ignore.txt").write_text("not js")
    out = _read_repo_code(tmp_path)
    assert "main.ts" in out
    assert "const x = 1" in out


def test_task_requirements_with_route_expectations(tmp_path: Path):
    task = Task(
        id="t1",
        type=TaskType.FRONTEND,
        title="Build login",
        description="UI for login form",
        assignee="frontend",
        requirements="Make a login page",
        acceptance_criteria=["AC1"],
    )
    out = _task_requirements_with_route_expectations(task, tmp_path)
    assert isinstance(out, str)
    assert "login" in out.lower() or "Make a login page" in out


def test_orch_module_constants():
    """Module-level constants exist and have expected values."""
    assert orch_mod.MAX_CODE_REVIEW_ITERATIONS > 0
    assert orch_mod.MAX_EXISTING_CODE_CHARS > 0
    assert "Frontend Security" in orch_mod.FRONTEND_SECURITY_CHECKLIST
    assert "accessibility" in orch_mod.FRONTEND_A11Y_CHECKLIST
    assert "anti-patterns" in orch_mod.FRONTEND_CODE_REVIEW_CHECKLIST
