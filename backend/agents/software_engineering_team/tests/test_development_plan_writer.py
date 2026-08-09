"""Tests for ``development_plan_writer`` — writes Markdown plan artifacts to
the per-project ``plan/`` folder.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


def _overview(**overrides):
    """Build a minimal SimpleNamespace project overview that satisfies the
    attribute-based contract of ``write_project_overview_plan``."""
    defaults = dict(
        primary_goal="Build a great product",
        secondary_goals=["scale", "delight users"],
        delivery_strategy="ship MVP first",
        milestones=[
            SimpleNamespace(
                name="M1",
                target_order=1,
                description="initial",
                scope_summary="auth",
                definition_of_done="login works",
            ),
            SimpleNamespace(
                name="M2",
                target_order=2,
                description="",
                scope_summary="",
                definition_of_done="",
            ),
        ],
        scope_cut="MVP only",
        epic_story_breakdown=[
            SimpleNamespace(
                id="E1", name="Auth", description="login", dependencies=["E0"], scope="MVP"
            ),
            SimpleNamespace(
                id="E2", name="Payments", description="", dependencies=[], scope=""
            ),
        ],
        non_functional_requirements=["fast", "secure"],
        risk_items=[
            SimpleNamespace(severity="high", description="risk1", mitigation="mit1"),
            SimpleNamespace(severity="low", description="risk2", mitigation=""),
        ],
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_normalize_mermaid_strips_fences() -> None:
    from software_engineering_team.shared.development_plan_writer import _normalize_mermaid

    raw = "```mermaid\ngraph LR\n  A-->B\n```"
    assert _normalize_mermaid(raw) == "graph LR\n  A-->B"


def test_normalize_mermaid_passthrough() -> None:
    from software_engineering_team.shared.development_plan_writer import _normalize_mermaid

    raw = "graph LR\n  A-->B"
    assert _normalize_mermaid(raw) == raw


def test_write_project_overview_plan(tmp_path: Path) -> None:
    from software_engineering_team.shared.development_plan_writer import (
        write_project_overview_plan,
    )

    out = write_project_overview_plan(tmp_path, _overview(), plan_dir=tmp_path / "plan")
    assert out.exists()
    text = out.read_text()
    assert "Build a great product" in text
    assert "## Secondary Goals" in text
    assert "## Milestones" in text
    assert "M1" in text
    assert "## Scope Cut" in text
    assert "## Epic/Story Breakdown" in text
    assert "## Non-Functional Requirements" in text
    assert "## Risks" in text


def test_write_project_overview_plan_minimal(tmp_path: Path) -> None:
    from software_engineering_team.shared.development_plan_writer import (
        write_project_overview_plan,
    )

    overview = _overview(
        primary_goal="",
        secondary_goals=[],
        delivery_strategy="",
        milestones=[],
        scope_cut="",
        epic_story_breakdown=[],
        non_functional_requirements=[],
        risk_items=[],
    )
    out = write_project_overview_plan(tmp_path, overview)
    text = out.read_text()
    assert "No primary goal provided" in text


def test_write_project_overview_default_plan_dir(tmp_path: Path) -> None:
    """When ``plan_dir`` is None, the file should land under ``{repo}/plan``."""
    from software_engineering_team.shared.development_plan_writer import (
        write_project_overview_plan,
    )

    out = write_project_overview_plan(tmp_path, _overview())
    assert out.parent.name == "plan"
    assert out.parent.parent == tmp_path.resolve()


def test_write_features_and_functionality_plan(tmp_path: Path) -> None:
    from software_engineering_team.shared.development_plan_writer import (
        write_features_and_functionality_plan,
    )

    out = write_features_and_functionality_plan(tmp_path, "## Features\nLogin")
    text = out.read_text()
    assert "Features and Functionality" in text
    assert "Login" in text


def test_write_features_and_functionality_empty(tmp_path: Path) -> None:
    from software_engineering_team.shared.development_plan_writer import (
        write_features_and_functionality_plan,
    )

    out = write_features_and_functionality_plan(tmp_path, "")
    assert "No features document generated" in out.read_text()


def test_write_architecture_plan_full(tmp_path: Path) -> None:
    from shared.dev_models.models import (
        ArchitectureComponent,
        SystemArchitecture,
    )
    from software_engineering_team.shared.development_plan_writer import (
        write_architecture_plan,
    )

    arch = SystemArchitecture(
        overview="A small system",
        architecture_document="# Arch doc body",
        components=[
            ArchitectureComponent(
                name="API",
                type="backend",
                description="REST API",
                technology="FastAPI",
                dependencies=["DB"],
                interfaces=["HTTP"],
            ),
        ],
        diagrams={"flow": "```mermaid\ngraph LR\nA-->B\n```"},
        decisions=[
            {"id": "ADR-001", "title": "Use PG", "rationale": "stable"},
            # Without an id, the writer uses the "name" / "title" / "Decision" fallback
            {"name": "Use REST"},
        ],
        tenancy_model="pooled",
        reliability_model="multi-zone",
    )
    out = write_architecture_plan(tmp_path, arch)
    text = out.read_text()
    assert "## Overview" in text
    assert "A small system" in text
    assert "## Components" in text
    assert "API" in text
    assert "FastAPI" in text
    assert "**Dependencies:** DB" in text
    assert "## Diagrams" in text
    assert "graph LR\nA-->B" in text  # Mermaid normalized
    assert "## Tenancy Model" in text
    assert "## Reliability Model" in text
    assert "## Architecture Decision Records" in text
    assert "ADR-001" in text


def test_write_architecture_plan_minimal(tmp_path: Path) -> None:
    from shared.dev_models.models import SystemArchitecture
    from software_engineering_team.shared.development_plan_writer import (
        write_architecture_plan,
    )

    arch = SystemArchitecture(overview="")
    out = write_architecture_plan(tmp_path, arch)
    text = out.read_text()
    assert "No overview provided" in text


def test_write_tech_lead_plan(tmp_path: Path) -> None:
    from shared.dev_models.models import Task, TaskAssignment, TaskType
    from software_engineering_team.shared.development_plan_writer import write_tech_lead_plan

    tasks = [
        Task(
            id="t1",
            type=TaskType.BACKEND,
            title="API",
            assignee="be",
            description="impl REST",
            user_story="As a user, I want API",
            requirements="Use FastAPI",
            acceptance_criteria=["AC1", "AC2"],
            dependencies=["t0"],
            metadata={"component_name": "API"},
        ),
        Task(
            id="t2",
            type=TaskType.FRONTEND,
            title="UI",
            assignee="fe",
            description="",
        ),
    ]
    assignment = TaskAssignment(
        tasks=tasks,
        execution_order=["t1", "t2"],
        rationale="rationale text",
    )
    mapping = [{"spec_item": "REQ-1", "task_ids": ["t1"]}]
    out = write_tech_lead_plan(
        tmp_path,
        assignment,
        summary="overall summary",
        requirement_task_mapping=mapping,
        validation_report="Validation: PASS",
    )
    text = out.read_text()
    assert "## Summary" in text
    assert "overall summary" in text
    assert "Validation: PASS" in text
    assert "## Rationale" in text
    assert "## Requirement → Task Mapping" in text
    assert "REQ-1" in text
    assert "### t1" in text
    assert "API" in text
    assert "AC1" in text
    assert "**Dependencies:** t0" in text
    assert "**Architecture component:** API" in text
    assert "### t2" in text


def test_write_tech_lead_plan_minimal(tmp_path: Path) -> None:
    from shared.dev_models.models import Task, TaskAssignment, TaskType
    from software_engineering_team.shared.development_plan_writer import write_tech_lead_plan

    assignment = TaskAssignment(
        tasks=[Task(id="t1", type=TaskType.BACKEND, assignee="be")],
        execution_order=["t1"],
    )
    out = write_tech_lead_plan(tmp_path, assignment)
    text = out.read_text()
    assert "No summary provided" in text
    assert "No description" in text
