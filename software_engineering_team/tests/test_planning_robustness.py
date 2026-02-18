"""Tests for planning robustness: model_to_dict, fallback overview, and planning failure recovery."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from planning_team.project_planning_agent.models import (
    ProjectOverview,
    build_fallback_overview_from_requirements,
)
from shared.models import ProductRequirements, model_to_dict


def test_model_to_dict_uses_model_dump_when_available() -> None:
    """model_to_dict uses .model_dump() for Pydantic v2-style models."""
    overview = ProjectOverview(
        primary_goal="Test goal",
        secondary_goals=["g1"],
        milestones=[],
        risk_items=[],
        delivery_strategy="vertical slices",
    )
    result = model_to_dict(overview)
    assert isinstance(result, dict)
    assert result.get("primary_goal") == "Test goal"
    assert result.get("secondary_goals") == ["g1"]
    assert result.get("delivery_strategy") == "vertical slices"


def test_model_to_dict_uses_dict_when_model_dump_missing() -> None:
    """model_to_dict falls back to .dict() for Pydantic v1-style models."""
    # Simulate Pydantic v1 model: has .dict() but not .model_dump()
    class PydanticV1Style:
        def dict(self):
            return {"primary_goal": "v1 goal", "secondary_goals": []}

    obj = PydanticV1Style()
    assert not hasattr(obj, "model_dump")
    result = model_to_dict(obj)
    assert result == {"primary_goal": "v1 goal", "secondary_goals": []}


def test_model_to_dict_returns_empty_dict_for_none() -> None:
    """model_to_dict returns {} for None."""
    assert model_to_dict(None) == {}


def test_model_to_dict_handles_plain_object_with_dict_attr() -> None:
    """model_to_dict uses __dict__ for plain objects when no model_dump/dict."""
    class Plain:
        def __init__(self):
            self.x = 1
            self.y = "two"

    result = model_to_dict(Plain())
    assert result == {"x": 1, "y": "two"}


def test_model_to_dict_falls_back_on_attribute_error() -> None:
    """model_to_dict falls back to .dict() when .model_dump() raises AttributeError."""
    # Simulate object where hasattr(model_dump) is True but call raises (e.g. proxy/partial impl)
    class BrokenModelDump:
        def model_dump(self):
            raise AttributeError("model_dump not implemented")

        def dict(self):
            return {"primary_goal": "recovered", "fallback": True}

    obj = BrokenModelDump()
    result = model_to_dict(obj)
    assert result == {"primary_goal": "recovered", "fallback": True}


def test_build_fallback_overview_from_requirements_structure() -> None:
    """Fallback overview has correct structure and required fields."""
    reqs = ProductRequirements(
        title="Todo App",
        description="A multi-tenant todo application with token auth",
        acceptance_criteria=["AC1", "AC2"],
        constraints=["Use PostgreSQL"],
        priority="high",
    )
    overview = build_fallback_overview_from_requirements(reqs)
    assert isinstance(overview, ProjectOverview)
    assert "Todo App" in overview.primary_goal
    assert "multi-tenant" in overview.primary_goal or "todo" in overview.primary_goal.lower()
    assert len(overview.secondary_goals) >= 1
    assert len(overview.milestones) == 3
    assert len(overview.risk_items) == 3
    assert overview.delivery_strategy
    assert overview.milestones[0].id == "M1"
    assert overview.milestones[1].id == "M2"
    assert overview.milestones[2].id == "M3"


def test_build_fallback_overview_from_minimal_requirements() -> None:
    """Fallback works with minimal ProductRequirements."""
    reqs = ProductRequirements(
        title="",
        description="",
        acceptance_criteria=[],
        constraints=[],
    )
    overview = build_fallback_overview_from_requirements(reqs)
    assert isinstance(overview, ProjectOverview)
    assert overview.primary_goal  # Should have default
    assert "Deliver" in overview.primary_goal or overview.primary_goal
    assert overview.delivery_strategy
    assert len(overview.milestones) == 3
    assert len(overview.risk_items) == 3


def test_fallback_overview_serializes_via_model_to_dict() -> None:
    """Fallback ProjectOverview can be serialized with model_to_dict for downstream agents."""
    reqs = ProductRequirements(
        title="Test",
        description="Desc",
        acceptance_criteria=["AC1"],
        constraints=[],
    )
    overview = build_fallback_overview_from_requirements(reqs)
    d = model_to_dict(overview)
    assert isinstance(d, dict)
    assert "primary_goal" in d
    assert "milestones" in d
    assert "risk_items" in d
    assert "delivery_strategy" in d
    assert isinstance(d["milestones"], list)
    assert isinstance(d["risk_items"], list)
