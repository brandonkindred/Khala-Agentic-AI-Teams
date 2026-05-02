"""Unit tests for :class:`ReleaseManagerAgent`.

The agent is invoked from the SE-pipeline hook after Integration runs;
these tests stub the store + the notes writer so they don't need
Postgres or strands. The contract points the hook relies on are:

* a sprint with all stories terminal → release row + markdown file +
  one feedback item per Integration issue, all tagged with the sprint;
* a sprint with open stories → ``SprintNotComplete`` raised, no row
  written;
* a missing sprint → ``UnknownProductDeliveryEntity`` raised;
* version defaults to ``YYYY-MM-DD``, with ``-N`` suffix on collision;
* notes writer raising mid-call doesn't block — the agent's writer has
  its own deterministic fallback (covered separately in the SE-team
  tests for ``ReleaseNotesAgent``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from product_delivery.models import (
    AcceptanceCriterion,
    FeedbackItem,
    Release,
    Sprint,
    SprintWithStories,
    Story,
)
from product_delivery.release_manager_agent import ReleaseManagerAgent
from product_delivery.store import (
    SprintNotComplete,
    UnknownProductDeliveryEntity,
)

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime(2026, 5, 2, 12, 0, 0, tzinfo=timezone.utc)


def _story(sid: str = "story-1", status: str = "done") -> Story:
    now = _now()
    return Story(
        id=sid,
        epic_id="epic-1",
        title=f"Title {sid}",
        user_story=f"As a user I want feature {sid}",
        status=status,
        wsjf_score=10.0,
        rice_score=None,
        estimate_points=3.0,
        author="tester",
        created_at=now,
        updated_at=now,
    )


def _ac(text: str = "criterion") -> AcceptanceCriterion:
    now = _now()
    return AcceptanceCriterion(
        id=f"ac-{text}",
        story_id="story-1",
        text=text,
        satisfied=True,
        author="tester",
        created_at=now,
        updated_at=now,
    )


def _sprint() -> Sprint:
    now = _now()
    return Sprint(
        id="sprint-1",
        product_id="product-1",
        name="S1",
        capacity_points=13.0,
        starts_at=None,
        ends_at=None,
        status="active",
        author="tester",
        created_at=now,
        updated_at=now,
    )


class _StubWriter:
    def __init__(self, markdown: str = "# Release notes body\n", summary: str = "ok") -> None:
        self.markdown = markdown
        self.summary = summary
        self.calls: list[Any] = []
        self.raise_with: Exception | None = None

    def run(self, input_data: Any) -> Any:
        self.calls.append(input_data)
        if self.raise_with is not None:
            raise self.raise_with
        # Return a duck-typed object so the agent can read `.markdown`,
        # `.summary`, `.llm_failed`, `.error`. Importing the real model
        # here would couple this test to the SE-team module.
        from software_engineering_team.technical_writers.release_notes_agent.models import (
            ReleaseNotesOutput,
        )

        return ReleaseNotesOutput(
            markdown=self.markdown, summary=self.summary, llm_failed=False, error=None
        )


class _StubStore:
    """Minimal store double — only the methods the agent calls."""

    def __init__(
        self,
        *,
        sprint_view: SprintWithStories | None,
        open_count: int = 0,
    ) -> None:
        self._sprint_view = sprint_view
        self._open_count = open_count
        self.created_releases: list[dict[str, Any]] = []
        self.created_feedback: list[dict[str, Any]] = []

    def get_sprint_with_stories(self, sprint_id: str) -> SprintWithStories | None:
        return self._sprint_view

    def count_open_stories_in_sprint(self, sprint_id: str) -> int:
        if self._sprint_view is None:
            raise UnknownProductDeliveryEntity(f"unknown sprint: {sprint_id}")
        return self._open_count

    def create_release(
        self,
        *,
        sprint_id: str,
        version: str,
        notes_path: str | None,
        shipped_at: datetime | None,
        author: str,
    ) -> Release:
        self.created_releases.append(
            {
                "sprint_id": sprint_id,
                "version": version,
                "notes_path": notes_path,
                "shipped_at": shipped_at,
                "author": author,
            }
        )
        return Release(
            id=f"release-{len(self.created_releases)}",
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
        self.created_feedback.append(
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
            id=f"feedback-{len(self.created_feedback)}",
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


def _make_view(
    stories: list[Story], acs: dict[str, list[AcceptanceCriterion]] | None = None
) -> SprintWithStories:
    return SprintWithStories(
        sprint=_sprint(),
        stories=stories,
        acceptance_criteria_by_story_id=acs or {},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_ship_writes_release_row_notes_file_and_promotes_failures(tmp_path: Path) -> None:
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    story = _story("story-1", status="done")
    view = _make_view([story], acs={"story-1": [_ac("ac-1"), _ac("ac-2")]})
    store = _StubStore(sprint_view=view, open_count=0)
    writer = _StubWriter(markdown="# Release notes body for sprint\n", summary="shipped clean")
    agent = ReleaseManagerAgent(store, notes_writer=writer)  # type: ignore[arg-type]

    # Two integration issues: one critical (→ severity=high), one medium (→ normal).
    class _Issue:
        def __init__(self, severity: str, description: str, backend: str = "be.py") -> None:
            self.severity = severity
            self.description = description
            self.category = "contract_mismatch"
            self.backend_location = backend
            self.frontend_location = ""
            self.recommendation = f"fix {description}"

    issues = [
        _Issue("critical", "missing endpoint"),
        _Issue("medium", "wrong payload"),
    ]

    release = agent.ship(
        sprint_id="sprint-1",
        plan_dir=plan_dir,
        integration_issues=issues,
        clock=_now,
        author="orchestrator",
    )

    # Release row written.
    assert len(store.created_releases) == 1
    row = store.created_releases[0]
    assert row["sprint_id"] == "sprint-1"
    assert row["version"] == "2026-05-02"
    assert row["author"] == "orchestrator"
    assert row["shipped_at"] == _now()
    assert release.notes_path == row["notes_path"]

    # Markdown file landed at plan_dir/releases/<version>.md.
    notes_file = plan_dir / "releases" / "2026-05-02.md"
    assert notes_file.exists()
    assert notes_file.read_text(encoding="utf-8").startswith("# Release notes body")

    # Notes writer was called with the structured input shape.
    assert len(writer.calls) == 1
    notes_input = writer.calls[0]
    assert notes_input.version == "2026-05-02"
    assert notes_input.sprint_id == "sprint-1"
    assert len(notes_input.stories) == 1
    assert notes_input.stories[0].acceptance_criteria == ["ac-1", "ac-2"]
    assert {f.source for f in notes_input.failures} == {"integration"}

    # Two feedback items created, each tagged with the sprint, severity
    # remapped onto the feedback scale.
    assert len(store.created_feedback) == 2
    severities = sorted(fb["severity"] for fb in store.created_feedback)
    assert severities == ["high", "normal"]
    for fb in store.created_feedback:
        assert fb["sprint_id"] == "sprint-1"
        assert fb["product_id"] == "product-1"
        assert fb["source"] == "se-integration"
        assert fb["author"] == "orchestrator"
        assert fb["raw_payload"]["kind"] == "integration"


def test_ship_raises_sprint_not_complete_when_stories_open(tmp_path: Path) -> None:
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    view = _make_view([_story("story-1", status="proposed")])
    store = _StubStore(sprint_view=view, open_count=1)
    agent = ReleaseManagerAgent(store, notes_writer=_StubWriter())  # type: ignore[arg-type]

    with pytest.raises(SprintNotComplete):
        agent.ship(sprint_id="sprint-1", plan_dir=plan_dir, clock=_now)
    # Nothing landed.
    assert store.created_releases == []
    assert store.created_feedback == []
    assert not (plan_dir / "releases").exists()


def test_ship_raises_when_sprint_missing(tmp_path: Path) -> None:
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    store = _StubStore(sprint_view=None)
    agent = ReleaseManagerAgent(store, notes_writer=_StubWriter())  # type: ignore[arg-type]

    with pytest.raises(UnknownProductDeliveryEntity):
        agent.ship(sprint_id="missing", plan_dir=plan_dir, clock=_now)


def test_ship_uses_explicit_version_when_provided(tmp_path: Path) -> None:
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    view = _make_view([_story()])
    store = _StubStore(sprint_view=view, open_count=0)
    agent = ReleaseManagerAgent(store, notes_writer=_StubWriter())  # type: ignore[arg-type]

    agent.ship(
        sprint_id="sprint-1",
        plan_dir=plan_dir,
        version="v1.2.3",
        clock=_now,
        author="orchestrator",
    )
    assert store.created_releases[0]["version"] == "v1.2.3"
    assert (plan_dir / "releases" / "v1.2.3.md").exists()


def test_ship_strips_path_separators_from_explicit_version(tmp_path: Path) -> None:
    """Path separators in an explicit version are sanitized so the
    resulting notes path is always inside ``plan_dir/releases``.

    ``.`` is preserved (legitimate in semver: ``1.2.3``), so a sequence
    like ``..`` survives as a literal — but it can't traverse, because
    the version becomes the *filename* (no intervening separator).
    """
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    view = _make_view([_story()])
    store = _StubStore(sprint_view=view, open_count=0)
    agent = ReleaseManagerAgent(store, notes_writer=_StubWriter())  # type: ignore[arg-type]

    agent.ship(
        sprint_id="sprint-1",
        plan_dir=plan_dir,
        version="v1/../2",
        clock=_now,
        author="orchestrator",
    )
    rec = store.created_releases[0]
    # Path separators must be gone — the version is a filename.
    assert "/" not in rec["version"]
    assert "\\" not in rec["version"]
    # Notes file lands inside plan_dir/releases (no traversal escape).
    notes_path = Path(rec["notes_path"])
    notes_path.relative_to(plan_dir / "releases")
    assert notes_path.exists()


def test_ship_collision_appends_dash_n_suffix(tmp_path: Path) -> None:
    plan_dir = tmp_path / "plan"
    (plan_dir / "releases").mkdir(parents=True)
    # Pre-create today's notes file so the agent must bump the suffix.
    (plan_dir / "releases" / "2026-05-02.md").write_text("prior\n", encoding="utf-8")
    view = _make_view([_story()])
    store = _StubStore(sprint_view=view, open_count=0)
    agent = ReleaseManagerAgent(store, notes_writer=_StubWriter())  # type: ignore[arg-type]

    agent.ship(sprint_id="sprint-1", plan_dir=plan_dir, clock=_now, author="orchestrator")
    rec = store.created_releases[0]
    assert rec["version"] == "2026-05-02-1"
    assert (plan_dir / "releases" / "2026-05-02-1.md").exists()
    # The earlier file is untouched.
    assert (plan_dir / "releases" / "2026-05-02.md").read_text(encoding="utf-8") == "prior\n"


def test_ship_promotes_qa_and_devops_failures(tmp_path: Path) -> None:
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    view = _make_view([_story()])
    store = _StubStore(sprint_view=view, open_count=0)
    agent = ReleaseManagerAgent(store, notes_writer=_StubWriter())  # type: ignore[arg-type]

    class _Bug:
        def __init__(self) -> None:
            self.severity = "high"
            self.description = "null pointer in /api/foo"
            self.location = "backend/service.py:42"
            self.recommendation = "guard nullable input"
            self.expected_vs_actual = "expected 200; got 500"

    devops_failure = {
        "severity": "low",
        "description": "build cache miss",
        "location": "ci.yml",
        "recommendation": "warm cache nightly",
    }

    agent.ship(
        sprint_id="sprint-1",
        plan_dir=plan_dir,
        qa_failures=[_Bug()],
        devops_failures=[devops_failure],
        clock=_now,
        author="orchestrator",
    )
    sources = sorted(fb["source"] for fb in store.created_feedback)
    assert sources == ["se-devops", "se-qa"]
    qa_row = next(fb for fb in store.created_feedback if fb["source"] == "se-qa")
    assert qa_row["severity"] == "high"
    assert qa_row["raw_payload"]["expected_vs_actual"].startswith("expected")
    devops_row = next(fb for fb in store.created_feedback if fb["source"] == "se-devops")
    assert devops_row["severity"] == "normal"
    assert devops_row["raw_payload"]["kind"] == "devops"


def test_ship_proceeds_when_writer_falls_back(tmp_path: Path) -> None:
    """LLM failure → writer returns ``llm_failed=True`` markdown; we still ship."""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    view = _make_view([_story()])
    store = _StubStore(sprint_view=view, open_count=0)
    writer = _StubWriter()

    # Simulate the production writer's fallback: still returns a body.
    from software_engineering_team.technical_writers.release_notes_agent.models import (
        ReleaseNotesOutput,
    )

    def _fallback_run(input_data: Any) -> Any:
        writer.calls.append(input_data)
        return ReleaseNotesOutput(
            markdown="# Fallback body\n", summary="fallback", llm_failed=True, error="boom"
        )

    writer.run = _fallback_run  # type: ignore[assignment]
    agent = ReleaseManagerAgent(store, notes_writer=writer)  # type: ignore[arg-type]

    release = agent.ship(sprint_id="sprint-1", plan_dir=plan_dir, clock=_now, author="orchestrator")
    assert release.version == "2026-05-02"
    assert (
        (plan_dir / "releases" / "2026-05-02.md")
        .read_text(encoding="utf-8")
        .startswith("# Fallback body")
    )


def test_ship_rejects_writer_and_factory_together() -> None:
    store = _StubStore(sprint_view=None)
    with pytest.raises(ValueError, match="exactly one"):
        ReleaseManagerAgent(  # type: ignore[arg-type]
            store,
            notes_writer=_StubWriter(),
            notes_writer_factory=lambda: _StubWriter(),
        )
