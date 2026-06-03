"""Live-Postgres tests for the cognition memory store (Step 2 DAL).

Skipped automatically when ``POSTGRES_HOST`` is unset, matching the pattern
used by ``agent_console`` / ``shared_postgres`` store tests. The autouse
fixture registers the schema and truncates the cognition tables before each
test so cases are independent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from agent_cognition.memory import store
from agent_cognition.models import EventKind, MemoryEvent, PeriodSummary, Scale
from agent_cognition.postgres import SCHEMA
from shared_postgres import is_postgres_enabled, register_team_schemas
from shared_postgres.testing import truncate_team_tables

pytestmark = pytest.mark.skipif(
    not is_postgres_enabled(),
    reason="POSTGRES_HOST not set; skipping live-Postgres store tests",
)


@pytest.fixture(autouse=True)
def _provision_schema() -> None:
    register_team_schemas(SCHEMA)
    truncate_team_tables(SCHEMA)


# ---------------------------------------------------------------------------
# Fixed UTC windows. 2026-06-01 is a Monday, so the calendar day, ISO week,
# and month windows below all contain ``_MID_DAY``.
# ---------------------------------------------------------------------------
_UTC = timezone.utc
_MID_DAY = datetime(2026, 6, 1, 12, 0, tzinfo=_UTC)
_DAY = (datetime(2026, 6, 1, tzinfo=_UTC), datetime(2026, 6, 2, tzinfo=_UTC))
_WEEK = (datetime(2026, 6, 1, tzinfo=_UTC), datetime(2026, 6, 8, tzinfo=_UTC))
_MONTH = (datetime(2026, 6, 1, tzinfo=_UTC), datetime(2026, 7, 1, tzinfo=_UTC))
_CREATED = datetime(2026, 6, 2, tzinfo=_UTC)

# Far-past windows for prune tests (always older than any sane cutoff).
_OLD = datetime(2020, 1, 1, 12, 0, tzinfo=_UTC)
_OLD_DAY = (datetime(2020, 1, 1, tzinfo=_UTC), datetime(2020, 1, 2, tzinfo=_UTC))
_OLD2 = datetime(2020, 2, 1, 12, 0, tzinfo=_UTC)
_OLD2_DAY = (datetime(2020, 2, 1, tzinfo=_UTC), datetime(2020, 2, 2, tzinfo=_UTC))
_OLD3 = datetime(2020, 3, 1, 12, 0, tzinfo=_UTC)
_OLD3_DAY = (datetime(2020, 3, 1, tzinfo=_UTC), datetime(2020, 3, 2, tzinfo=_UTC))


def _event(
    agent_id: str,
    *,
    seq: int = 1,
    run_id: str | None = None,
    salience: float = 0.0,
    occurred_at: datetime = _MID_DAY,
    kind: EventKind = EventKind.OBSERVATION,
    content: str = "",
    data: dict | None = None,
) -> MemoryEvent:
    return MemoryEvent(
        id=str(uuid4()),
        agent_id=agent_id,
        kind=kind,
        content=content,
        data=data or {},
        salience=salience,
        occurred_at=occurred_at,
        source_run_id=run_id or str(uuid4()),
        source_seq=seq,
    )


def _summary(
    agent_id: str,
    *,
    scale: Scale = Scale.DAY,
    window: tuple[datetime, datetime] = _DAY,
    source_count: int = 0,
    version: int = 1,
    stale: bool = False,
    summary: str = "",
    highlights: list | None = None,
    covers_through: datetime | None = None,
) -> PeriodSummary:
    return PeriodSummary(
        id=str(uuid4()),
        agent_id=agent_id,
        scale=scale,
        period_start=window[0],
        period_end=window[1],
        summary=summary,
        highlights=highlights or [],
        source_count=source_count,
        covers_through=covers_through,
        version=version,
        stale=stale,
        created_at=_CREATED,
    )


def _all_events(agent_id: str) -> list[MemoryEvent]:
    """Every event for an agent (wide window spanning all test fixtures)."""
    return store.fetch_events_for_period(
        agent_id,
        datetime(2000, 1, 1, tzinfo=_UTC),
        datetime(2100, 1, 1, tzinfo=_UTC),
    )


# ---------------------------------------------------------------------------
# Events: round-trip + idempotency
# ---------------------------------------------------------------------------
def test_append_and_fetch_for_period_round_trip() -> None:
    ev = _event(
        "a",
        kind=EventKind.OUTCOME,
        content="shipped",
        data={"pr": 42, "nested": {"ok": True}},
        salience=0.7,
    )
    store.append_event("a", ev)

    got = store.fetch_events_for_period("a", _DAY[0], _DAY[1])
    assert len(got) == 1
    row = got[0]
    assert row.id == ev.id
    assert row.kind is EventKind.OUTCOME
    assert row.content == "shipped"
    assert row.data == {"pr": 42, "nested": {"ok": True}}
    assert row.salience == 0.7
    assert row.occurred_at == _MID_DAY


def test_fetch_events_for_period_is_half_open() -> None:
    on_start = _event("a", occurred_at=_DAY[0])
    on_end = _event("a", occurred_at=_DAY[1])
    store.append_event("a", on_start)
    store.append_event("a", on_end)

    got = store.fetch_events_for_period("a", _DAY[0], _DAY[1])
    # start is inclusive, end is exclusive
    assert [e.id for e in got] == [on_start.id]


def test_append_event_idempotent_on_writeback_key() -> None:
    run_id = "run-1"
    store.append_event("a", _event("a", run_id=run_id, seq=3, content="first"))
    # Same (agent_id, source_run_id, source_seq), different content/id → no-op.
    store.append_event("a", _event("a", run_id=run_id, seq=3, content="second"))

    events = _all_events("a")
    assert len(events) == 1
    assert events[0].content == "first"


def test_append_event_agent_id_mismatch_raises() -> None:
    with pytest.raises(AssertionError):
        store.append_event("a", _event("b"))


# ---------------------------------------------------------------------------
# fetch_recent_events ordering + limits
# ---------------------------------------------------------------------------
def test_fetch_recent_events_by_salience() -> None:
    low = _event("a", run_id="r1", salience=0.1, occurred_at=_MID_DAY)
    high = _event(
        "a",
        run_id="r2",
        salience=0.9,
        occurred_at=datetime(2026, 6, 1, 6, tzinfo=_UTC),
    )
    store.append_event("a", low)
    store.append_event("a", high)

    ordered = store.fetch_recent_events("a", top_n=10, by_salience=True)
    assert [e.id for e in ordered] == [high.id, low.id]


def test_fetch_recent_events_by_recency() -> None:
    older = _event("a", run_id="r1", salience=0.9, occurred_at=datetime(2026, 6, 1, 6, tzinfo=_UTC))
    newer = _event("a", run_id="r2", salience=0.1, occurred_at=_MID_DAY)
    store.append_event("a", older)
    store.append_event("a", newer)

    ordered = store.fetch_recent_events("a", top_n=10, by_salience=False)
    assert [e.id for e in ordered] == [newer.id, older.id]


def test_fetch_recent_events_respects_top_n() -> None:
    for i in range(5):
        store.append_event("a", _event("a", run_id=f"r{i}", seq=i, salience=i / 10))
    assert len(store.fetch_recent_events("a", top_n=2)) == 2


def test_fetch_recent_events_top_n_zero_is_empty() -> None:
    store.append_event("a", _event("a"))
    assert store.fetch_recent_events("a", top_n=0) == []


def test_fetch_recent_events_negative_top_n_raises() -> None:
    with pytest.raises(AssertionError):
        store.fetch_recent_events("a", top_n=-1)


# ---------------------------------------------------------------------------
# Summaries: upsert idempotency + reads
# ---------------------------------------------------------------------------
def test_upsert_summary_insert_then_update_idempotent() -> None:
    s = _summary("a", source_count=3, summary="v1")
    store.upsert_summary("a", s)

    # Same (agent_id, scale, period_start) → update in place, not a new row.
    updated = _summary("a", window=_DAY, source_count=9, version=2, stale=True, summary="v2")
    store.upsert_summary("a", updated)

    rows = store.fetch_summaries("a", Scale.DAY)
    assert len(rows) == 1
    row = rows[0]
    assert row.summary == "v2"
    assert row.source_count == 9
    assert row.version == 2
    assert row.stale is True
    # id / created_at of the original row are preserved on update.
    assert row.id == s.id
    assert row.created_at == _CREATED


def test_upsert_summary_agent_id_mismatch_raises() -> None:
    with pytest.raises(AssertionError):
        store.upsert_summary("a", _summary("b"))


def test_fetch_summaries_filters_scale_and_paginates() -> None:
    months = [
        _summary(
            "a",
            scale=Scale.MONTH,
            window=(datetime(2026, m, 1, tzinfo=_UTC), datetime(2026, m + 1, 1, tzinfo=_UTC)),
        )
        for m in (1, 2, 3)
    ]
    for m in months:
        store.upsert_summary("a", m)
    store.upsert_summary("a", _summary("a", scale=Scale.DAY))

    month_rows = store.fetch_summaries("a", Scale.MONTH)
    assert [r.period_start.month for r in month_rows] == [3, 2, 1]  # newest first
    assert store.fetch_summaries("a", Scale.DAY) and len(month_rows) == 3

    page = store.fetch_summaries("a", Scale.MONTH, limit=1, offset=1)
    assert [r.period_start.month for r in page] == [2]


def test_fetch_summaries_negative_offset_raises() -> None:
    with pytest.raises(AssertionError):
        store.fetch_summaries("a", Scale.DAY, offset=-1)


def test_fetch_summaries_negative_limit_raises() -> None:
    with pytest.raises(AssertionError):
        store.fetch_summaries("a", Scale.DAY, limit=-1)


def test_get_last_summary_returns_newest_or_none() -> None:
    assert store.get_last_summary("a", Scale.DAY) is None

    store.upsert_summary(
        "a",
        _summary(
            "a", window=(datetime(2026, 6, 1, tzinfo=_UTC), datetime(2026, 6, 2, tzinfo=_UTC))
        ),
    )
    store.upsert_summary(
        "a",
        _summary(
            "a",
            window=(datetime(2026, 6, 3, tzinfo=_UTC), datetime(2026, 6, 4, tzinfo=_UTC)),
            summary="newest",
        ),
    )
    last = store.get_last_summary("a", Scale.DAY)
    assert last is not None
    assert last.summary == "newest"


# ---------------------------------------------------------------------------
# mark_period_stale
# ---------------------------------------------------------------------------
def test_mark_period_stale_flags_containing_summaries_retained() -> None:
    # Day summary folded 2 events and both originals are still present.
    # mark_period_stale is called *before* the late event is appended, so the
    # retained-count reflects the surviving originals only.
    store.upsert_summary("a", _summary("a", scale=Scale.DAY, window=_DAY, source_count=2))
    store.upsert_summary("a", _summary("a", scale=Scale.WEEK, window=_WEEK))
    store.upsert_summary("a", _summary("a", scale=Scale.MONTH, window=_MONTH))
    store.append_event("a", _event("a", run_id="r1", seq=1, occurred_at=_MID_DAY))
    store.append_event("a", _event("a", run_id="r2", seq=2, occurred_at=_MID_DAY))

    retained = store.mark_period_stale("a", _MID_DAY)
    assert retained is True

    for scale in (Scale.DAY, Scale.WEEK, Scale.MONTH):
        s = store.get_last_summary("a", scale)
        assert s is not None and s.stale is True
        assert s.version == 2  # bumped from 1


def test_mark_period_stale_pruned_period_returns_false() -> None:
    # Regression guard for the late-event-inflation bug: a partially-pruned
    # day folded 2 events, one was pruned, one original survives. The mark
    # runs before the late event is appended, so the count is 1 < 2 → pruned.
    # (Had we counted the in-flight late event, the count would be 2 == 2 and
    # this would wrongly report the period as retained.)
    store.upsert_summary("a", _summary("a", scale=Scale.DAY, window=_DAY, source_count=2))
    store.append_event("a", _event("a", run_id="survivor", occurred_at=_MID_DAY))

    assert store.mark_period_stale("a", _MID_DAY) is False


def test_mark_period_stale_no_summary_returns_true_and_flags_nothing() -> None:
    # No summary covers the timestamp → never summarized → trivially retained.
    assert store.mark_period_stale("a", _MID_DAY) is True
    assert store.fetch_summaries("a", Scale.DAY) == []


# ---------------------------------------------------------------------------
# prune_events
# ---------------------------------------------------------------------------
def test_prune_events_deletes_only_summarized_nonstale_past_cutoff() -> None:
    # A: old, day summary exists non-stale → deleted.
    a = _event("a", run_id="a", occurred_at=_OLD)
    store.upsert_summary("a", _summary("a", scale=Scale.DAY, window=_OLD_DAY, source_count=1))
    # B: old, no day summary → kept.
    b = _event("a", run_id="b", occurred_at=_OLD2)
    # C: old, day summary is stale → kept.
    c = _event("a", run_id="c", occurred_at=_OLD3)
    store.upsert_summary(
        "a", _summary("a", scale=Scale.DAY, window=_OLD3_DAY, source_count=1, stale=True)
    )
    # D: recent (newer than cutoff), summary exists → kept.
    d = _event("a", run_id="d", occurred_at=_MID_DAY)
    store.upsert_summary("a", _summary("a", scale=Scale.DAY, window=_DAY, source_count=1))
    for ev in (a, b, c, d):
        store.append_event("a", ev)

    deleted = store.prune_events("a", retention_days=30)
    assert deleted == 1
    remaining = {e.id for e in _all_events("a")}
    assert remaining == {b.id, c.id, d.id}


def test_prune_events_negative_retention_raises() -> None:
    with pytest.raises(AssertionError):
        store.prune_events("a", retention_days=-1)


# ---------------------------------------------------------------------------
# Cross-agent isolation
# ---------------------------------------------------------------------------
def test_cross_agent_isolation() -> None:
    a_ev = _event("a", run_id="shared", seq=1, occurred_at=_MID_DAY)
    b_ev = _event("b", run_id="shared", seq=1, occurred_at=_OLD)
    store.append_event("a", a_ev)
    store.append_event("b", b_ev)
    store.upsert_summary("a", _summary("a", scale=Scale.DAY, window=_DAY))
    store.upsert_summary("b", _summary("b", scale=Scale.DAY, window=_OLD_DAY, source_count=1))

    # Readers never cross agents.
    assert [e.id for e in _all_events("a")] == [a_ev.id]
    assert [e.id for e in store.fetch_recent_events("a", top_n=10)] == [a_ev.id]
    assert [s.agent_id for s in store.fetch_summaries("a", Scale.DAY)] == ["a"]

    # Marking a's period stale leaves b's summary untouched.
    store.mark_period_stale("a", _MID_DAY)
    b_summary = store.get_last_summary("b", Scale.DAY)
    assert b_summary is not None and b_summary.stale is False and b_summary.version == 1

    # Pruning a does not touch b's (prunable) old event.
    store.prune_events("a", retention_days=30)
    assert [e.id for e in _all_events("b")] == [b_ev.id]


# ---------------------------------------------------------------------------
# Storage-unavailable guard
# ---------------------------------------------------------------------------
def test_storage_unavailable_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "is_postgres_enabled", lambda: False)
    with pytest.raises(store.AgentCognitionStorageUnavailable):
        store.fetch_recent_events("a", top_n=5)
