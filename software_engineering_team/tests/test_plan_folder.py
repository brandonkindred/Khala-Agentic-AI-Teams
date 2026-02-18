"""Tests for plan folder creation and development plan writer."""

from pathlib import Path

import pytest

from planning_team.plan_dir import ensure_plan_dir, get_plan_dir
from shared.development_plan_writer import (
    write_architecture_plan,
    write_project_overview_plan,
)
from shared.models import ProductRequirements, SystemArchitecture


def test_ensure_plan_dir_creates_folder(tmp_path: Path) -> None:
    """ensure_plan_dir creates plan/ when it does not exist."""
    plan_dir = ensure_plan_dir(tmp_path)
    assert plan_dir == tmp_path / "plan"
    assert plan_dir.exists()
    assert plan_dir.is_dir()


def test_ensure_plan_dir_idempotent(tmp_path: Path) -> None:
    """ensure_plan_dir is idempotent when folder already exists."""
    (tmp_path / "plan").mkdir()
    plan_dir = ensure_plan_dir(tmp_path)
    assert plan_dir.exists()
    assert (tmp_path / "plan").exists()


def test_get_plan_dir_resolves_path(tmp_path: Path) -> None:
    """get_plan_dir returns plan path without creating."""
    plan_dir = get_plan_dir(tmp_path)
    assert plan_dir == tmp_path.resolve() / "plan"


def test_write_project_overview_plan_uses_plan_dir(tmp_path: Path) -> None:
    """write_project_overview_plan writes to plan/ when plan_dir provided."""
    from planning_team.project_planning_agent.models import ProjectOverview

    overview = ProjectOverview(
        primary_goal="Test goal",
        features_and_functionality_doc="Features",
    )
    plan_dir = ensure_plan_dir(tmp_path)
    out = write_project_overview_plan(tmp_path, overview, plan_dir=plan_dir)
    assert out == plan_dir / "project_overview.md"
    assert out.exists()
    assert "Test goal" in out.read_text()


def test_write_architecture_plan_uses_plan_dir(tmp_path: Path) -> None:
    """write_architecture_plan writes to plan/ when plan_dir provided."""
    arch = SystemArchitecture(
        overview="Test overview",
        components=[],
    )
    plan_dir = ensure_plan_dir(tmp_path)
    out = write_architecture_plan(tmp_path, arch, plan_dir=plan_dir)
    assert out == plan_dir / "architecture.md"
    assert out.exists()
    assert "Test overview" in out.read_text()
