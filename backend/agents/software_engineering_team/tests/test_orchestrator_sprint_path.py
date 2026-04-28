"""Tests for the sprint-path synthesis helper added in #370.

The full ``run_orchestrator`` integration test is impractical (the
function spans ~800 lines and pulls in spec parsing, planning V3, the
coding team, etc.), so we pin the new bits directly:

* ``_load_requirements_from_sprint`` builds a ``ProductRequirements``
  whose ``title`` matches the sprint name, whose ``acceptance_criteria``
  are the union of every story's ACs, and whose ``metadata`` carries
  the sprint id + story ids;
* missing sprint → ``UnknownProductDeliveryEntity``;
* zero-story sprint → ``ValueError`` (we never silently fall back to
  repo spec parsing).

These tests stub ``product_delivery.get_store`` so they don't need a
running Postgres.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

# Load the orchestrator module the same way the team API does.
_team_dir = Path(__file__).resolve().parent.parent
if str(_team_dir) not in sys.path:
    sys.path.insert(0, str(_team_dir))
_spec = importlib.util.spec_from_file_location(
    "software_engineering_orchestrator",
    _team_dir / "orchestrator.py",
)
_orchestrator = importlib.util.module_from_spec(_spec)


@pytest.fixture(scope="module", autouse=True)
def _load_module() -> None:
    _spec.loader.exec_module(_orchestrator)


from product_delivery.models import (  # noqa: E402 — must come after sys.path tweak
    AcceptanceCriterion,
    Sprint,
    SprintWithStories,
    Story,
)

# RunTeamRequest is defined in api/main.py alongside the FastAPI app —
# importing the whole module at collection time pulls in the SE
# pipeline. We mirror the orchestrator-loader pattern above and lazy-
# load just the class we need from a fresh module spec.
_api_spec = importlib.util.spec_from_file_location(
    "software_engineering_api_main_for_test",
    _team_dir / "api" / "main.py",
)
_api_main = importlib.util.module_from_spec(_api_spec)


@pytest.fixture(scope="module", autouse=True)
def _load_api_main() -> None:
    _api_spec.loader.exec_module(_api_main)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _story(sid: str, title: str, user_story: str = "") -> Story:
    return Story(
        id=sid,
        epic_id="epic-1",
        title=title,
        user_story=user_story,
        status="proposed",
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


class _StubStore:
    def __init__(self, *, sprint_view: SprintWithStories | None) -> None:
        self._sprint_view = sprint_view

    def get_sprint_with_stories(self, sprint_id: str) -> SprintWithStories | None:
        return self._sprint_view


@pytest.fixture
def patch_product_delivery(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Patch the lazy ``from product_delivery import get_store`` lookup.

    The helper imports inside the function body, so we need to patch
    the *module attribute*. ACs now ride along on
    ``SprintWithStories.acceptance_criteria_by_story_id`` (Codex
    review on PR #396 — fetched in the same snapshot as the stories),
    so the stub store no longer needs a separate AC reader.
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
    sprint = Sprint(
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
    patch_product_delivery["store"] = _StubStore(
        sprint_view=SprintWithStories(
            sprint=sprint,
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

    requirements, spec_markdown = _orchestrator._load_requirements_from_sprint("sprint-1")

    # Title is the sprint name; metadata carries the sprint identity.
    assert requirements.title == "Iteration 5"
    assert requirements.metadata["sprint_id"] == "sprint-1"
    assert requirements.metadata["synthesized_from_sprint"] is True
    assert requirements.metadata["story_ids"] == ["story-1", "story-2"]

    # Acceptance criteria is the union, in story order then create order.
    assert requirements.acceptance_criteria == [
        "submit returns 200",
        "rate-limited",
        "email is sent",
    ]

    # The synthesized markdown carries every story's heading + user_story.
    assert "## Login form" in spec_markdown
    assert "## Forgot password" in spec_markdown
    assert "As a user, I want to log in" in spec_markdown
    # And the body is what got assigned to ``description``.
    assert requirements.description == spec_markdown


def test_load_requirements_from_sprint_raises_when_missing(patch_product_delivery: Any) -> None:
    from product_delivery import UnknownProductDeliveryEntity

    patch_product_delivery["store"] = _StubStore(sprint_view=None)
    with pytest.raises(UnknownProductDeliveryEntity):
        _orchestrator._load_requirements_from_sprint("missing")


def test_load_requirements_from_sprint_raises_on_empty_scope(patch_product_delivery: Any) -> None:
    sprint = Sprint(
        id="sprint-empty",
        product_id="product-1",
        name="Empty",
        capacity_points=0.0,
        starts_at=None,
        ends_at=None,
        status="planned",
        author="tester",
        created_at=_now(),
        updated_at=_now(),
    )
    patch_product_delivery["store"] = _StubStore(
        sprint_view=SprintWithStories(sprint=sprint, stories=[])
    )
    with pytest.raises(ValueError, match="no planned stories"):
        _orchestrator._load_requirements_from_sprint("sprint-empty")


# ---------------------------------------------------------------------------
# RunTeamRequest.sprint_id validator — Codex review on PR #396
# ---------------------------------------------------------------------------


def test_run_team_request_normalises_sprint_id() -> None:
    """A real value passes through, trailing whitespace is stripped."""
    RunTeamRequest = _api_main.RunTeamRequest
    assert RunTeamRequest(repo_path="/tmp/x").sprint_id is None
    assert RunTeamRequest(repo_path="/tmp/x", sprint_id="sprint-1").sprint_id == "sprint-1"
    assert RunTeamRequest(repo_path="/tmp/x", sprint_id="  sprint-2 ").sprint_id == "sprint-2"


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n", " \t\n "])
def test_run_team_request_rejects_blank_sprint_id(blank: str) -> None:
    """Whitespace-only ``sprint_id`` must 422 at the API boundary so a
    mistake doesn't silently enable sprint mode (Codex review on PR #396).
    """
    from pydantic import ValidationError

    RunTeamRequest = _api_main.RunTeamRequest
    with pytest.raises(ValidationError):
        RunTeamRequest(repo_path="/tmp/x", sprint_id=blank)


def _story_with_status(sid: str, title: str, status: str) -> Story:
    """Like ``_story`` but lets the test set a specific status."""
    return Story(
        id=sid,
        epic_id="epic-1",
        title=title,
        user_story="",
        status=status,
        wsjf_score=None,
        rice_score=None,
        estimate_points=None,
        author="tester",
        created_at=_now(),
        updated_at=_now(),
    )


def test_load_requirements_skips_terminal_status_stories(patch_product_delivery: Any) -> None:
    """Stories marked done/completed/cancelled/closed after planning must
    NOT appear in the synthesized spec (Codex review on PR #396).
    Otherwise execution would re-do already-finished work — divergent
    from the planner, which excludes terminal stories at selection.
    """
    s_active = _story_with_status("story-1", "Active work", "in_progress")
    s_done = _story_with_status("story-2", "Already done", "done")
    s_cancelled = _story_with_status("story-3", "Cancelled work", "cancelled")
    sprint = Sprint(
        id="sprint-1",
        product_id="product-1",
        name="S1",
        capacity_points=10.0,
        starts_at=None,
        ends_at=None,
        status="active",
        author="tester",
        created_at=_now(),
        updated_at=_now(),
    )
    patch_product_delivery["store"] = _StubStore(
        sprint_view=SprintWithStories(
            sprint=sprint,
            stories=[s_active, s_done, s_cancelled],
            acceptance_criteria_by_story_id={},
        ),
    )

    requirements, spec_markdown = _orchestrator._load_requirements_from_sprint("sprint-1")

    # Only the active story is in metadata.story_ids and the markdown.
    assert requirements.metadata["story_ids"] == ["story-1"]
    assert "## Active work" in spec_markdown
    assert "## Already done" not in spec_markdown
    assert "## Cancelled work" not in spec_markdown


def test_load_requirements_raises_when_all_stories_terminal(patch_product_delivery: Any) -> None:
    """A sprint where every planned story is terminal-status raises
    `ValueError` — same shape as the empty-scope case, never silently
    falls back to repo spec parsing (Codex review on PR #396).
    """
    s_done = _story_with_status("story-1", "Done", "done")
    sprint = Sprint(
        id="sprint-x",
        product_id="product-1",
        name="X",
        capacity_points=5.0,
        starts_at=None,
        ends_at=None,
        status="completed",
        author="tester",
        created_at=_now(),
        updated_at=_now(),
    )
    patch_product_delivery["store"] = _StubStore(
        sprint_view=SprintWithStories(
            sprint=sprint, stories=[s_done], acceptance_criteria_by_story_id={}
        )
    )
    with pytest.raises(ValueError, match="no executable stories"):
        _orchestrator._load_requirements_from_sprint("sprint-x")


def test_load_requirements_from_sprint_falls_back_when_no_acs(patch_product_delivery: Any) -> None:
    """When stories have no AC rows, we still produce a non-empty AC list.

    Otherwise the synthesized ``ProductRequirements`` would have an
    empty ``acceptance_criteria`` and downstream stages that key off
    that field would short-circuit. The fallback string keeps the
    contract intact.
    """
    s = _story("story-x", "Bare story", "")
    sprint = Sprint(
        id="sprint-x",
        product_id="product-1",
        name="X",
        capacity_points=5.0,
        starts_at=None,
        ends_at=None,
        status="planned",
        author="tester",
        created_at=_now(),
        updated_at=_now(),
    )
    patch_product_delivery["store"] = _StubStore(
        sprint_view=SprintWithStories(
            sprint=sprint, stories=[s], acceptance_criteria_by_story_id={}
        )
    )
    requirements, _ = _orchestrator._load_requirements_from_sprint("sprint-x")
    assert requirements.acceptance_criteria == ["Deliver according to planned story scope."]
