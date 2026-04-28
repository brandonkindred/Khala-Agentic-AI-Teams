"""Unit tests for :class:`SprintPlannerAgent`.

The agent is a thin shell over ``store.select_sprint_scope``. These
tests stub the store so they don't need Postgres and pin the contract
points the route relies on:

* ``capacity_points=None`` reads the sprint row's stored capacity;
* an explicit ``capacity_points`` overrides it;
* a missing sprint surfaces as ``UnknownProductDeliveryEntity``;
* the underlying store exception passes through unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from product_delivery.models import Sprint, SprintPlanResult
from product_delivery.sprint_planner_agent import SprintPlannerAgent
from product_delivery.store import UnknownProductDeliveryEntity


class _StubStore:
    def __init__(self, sprint: Sprint | None) -> None:
        self._sprint = sprint
        self.calls: list[dict[str, Any]] = []

    def get_sprint(self, sprint_id: str) -> Sprint | None:
        # Only matched when the agent is asked to default capacity.
        return self._sprint

    def select_sprint_scope(
        self, *, sprint_id: str, capacity_points: float | None = None
    ) -> SprintPlanResult:
        self.calls.append({"sprint_id": sprint_id, "capacity_points": capacity_points})
        return SprintPlanResult(
            sprint_id=sprint_id,
            selected_story_ids=["story-1"],
            skipped_story_ids=[],
            used_capacity=float(capacity_points or 0.0),
            remaining_capacity=0.0,
            rationale="stub",
        )


def _sprint(*, capacity_points: float = 13.0) -> Sprint:
    now = datetime.now(tz=timezone.utc)
    return Sprint(
        id="sprint-1",
        product_id="product-1",
        name="S1",
        capacity_points=capacity_points,
        starts_at=None,
        ends_at=None,
        status="planned",
        author="tester",
        created_at=now,
        updated_at=now,
    )


def test_plan_uses_sprint_capacity_when_none_supplied() -> None:
    store = _StubStore(_sprint(capacity_points=13.0))
    agent = SprintPlannerAgent(store)  # type: ignore[arg-type]
    result = agent.plan(sprint_id="sprint-1")
    assert store.calls == [{"sprint_id": "sprint-1", "capacity_points": 13.0}]
    assert result.used_capacity == 13.0


def test_plan_overrides_capacity_when_supplied() -> None:
    store = _StubStore(_sprint(capacity_points=13.0))
    agent = SprintPlannerAgent(store)  # type: ignore[arg-type]
    agent.plan(sprint_id="sprint-1", capacity_points=5.0)
    # Override wins; sprint row's 13.0 is unused.
    assert store.calls == [{"sprint_id": "sprint-1", "capacity_points": 5.0}]


def test_plan_raises_when_sprint_missing_for_default_capacity() -> None:
    store = _StubStore(None)
    agent = SprintPlannerAgent(store)  # type: ignore[arg-type]
    with pytest.raises(UnknownProductDeliveryEntity):
        agent.plan(sprint_id="missing")  # capacity_points=None → needs sprint row
    assert store.calls == []  # store.select_sprint_scope was not reached


def test_plan_passes_through_store_exceptions() -> None:
    class _ErrStore(_StubStore):
        def select_sprint_scope(self, **kwargs: Any) -> SprintPlanResult:  # type: ignore[override]
            raise UnknownProductDeliveryEntity("boom")

    store = _ErrStore(_sprint())
    agent = SprintPlannerAgent(store)  # type: ignore[arg-type]
    with pytest.raises(UnknownProductDeliveryEntity, match="boom"):
        agent.plan(sprint_id="sprint-1", capacity_points=1.0)
