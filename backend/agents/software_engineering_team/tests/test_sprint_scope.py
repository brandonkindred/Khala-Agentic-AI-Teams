"""Tests for shared.sprint_scope.load_requirements_from_sprint (extracted out of
discovery.py so a future V2 activity can reuse it without duplicating the
product_delivery-read logic).

Mirrors the scenarios ``tests/test_orchestrator_sprint_path.py`` already pins
against the orchestrator's re-export — this file exercises the shared helper
directly instead. These tests stub ``product_delivery.get_store`` so they
don't need a running Postgres.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from product_delivery.models import (
    AcceptanceCriterion,
    Sprint,
    SprintWithStories,
    Story,
)
from software_engineering_team.shared.sprint_scope import load_requirements_from_sprint


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _story(sid: str, title: str, user_story: str = "", status: str = "proposed") -> Story:
    return Story(
        id=sid,
        epic_id="epic-1",
        title=title,
        user_story=user_story,
        status=status,
        wsjf_score=None,
        rice_score=None,
        estimate_points=None,
        author="tester",
        created_at=_now(),
        updated_at=_now(),
    )


def _ac(text: str, story_id: str) -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=f"ac-{text[:6]}",
        story_id=story_id,
        text=text,
        satisfied=False,
        author="tester",
        created_at=_now(),
        updated_at=_now(),
    )


def _sprint(**overrides: Any) -> Sprint:
    defaults: dict[str, Any] = dict(
        id="sprint-1",
        product_id="product-1",
        name="Iteration 5",
        capacity_points=13.0,
        starts_at=None,
        ends_at=None,
        status="planned",
        author="tester",
        created_at=_now(),
        updated_at=_now(),
    )
    defaults.update(overrides)
    return Sprint(**defaults)


class _StubStore:
    def __init__(self, *, sprint_view: SprintWithStories | None) -> None:
        self._sprint_view = sprint_view

    def get_sprint_with_stories(self, sprint_id: str) -> SprintWithStories | None:
        return self._sprint_view


@pytest.fixture
def patch_product_delivery(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Patch the lazy ``from product_delivery import get_store`` lookup.

    The helper imports inside the function body, so we need to patch the
    *module attribute* — this works regardless of which module calls the
    helper, since the lazy import reads the current attribute at call time.
    """
    state: dict[str, Any] = {"store": None}

    import product_delivery as pd_mod

    def _fake_get_store() -> Any:
        return state["store"]

    monkeypatch.setattr(pd_mod, "get_store", _fake_get_store)
    return state


def test_load_requirements_from_sprint_synthesizes_from_stories(
    patch_product_delivery: Any,
) -> None:
    s1 = _story("story-1", "Login form", "As a user, I want to log in")
    s2 = _story("story-2", "Forgot password", "As a user, I want to reset my password")
    patch_product_delivery["store"] = _StubStore(
        sprint_view=SprintWithStories(
            sprint=_sprint(),
            stories=[s1, s2],
            acceptance_criteria_by_story_id={
                "story-1": [
                    _ac("submit returns 200", "story-1"),
                    _ac("rate-limited", "story-1"),
                ],
                "story-2": [_ac("email is sent", "story-2")],
            },
        ),
    )

    requirements, spec_markdown = load_requirements_from_sprint("sprint-1")

    assert requirements.title == "Iteration 5"
    assert requirements.metadata["sprint_id"] == "sprint-1"
    assert requirements.metadata["synthesized_from_sprint"] is True
    assert requirements.metadata["story_ids"] == ["story-1", "story-2"]
    assert requirements.acceptance_criteria == [
        "submit returns 200",
        "rate-limited",
        "email is sent",
    ]
    assert "## Login form" in spec_markdown
    assert "## Forgot password" in spec_markdown
    assert "As a user, I want to log in" in spec_markdown
    assert requirements.description == spec_markdown


def test_load_requirements_from_sprint_raises_when_missing(patch_product_delivery: Any) -> None:
    from product_delivery import UnknownProductDeliveryEntity

    patch_product_delivery["store"] = _StubStore(sprint_view=None)

    with pytest.raises(UnknownProductDeliveryEntity):
        load_requirements_from_sprint("sprint-missing")


def test_load_requirements_from_sprint_raises_on_empty_scope(
    patch_product_delivery: Any,
) -> None:
    patch_product_delivery["store"] = _StubStore(
        sprint_view=SprintWithStories(
            sprint=_sprint(),
            stories=[],
            acceptance_criteria_by_story_id={},
        ),
    )

    with pytest.raises(ValueError, match="no planned stories"):
        load_requirements_from_sprint("sprint-1")


def test_load_requirements_skips_terminal_status_stories(patch_product_delivery: Any) -> None:
    active = _story("story-1", "In progress work", status="in_progress")
    done = _story("story-2", "Already done", status="done")
    cancelled = _story("story-3", "Cancelled work", status="cancelled")
    patch_product_delivery["store"] = _StubStore(
        sprint_view=SprintWithStories(
            sprint=_sprint(),
            stories=[active, done, cancelled],
            acceptance_criteria_by_story_id={},
        ),
    )

    requirements, spec_markdown = load_requirements_from_sprint("sprint-1")

    assert requirements.metadata["story_ids"] == ["story-1"]
    assert "## In progress work" in spec_markdown
    assert "## Already done" not in spec_markdown
    assert "## Cancelled work" not in spec_markdown


def test_load_requirements_raises_when_all_stories_terminal(
    patch_product_delivery: Any,
) -> None:
    done = _story("story-1", "Already done", status="done")
    patch_product_delivery["store"] = _StubStore(
        sprint_view=SprintWithStories(
            sprint=_sprint(),
            stories=[done],
            acceptance_criteria_by_story_id={},
        ),
    )

    with pytest.raises(ValueError, match="no executable stories"):
        load_requirements_from_sprint("sprint-1")


def test_load_requirements_from_sprint_falls_back_when_no_acs(
    patch_product_delivery: Any,
) -> None:
    s1 = _story("story-1", "No ACs yet")
    patch_product_delivery["store"] = _StubStore(
        sprint_view=SprintWithStories(
            sprint=_sprint(),
            stories=[s1],
            acceptance_criteria_by_story_id={},
        ),
    )

    requirements, _spec_markdown = load_requirements_from_sprint("sprint-1")

    assert requirements.acceptance_criteria == ["Deliver according to planned story scope."]
