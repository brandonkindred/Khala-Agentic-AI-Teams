"""Tests for the SE-pipeline release hook added in #371.

The hook lives in ``orchestrator._maybe_ship_sprint_release``. We pin
the contract points the SE pipeline relies on without spinning up the
full ``run_orchestrator`` (the function is ~800 lines and pulls in spec
parsing, planning, and the coding team):

* ``sprint_id is None`` → no-op (one-shot path is byte-identical to
  pre-#371).
* sprint with open stories → log + skip; no release row, no feedback.
* sprint complete + no integration issues → release row + plan/releases
  file written; no feedback rows.
* sprint complete + critical/high integration issues → release row +
  one feedback item per issue, all tagged with the sprint.
* ``ReleaseManagerAgent`` raising mid-call → swallowed by the hook + a
  ``release-manager-error`` feedback item is opened so the next groom
  notices.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

# Load the orchestrator module the same way the team API does. Mirrors
# the pattern in ``test_orchestrator_sprint_path.py`` so we don't pull
# in the whole SE pipeline at collection time.
_team_dir = Path(__file__).resolve().parent.parent
if str(_team_dir) not in sys.path:
    sys.path.insert(0, str(_team_dir))
_spec = importlib.util.spec_from_file_location(
    "software_engineering_orchestrator_for_release_hook",
    _team_dir / "orchestrator.py",
)
_orchestrator = importlib.util.module_from_spec(_spec)


@pytest.fixture(scope="module", autouse=True)
def _load_module() -> None:
    _spec.loader.exec_module(_orchestrator)


from product_delivery.models import (  # noqa: E402 — must come after sys.path tweak
    FeedbackItem,
    Release,
    Sprint,
    SprintWithStories,
    Story,
)


def _now() -> datetime:
    return datetime(2026, 5, 2, 12, 0, 0, tzinfo=timezone.utc)


def _story(sid: str, status: str = "done") -> Story:
    n = _now()
    return Story(
        id=sid,
        epic_id="epic-1",
        title=f"Story {sid}",
        user_story=f"as user I want {sid}",
        status=status,
        wsjf_score=10.0,
        rice_score=None,
        estimate_points=3.0,
        author="tester",
        created_at=n,
        updated_at=n,
    )


def _sprint() -> Sprint:
    n = _now()
    return Sprint(
        id="sprint-1",
        product_id="product-1",
        name="S1",
        capacity_points=13.0,
        starts_at=None,
        ends_at=None,
        status="active",
        author="tester",
        created_at=n,
        updated_at=n,
    )


class _IntegrationIssue:
    """Duck-typed stand-in for ``IntegrationOutput.issues[*]``.

    Keeping this local avoids importing the integration_team module
    (which depends on strands) at collection time.
    """

    def __init__(
        self,
        severity: str,
        description: str,
        backend: str = "",
        frontend: str = "",
        recommendation: str = "",
    ) -> None:
        self.severity = severity
        self.category = "contract_mismatch"
        self.description = description
        self.backend_location = backend
        self.frontend_location = frontend
        self.recommendation = recommendation


class _IntegrationResult:
    def __init__(self, issues: list[_IntegrationIssue]) -> None:
        self.passed = not any(i.severity in ("critical", "high") for i in issues)
        self.issues = issues
        self.summary = ""
        self.fix_task_suggestions: list[Any] = []


class _StubStore:
    """In-memory stand-in for ``ProductDeliveryStore`` — only the
    methods the hook + ReleaseManagerAgent call.
    """

    def __init__(
        self,
        *,
        sprint_view: SprintWithStories | None,
        open_count: int = 0,
        ship_raises: Exception | None = None,
    ) -> None:
        self._sprint_view = sprint_view
        self._open_count = open_count
        self._ship_raises = ship_raises
        self.releases: list[dict[str, Any]] = []
        self.feedback: list[dict[str, Any]] = []

    # --- read paths used by the hook + the agent ----------------------

    def get_sprint_with_stories(self, sprint_id: str) -> SprintWithStories | None:
        return self._sprint_view

    def count_open_stories_in_sprint(self, sprint_id: str) -> int:
        if self._sprint_view is None:
            from product_delivery.store import UnknownProductDeliveryEntity

            raise UnknownProductDeliveryEntity(f"unknown sprint: {sprint_id}")
        return self._open_count

    def get_product_id_for_sprint(self, sprint_id: str) -> str | None:
        return self._sprint_view.sprint.product_id if self._sprint_view else None

    # --- write paths used by the agent --------------------------------

    def create_release(
        self,
        *,
        sprint_id: str,
        version: str,
        notes_path: str | None,
        shipped_at: datetime | None,
        author: str,
    ) -> Release:
        if self._ship_raises is not None:
            raise self._ship_raises
        self.releases.append(
            {
                "sprint_id": sprint_id,
                "version": version,
                "notes_path": notes_path,
                "shipped_at": shipped_at,
                "author": author,
            }
        )
        return Release(
            id=f"release-{len(self.releases)}",
            sprint_id=sprint_id,
            version=version,
            notes_path=notes_path,
            shipped_at=shipped_at,
            author=author,
            created_at=_now(),
            updated_at=_now(),
        )

    def create_feedback_item(
        self,
        *,
        product_id: str,
        source: str,
        raw_payload: dict[str, Any],
        severity: str,
        linked_story_id: str | None,
        author: str,
        sprint_id: str | None = None,
    ) -> FeedbackItem:
        self.feedback.append(
            {
                "product_id": product_id,
                "source": source,
                "raw_payload": raw_payload,
                "severity": severity,
                "linked_story_id": linked_story_id,
                "author": author,
                "sprint_id": sprint_id,
            }
        )
        return FeedbackItem(
            id=f"feedback-{len(self.feedback)}",
            product_id=product_id,
            source=source,
            raw_payload=raw_payload,
            severity=severity,
            status="open",
            linked_story_id=linked_story_id,
            sprint_id=sprint_id,
            author=author,
            created_at=_now(),
            updated_at=_now(),
        )


def _install_stub_store(monkeypatch: pytest.MonkeyPatch, store: _StubStore) -> None:
    """Patch the lazy-imported ``product_delivery`` module so the hook
    sees our stub. Both ``get_store`` (used by the hook) and the
    ``release_manager_agent`` package path need to resolve.
    """
    import product_delivery as pd

    monkeypatch.setattr(pd, "get_store", lambda: store)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_hook_no_op_when_sprint_id_is_none(tmp_path: Path) -> None:
    """One-shot path is unchanged: hook returns silently."""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    # No store patching needed — hook short-circuits before importing it.
    _orchestrator._maybe_ship_sprint_release(
        sprint_id=None,
        plan_dir=plan_dir,
        int_result=_IntegrationResult([]),
        job_id="job-1",
    )
    assert not (plan_dir / "releases").exists()


def test_hook_skips_when_sprint_has_open_stories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    view = SprintWithStories(
        sprint=_sprint(),
        stories=[_story("s1", status="proposed")],
        acceptance_criteria_by_story_id={},
    )
    store = _StubStore(sprint_view=view, open_count=1)
    _install_stub_store(monkeypatch, store)

    _orchestrator._maybe_ship_sprint_release(
        sprint_id="sprint-1",
        plan_dir=plan_dir,
        int_result=_IntegrationResult([]),
        job_id="job-1",
    )
    assert store.releases == []
    assert store.feedback == []
    assert not (plan_dir / "releases").exists()


def test_hook_ships_when_sprint_complete_and_no_issues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    view = SprintWithStories(
        sprint=_sprint(),
        stories=[_story("s1", status="done")],
        acceptance_criteria_by_story_id={},
    )
    store = _StubStore(sprint_view=view, open_count=0)
    _install_stub_store(monkeypatch, store)

    _orchestrator._maybe_ship_sprint_release(
        sprint_id="sprint-1",
        plan_dir=plan_dir,
        int_result=_IntegrationResult([]),
        job_id="job-1",
    )
    # Release row written.
    assert len(store.releases) == 1
    assert store.releases[0]["sprint_id"] == "sprint-1"
    assert store.releases[0]["version"].startswith("2026-")
    # Notes file landed.
    notes_path = Path(store.releases[0]["notes_path"])
    assert notes_path.exists()
    # No issues → no feedback rows.
    assert store.feedback == []


def test_hook_promotes_integration_issues_to_sprint_tagged_feedback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    view = SprintWithStories(
        sprint=_sprint(),
        stories=[_story("s1", status="done")],
        acceptance_criteria_by_story_id={},
    )
    store = _StubStore(sprint_view=view, open_count=0)
    _install_stub_store(monkeypatch, store)

    issues = [
        _IntegrationIssue("critical", "missing endpoint /api/foo", backend="api/foo.py"),
        _IntegrationIssue("medium", "wrong payload shape"),
    ]
    _orchestrator._maybe_ship_sprint_release(
        sprint_id="sprint-1",
        plan_dir=plan_dir,
        int_result=_IntegrationResult(issues),
        job_id="job-1",
    )
    # Two feedback rows, all tagged with the sprint.
    assert len(store.feedback) == 2
    for fb in store.feedback:
        assert fb["sprint_id"] == "sprint-1"
        assert fb["product_id"] == "product-1"
        assert fb["source"] == "se-integration"
    # Severity remap: critical → high; medium → normal.
    severities = sorted(fb["severity"] for fb in store.feedback)
    assert severities == ["high", "normal"]


def test_hook_is_non_fatal_and_records_release_manager_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Agent failure → hook swallows + opens a high-severity error feedback.

    Models the issue's "Failures are non-fatal (log + open a
    high-severity feedback_item so the next grooming sees the
    problem)" requirement.
    """
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    view = SprintWithStories(
        sprint=_sprint(),
        stories=[_story("s1", status="done")],
        acceptance_criteria_by_story_id={},
    )
    store = _StubStore(sprint_view=view, open_count=0, ship_raises=RuntimeError("disk full"))
    _install_stub_store(monkeypatch, store)

    # Hook must not raise.
    _orchestrator._maybe_ship_sprint_release(
        sprint_id="sprint-1",
        plan_dir=plan_dir,
        int_result=_IntegrationResult([]),
        job_id="job-1",
    )
    # No release row (the agent failed before persisting).
    assert store.releases == []
    # But the error was promoted into a feedback row.
    assert len(store.feedback) == 1
    err = store.feedback[0]
    assert err["source"] == "release-manager-error"
    assert err["severity"] == "high"
    assert err["sprint_id"] == "sprint-1"
    assert "disk full" in err["raw_payload"]["error"]
    assert err["raw_payload"]["job_id"] == "job-1"
