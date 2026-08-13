"""Live-Postgres tests for the cognition memory store (Step 2 DAL).

Skipped automatically when ``POSTGRES_HOST`` is unset, matching the pattern
used by ``agent_platform.console`` / ``shared.postgres`` store tests. The autouse
fixture registers the schema and truncates the cognition tables before each
test so cases are independent.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from agent_cognition.memory import store
from agent_cognition.models import (
    EventKind,
    MemoryEvent,
    PeriodSummary,
    ProposalAction,
    ProposalStatus,
    Rule,
    RuleMode,
    RuleProposal,
    RuleSource,
    RuleStatus,
    Scale,
)
from agent_cognition.postgres import SCHEMA
from agent_cognition.rules import store as rules_store
from shared.postgres import is_postgres_enabled, register_team_schemas
from shared.postgres.testing import truncate_team_tables

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

# Rollup snapshot times for prune tests. Events are appended in real time, so
# their store-set ``recorded_at`` is ~now (2026). _FOLDED_AT (far future) marks
# a summary computed *after* those events arrived (they count as folded, so
# prunable); _PRECOMPUTED_AT (far past) marks a summary computed *before* the
# events arrived (not yet folded, so prune must leave them).
_FOLDED_AT = datetime(2099, 1, 1, tzinfo=_UTC)
_PRECOMPUTED_AT = datetime(2000, 1, 1, tzinfo=_UTC)

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


def _upsert(agent_id: str, summary: PeriodSummary, *, computed_at: datetime = _FOLDED_AT) -> None:
    """upsert_summary with a required computed_at, defaulting to "folded".

    Most tests don't prune, so the default _FOLDED_AT (a snapshot after every
    real append) marks events as folded; prune tests pass an explicit snapshot.
    """
    store.upsert_summary(agent_id, summary, computed_at=computed_at)


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


def test_fetch_recent_events_since_lower_bound() -> None:
    # ``since`` excludes events before the bound even when they outrank by salience.
    before = _event("a", run_id="r1", salience=0.9, occurred_at=datetime(2026, 5, 1, tzinfo=_UTC))
    after = _event("a", run_id="r2", salience=0.1, occurred_at=datetime(2026, 6, 5, tzinfo=_UTC))
    store.append_event("a", before)
    store.append_event("a", after)

    got = store.fetch_recent_events("a", top_n=10, since=datetime(2026, 6, 1, tzinfo=_UTC))
    assert [e.id for e in got] == [after.id]  # only occurred_at >= since


# ---------------------------------------------------------------------------
# fetch_recent_unfolded_events: bounded late-event scan across stale periods
# ---------------------------------------------------------------------------
def test_fetch_recent_unfolded_events_returns_late_rows_ranked() -> None:
    # Stale day summary computed in the far past → every appended event (recorded
    # ~now) arrived after the fold point, so all are unfolded late rows, salience DESC.
    _upsert(
        "a", _summary("a", scale=Scale.DAY, window=_DAY, stale=True), computed_at=_PRECOMPUTED_AT
    )
    lo = _event("a", run_id="r1", salience=0.2, occurred_at=_MID_DAY)
    hi = _event("a", run_id="r2", salience=0.8, occurred_at=datetime(2026, 6, 1, 6, tzinfo=_UTC))
    store.append_event("a", lo)
    store.append_event("a", hi)

    got = store.fetch_recent_unfolded_events("a", Scale.DAY, top_n=10, snapshot=_FOLDED_AT)
    assert [e.id for e in got] == [hi.id, lo.id]


def test_fetch_recent_unfolded_events_respects_top_n() -> None:
    _upsert(
        "a", _summary("a", scale=Scale.DAY, window=_DAY, stale=True), computed_at=_PRECOMPUTED_AT
    )
    for i in range(4):
        store.append_event("a", _event("a", run_id=f"r{i}", seq=i, salience=i / 10))
    assert (
        len(store.fetch_recent_unfolded_events("a", Scale.DAY, top_n=2, snapshot=_FOLDED_AT)) == 2
    )


def test_fetch_recent_unfolded_events_excludes_non_stale_period() -> None:
    # A non-stale day's events are reconciled, not "late" — never surfaced here.
    _upsert(
        "a", _summary("a", scale=Scale.DAY, window=_DAY, stale=False), computed_at=_PRECOMPUTED_AT
    )
    store.append_event("a", _event("a", occurred_at=_MID_DAY))
    assert store.fetch_recent_unfolded_events("a", Scale.DAY, top_n=10, snapshot=_FOLDED_AT) == []


def test_fetch_recent_unfolded_events_excludes_already_folded() -> None:
    # Stale day, but the fold point is after every event's arrival → already folded.
    _upsert("a", _summary("a", scale=Scale.DAY, window=_DAY, stale=True), computed_at=_FOLDED_AT)
    store.append_event("a", _event("a", occurred_at=_MID_DAY))
    assert store.fetch_recent_unfolded_events("a", Scale.DAY, top_n=10, snapshot=_FOLDED_AT) == []


def test_fetch_recent_unfolded_events_honours_snapshot_upper_bound() -> None:
    # Events recorded after the snapshot are not yet visible to this read.
    _upsert(
        "a", _summary("a", scale=Scale.DAY, window=_DAY, stale=True), computed_at=_PRECOMPUTED_AT
    )
    store.append_event("a", _event("a", occurred_at=_MID_DAY))  # recorded ~now
    assert (
        store.fetch_recent_unfolded_events("a", Scale.DAY, top_n=10, snapshot=_PRECOMPUTED_AT) == []
    )


def test_fetch_recent_unfolded_events_agent_isolation() -> None:
    _upsert(
        "a", _summary("a", scale=Scale.DAY, window=_DAY, stale=True), computed_at=_PRECOMPUTED_AT
    )
    _upsert(
        "b", _summary("b", scale=Scale.DAY, window=_DAY, stale=True), computed_at=_PRECOMPUTED_AT
    )
    store.append_event("a", _event("a", occurred_at=_MID_DAY))
    store.append_event("b", _event("b", occurred_at=_MID_DAY))

    got = store.fetch_recent_unfolded_events("a", Scale.DAY, top_n=10, snapshot=_FOLDED_AT)
    assert all(e.agent_id == "a" for e in got)
    assert len(got) == 1


def test_fetch_recent_unfolded_events_negative_top_n_raises() -> None:
    with pytest.raises(AssertionError):
        store.fetch_recent_unfolded_events("a", Scale.DAY, top_n=-1, snapshot=_FOLDED_AT)


# ---------------------------------------------------------------------------
# Summaries: upsert idempotency + reads
# ---------------------------------------------------------------------------
def test_upsert_summary_insert_then_update_idempotent() -> None:
    s = _summary("a", source_count=3, summary="v1")
    _upsert("a", s)

    # Same (agent_id, scale, period_start) → update in place, not a new row.
    updated = _summary("a", window=_DAY, source_count=9, version=2, stale=True, summary="v2")
    _upsert("a", updated)

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


def test_upsert_summary_does_not_regress_version() -> None:
    # mark_period_stale (or a prior recompute) can leave version ahead of a
    # freshly-built summary; the GREATEST guard keeps the higher stored value.
    _upsert("a", _summary("a", window=_DAY, version=5, summary="v1"))
    _upsert("a", _summary("a", window=_DAY, version=1, summary="v2"))

    row = store.get_last_summary("a", Scale.DAY)
    assert row is not None
    assert row.summary == "v2"
    assert row.version == 5  # not regressed to 1


def test_upsert_summary_agent_id_mismatch_raises() -> None:
    with pytest.raises(AssertionError):
        _upsert("a", _summary("b"))


def test_upsert_summary_requires_computed_at() -> None:
    # computed_at is a required keyword arg (the rollup's input-read snapshot),
    # so the unsafe "default to write time" footgun can't happen.
    with pytest.raises(TypeError):
        store.upsert_summary("a", _summary("a"))


def test_upsert_summary_preserves_concurrently_set_stale() -> None:
    # Lost-update race: a rollup reads the period (snapshot T0), a late event
    # then marks it stale (stale_since = now ≫ T0), and the slow rollup's write
    # (computed_at = T0) must NOT clear that stale flag — else the late event
    # is never folded.
    _upsert("a", _summary("a", window=_DAY, version=1), computed_at=_PRECOMPUTED_AT)
    store.mark_period_stale("a", _MID_DAY)
    assert store.get_last_summary("a", Scale.DAY).stale is True

    # Slow rollup that read at T0 finishes and writes a non-stale recompute.
    _upsert(
        "a",
        _summary("a", window=_DAY, version=2, stale=False, summary="stale-read"),
        computed_at=_PRECOMPUTED_AT,
    )
    kept = store.get_last_summary("a", Scale.DAY)
    assert kept.stale is True  # stale_since (now) > computed_at (T0) → preserved
    assert kept.summary == "stale-read"  # content still updated

    # A rollup that read *after* the staleness (snapshot in the far future)
    # legitimately clears the flag.
    _upsert(
        "a",
        _summary("a", window=_DAY, version=3, stale=False, summary="fresh"),
        computed_at=_FOLDED_AT,
    )
    assert store.get_last_summary("a", Scale.DAY).stale is False


def test_upsert_summary_older_snapshot_is_skipped() -> None:
    # Out-of-order rollups: a fresher snapshot (T2) writes, then a staler one
    # (T1 < T2) commits last. The older write must be ignored wholesale — it
    # can't regress content, computed_at, version, or stale.
    _upsert("a", _summary("a", window=_DAY, version=1, summary="t1"), computed_at=_PRECOMPUTED_AT)
    _upsert("a", _summary("a", window=_DAY, version=2, summary="t2"), computed_at=_FOLDED_AT)
    assert store.get_last_summary("a", Scale.DAY).summary == "t2"

    # Staler rollup (snapshot T1) committing last is a no-op on conflict.
    _upsert(
        "a",
        _summary("a", window=_DAY, version=99, summary="t1-late"),
        computed_at=_PRECOMPUTED_AT,
    )
    row = store.get_last_summary("a", Scale.DAY)
    assert row.summary == "t2"  # not clobbered by the older snapshot
    assert row.version == 2  # skipped update never reached the GREATEST bump


def test_upsert_summary_updated_at_advances_only_on_accepted_update() -> None:
    # updated_at is the knowledge-graph keyset cursor: an accepted update must
    # advance it (so a recompute is re-ingested), a superseded (staler
    # computed_at) update must leave it untouched (so a no-op is not re-ingested).
    def _updated_at() -> datetime:
        rows = store.fetch_summaries_updated_after(
            "a", after_updated_at=None, after_id=None, limit=50
        )
        assert len(rows) == 1
        return rows[0].updated_at

    _upsert("a", _summary("a", window=_DAY, version=1, summary="v1"), computed_at=_PRECOMPUTED_AT)
    first = _updated_at()

    # Accepted update (fresher computed_at) advances updated_at.
    _upsert("a", _summary("a", window=_DAY, version=2, summary="v2"), computed_at=_FOLDED_AT)
    advanced = _updated_at()
    assert advanced > first

    # Superseded update (staler computed_at) is skipped wholesale → updated_at frozen.
    _upsert(
        "a", _summary("a", window=_DAY, version=99, summary="stale"), computed_at=_PRECOMPUTED_AT
    )
    assert _updated_at() == advanced


# ---------------------------------------------------------------------------
# fetch_summaries_updated_after — the knowledge-graph keyset drain. Keysets on
# (updated_at, id) so a recomputed (version-advanced) summary re-sorts after the
# cursor and is re-ingested, while a stable summary is drained exactly once.
# ---------------------------------------------------------------------------
def test_fetch_summaries_updated_after_cold_start_keyset_ordered() -> None:
    _upsert("a", _summary("a", scale=Scale.DAY, window=_DAY))
    _upsert("a", _summary("a", scale=Scale.WEEK, window=_WEEK))
    rows = store.fetch_summaries_updated_after("a", after_updated_at=None, after_id=None, limit=50)
    assert len(rows) == 2
    assert all(isinstance(r, store.RecordedSummary) for r in rows)
    # Each row rides its updated_at; the scan is non-decreasing in (updated_at, id).
    keys = [(r.updated_at, r.summary.id) for r in rows]
    assert keys == sorted(keys)


def test_fetch_summaries_updated_after_excludes_cursor_row() -> None:
    _upsert("a", _summary("a", scale=Scale.DAY, window=_DAY))
    _upsert("a", _summary("a", scale=Scale.WEEK, window=_WEEK))
    first = store.fetch_summaries_updated_after("a", after_updated_at=None, after_id=None, limit=1)
    assert len(first) == 1
    cur = first[0]
    rest = store.fetch_summaries_updated_after(
        "a", after_updated_at=cur.updated_at, after_id=cur.summary.id, limit=50
    )
    assert len(rest) == 1
    assert rest[0].summary.id != cur.summary.id


def test_fetch_summaries_updated_after_repickups_recomputed_summary() -> None:
    # Drain a summary to its cursor; recompute it (version advances, updated_at
    # moves) → it re-sorts strictly after the cursor and is re-fetched.
    _upsert("a", _summary("a", window=_DAY, version=1, summary="v1"))
    drained = store.fetch_summaries_updated_after(
        "a", after_updated_at=None, after_id=None, limit=50
    )
    assert len(drained) == 1
    cur = drained[0]
    # Nothing new strictly after the cursor yet.
    assert (
        store.fetch_summaries_updated_after(
            "a", after_updated_at=cur.updated_at, after_id=cur.summary.id, limit=50
        )
        == []
    )
    # A late event bumps version; the rollup recompute then writes fresh content.
    store.mark_period_stale("a", _MID_DAY)
    _upsert("a", _summary("a", window=_DAY, version=2, summary="v2"))
    again = store.fetch_summaries_updated_after(
        "a", after_updated_at=cur.updated_at, after_id=cur.summary.id, limit=50
    )
    # Re-pickup with the same id past the cursor proves updated_at advanced: the
    # keyset (updated_at, id) > (cur.updated_at, cur.id) with an equal id can only
    # hold when updated_at strictly advanced — no separate timing assertion needed.
    assert len(again) == 1
    assert again[0].summary.id == cur.summary.id  # same row, new version
    assert again[0].summary.version == 2
    assert again[0].summary.summary == "v2"


def test_fetch_summaries_updated_after_stable_summary_drained_once() -> None:
    # A summary that is never recomputed keeps its updated_at, so it is not
    # returned again after the cursor advances past it (no wasted re-ingestion).
    _upsert("a", _summary("a", window=_DAY, version=1))
    drained = store.fetch_summaries_updated_after(
        "a", after_updated_at=None, after_id=None, limit=50
    )
    cur = drained[0]
    assert (
        store.fetch_summaries_updated_after(
            "a", after_updated_at=cur.updated_at, after_id=cur.summary.id, limit=50
        )
        == []
    )


def test_fetch_summaries_updated_after_agent_isolation() -> None:
    _upsert("a", _summary("a", window=_DAY))
    _upsert("b", _summary("b", window=_DAY))
    rows = store.fetch_summaries_updated_after("a", after_updated_at=None, after_id=None, limit=50)
    assert len(rows) == 1
    assert all(r.summary.agent_id == "a" for r in rows)


def test_fetch_summaries_updated_after_precondition_asserts() -> None:
    with pytest.raises(AssertionError):
        store.fetch_summaries_updated_after("", after_updated_at=None, after_id=None, limit=50)
    with pytest.raises(AssertionError):
        store.fetch_summaries_updated_after("a", after_updated_at=None, after_id=None, limit=0)
    with pytest.raises(AssertionError):
        # Half-set cursor (updated_at without id) is a caller bug.
        store.fetch_summaries_updated_after("a", after_updated_at=_MID_DAY, after_id=None, limit=5)


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
        _upsert("a", m)
    _upsert("a", _summary("a", scale=Scale.DAY))

    month_rows = store.fetch_summaries("a", Scale.MONTH)
    assert [r.period_start.month for r in month_rows] == [3, 2, 1]  # newest first
    assert store.fetch_summaries("a", Scale.DAY) and len(month_rows) == 3

    page = store.fetch_summaries("a", Scale.MONTH, limit=1, offset=1)
    assert [r.period_start.month for r in page] == [2]

    # offset without limit must still skip rows (not silently return page one).
    offset_only = store.fetch_summaries("a", Scale.MONTH, offset=1)
    assert [r.period_start.month for r in offset_only] == [2, 1]


def test_fetch_summaries_exclude_stale_filters_before_limit() -> None:
    windows = {
        m: (datetime(2026, m, 1, tzinfo=_UTC), datetime(2026, m + 1, 1, tzinfo=_UTC))
        for m in (1, 2, 3)
    }
    _upsert("a", _summary("a", scale=Scale.MONTH, window=windows[1], stale=False))
    _upsert("a", _summary("a", scale=Scale.MONTH, window=windows[2], stale=False))
    _upsert("a", _summary("a", scale=Scale.MONTH, window=windows[3], stale=True))  # newest, stale

    # Default returns every row, stale included.
    assert [r.period_start.month for r in store.fetch_summaries("a", Scale.MONTH)] == [3, 2, 1]
    # exclude_stale drops the stale newest row.
    fresh = store.fetch_summaries("a", Scale.MONTH, exclude_stale=True)
    assert [r.period_start.month for r in fresh] == [2, 1]
    # The filter is applied BEFORE the limit: a limit of 2 returns two *fresh*
    # rows even though the single newest period is stale (no starvation).
    limited = store.fetch_summaries("a", Scale.MONTH, limit=2, exclude_stale=True)
    assert [r.period_start.month for r in limited] == [2, 1]


def test_fetch_summaries_negative_offset_raises() -> None:
    with pytest.raises(AssertionError):
        store.fetch_summaries("a", Scale.DAY, offset=-1)


def test_fetch_summaries_negative_limit_raises() -> None:
    with pytest.raises(AssertionError):
        store.fetch_summaries("a", Scale.DAY, limit=-1)


def test_get_last_summary_returns_newest_or_none() -> None:
    assert store.get_last_summary("a", Scale.DAY) is None

    _upsert(
        "a",
        _summary(
            "a", window=(datetime(2026, 6, 1, tzinfo=_UTC), datetime(2026, 6, 2, tzinfo=_UTC))
        ),
    )
    _upsert(
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
    # Day/week/month summaries exist and the day was never pruned → retained.
    _upsert("a", _summary("a", scale=Scale.DAY, window=_DAY))
    _upsert("a", _summary("a", scale=Scale.WEEK, window=_WEEK))
    _upsert("a", _summary("a", scale=Scale.MONTH, window=_MONTH))

    assert store.mark_period_stale("a", _MID_DAY) is True

    for scale in (Scale.DAY, Scale.WEEK, Scale.MONTH):
        s = store.get_last_summary("a", scale)
        assert s is not None and s.stale is True
        assert s.version == 2  # bumped from 1


def test_mark_period_stale_is_idempotent_on_version() -> None:
    # The non-stale → stale latch means a second late event into the same
    # already-stale period must not re-bump version (which would spuriously
    # move the (summary_id, version) evidence refs).
    _upsert("a", _summary("a", scale=Scale.DAY, window=_DAY))

    assert store.mark_period_stale("a", _MID_DAY) is True
    first = store.get_last_summary("a", Scale.DAY)
    assert first is not None and first.stale is True and first.version == 2

    assert store.mark_period_stale("a", _MID_DAY) is True
    second = store.get_last_summary("a", Scale.DAY)
    assert second is not None and second.version == 2  # not bumped again


def test_mark_period_stale_pruned_period_returns_false() -> None:
    # A real prune latches events_pruned on the day summary; a later late event
    # into that (now event-less) day must amend, not recompute. The regime is
    # read from the durable flag, so it is correct regardless of whether the
    # late event has been appended. computed_at=_FOLDED_AT marks the original
    # event as folded so prune is allowed to delete it.
    store.append_event("a", _event("a", occurred_at=_OLD))
    _upsert(
        "a", _summary("a", scale=Scale.DAY, window=_OLD_DAY, source_count=1), computed_at=_FOLDED_AT
    )
    assert store.prune_events("a", retention_days=30) == 1

    assert store.mark_period_stale("a", _OLD) is False


def test_mark_period_stale_no_summary_returns_true_and_flags_nothing() -> None:
    # No summary covers the timestamp → never summarized → trivially retained.
    assert store.mark_period_stale("a", _MID_DAY) is True
    assert store.fetch_summaries("a", Scale.DAY) == []


# ---------------------------------------------------------------------------
# prune_events
# ---------------------------------------------------------------------------
def test_prune_events_deletes_only_summarized_nonstale_past_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Freeze the prune clock so the 30-day retention cutoff is deterministic:
    # event ``d`` at _MID_DAY must stay *newer* than the cutoff regardless of the
    # real wall-clock date. Pinned to 15 days after _MID_DAY (cutoff = _MID_DAY −
    # 15d), keeping ``d`` inside the window while the 2020 events fall well past
    # it. Without this the test drifts and fails once real time crosses
    # _MID_DAY + 30 days.
    monkeypatch.setattr(store, "_now", lambda: _MID_DAY + timedelta(days=15))

    # Append all events first, then compute their day summaries (computed_at in
    # the future ⇒ every event is folded), so prune is purely gated on the
    # summary state below.
    a = _event("a", run_id="a", occurred_at=_OLD)  # day summary non-stale → deleted
    b = _event("a", run_id="b", occurred_at=_OLD2)  # no day summary → kept
    c = _event("a", run_id="c", occurred_at=_OLD3)  # day summary stale → kept
    d = _event("a", run_id="d", occurred_at=_MID_DAY)  # newer than cutoff → kept
    for ev in (a, b, c, d):
        store.append_event("a", ev)
    _upsert(
        "a", _summary("a", scale=Scale.DAY, window=_OLD_DAY, source_count=1), computed_at=_FOLDED_AT
    )
    _upsert(
        "a",
        _summary("a", scale=Scale.DAY, window=_OLD3_DAY, source_count=1, stale=True),
        computed_at=_FOLDED_AT,
    )
    _upsert(
        "a", _summary("a", scale=Scale.DAY, window=_DAY, source_count=1), computed_at=_FOLDED_AT
    )

    deleted = store.prune_events("a", retention_days=30)
    assert deleted == 1
    remaining = {e.id for e in _all_events("a")}
    assert remaining == {b.id, c.id, d.id}


def test_pruned_regime_visible_on_summary_reads() -> None:
    # After a prune latches events_pruned, a reader that rediscovers the stale
    # summary (e.g. the rollup after a restart) must see the regime on the row
    # itself — not only via mark_period_stale's return.
    store.append_event("a", _event("a", occurred_at=_OLD))
    _upsert("a", _summary("a", scale=Scale.DAY, window=_OLD_DAY, source_count=1))
    assert store.prune_events("a", retention_days=30) == 1

    last = store.get_last_summary("a", Scale.DAY)
    assert last is not None and last.events_pruned is True
    assert store.fetch_summaries("a", Scale.DAY)[0].events_pruned is True

    # A non-pruned summary reads back events_pruned == False.
    _upsert("b", _summary("b", scale=Scale.DAY, window=_DAY))
    assert store.get_last_summary("b", Scale.DAY).events_pruned is False


def test_fetch_events_for_period_snapshot_bound() -> None:
    # The snapshot bound excludes events recorded after the rollup's read time,
    # keeping the fold-input read consistent with the prune recorded/computed
    # comparison.
    ev = _event("a", occurred_at=_MID_DAY)
    store.append_event("a", ev)

    # Snapshot before the append → event excluded.
    assert store.fetch_events_for_period("a", _DAY[0], _DAY[1], snapshot=_PRECOMPUTED_AT) == []
    # Snapshot after the append → event included.
    got = store.fetch_events_for_period("a", _DAY[0], _DAY[1], snapshot=_FOLDED_AT)
    assert [e.id for e in got] == [ev.id]
    # No snapshot → plain window read still returns it.
    assert [e.id for e in store.fetch_events_for_period("a", _DAY[0], _DAY[1])] == [ev.id]


def test_prune_skips_late_event_not_yet_folded() -> None:
    # Race guard: the day summary was computed BEFORE the late event arrived
    # (computed_at < recorded_at), so the event isn't folded yet. Even though it
    # is old and the day summary is non-stale, prune must leave it — otherwise
    # it would be lost before the rollup could amend it in.
    _upsert(
        "a",
        _summary("a", scale=Scale.DAY, window=_OLD_DAY, source_count=1),
        computed_at=_PRECOMPUTED_AT,
    )
    late = _event("a", occurred_at=_OLD)  # recorded_at = now ≫ _PRECOMPUTED_AT
    store.append_event("a", late)

    assert store.prune_events("a", retention_days=30) == 0
    assert [e.id for e in _all_events("a")] == [late.id]
    # …and the regime stays "retained" since nothing was actually pruned.
    assert store.mark_period_stale("a", _OLD) is True


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
    _upsert("a", _summary("a", scale=Scale.DAY, window=_DAY))
    _upsert("b", _summary("b", scale=Scale.DAY, window=_OLD_DAY, source_count=1))

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
# Preconditions
# ---------------------------------------------------------------------------
def test_empty_agent_id_raises() -> None:
    with pytest.raises(AssertionError):
        store.append_event("", _event("", run_id="r"))
    with pytest.raises(AssertionError):
        store.fetch_events_for_period("", _DAY[0], _DAY[1])
    with pytest.raises(AssertionError):
        store.fetch_recent_events("", top_n=5)
    with pytest.raises(AssertionError):
        _upsert("", _summary(""))
    with pytest.raises(AssertionError):
        store.fetch_summaries("", Scale.DAY)
    with pytest.raises(AssertionError):
        store.get_last_summary("", Scale.DAY)
    with pytest.raises(AssertionError):
        store.mark_period_stale("", _MID_DAY)
    with pytest.raises(AssertionError):
        store.prune_events("", retention_days=30)


# ---------------------------------------------------------------------------
# Storage-unavailable guard
# ---------------------------------------------------------------------------
def test_storage_unavailable_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "is_postgres_enabled", lambda: False)
    with pytest.raises(store.AgentCognitionStorageUnavailable):
        store.fetch_recent_events("a", top_n=5)


def test_conn_propagates_body_errors_unwrapped() -> None:
    # An error raised inside the connection body must propagate unchanged
    # (and roll back) — it must NOT be masked as a storage-unavailable error,
    # which would hide genuine query bugs as infra outages.
    with pytest.raises(ValueError):
        with store._conn() as conn:
            assert conn is not None
            raise ValueError("boom")


# ---------------------------------------------------------------------------
# Version-staleness tolerates the non-load-bearing graph-provenance evidence
# entry that graph-grounded reflection appends (no summary_id/version keys).
# ---------------------------------------------------------------------------
_GRAPH_EVIDENCE = {"source": "graph", "kind": "grounding", "facts": 3}


def test_flag_stale_proposals_ignores_graph_provenance_entry() -> None:
    proposal = RuleProposal(
        id=str(uuid4()),
        agent_id="a",
        action=ProposalAction.ADD,
        proposed_rule={"text": "derived", "mode": "advisory", "source": "derived", "priority": 0},
        evidence=[{"summary_id": "s1", "version": 1}, _GRAPH_EVIDENCE],
        status=ProposalStatus.PENDING,
        created_at=_CREATED,
    )
    rules_store.create_proposal("a", proposal)

    # Flags on the summary ref; the graph entry (no summary_id/version) is skipped,
    # not cast-errored.
    assert store.flag_stale_proposals("a", "s1", 2) == 1
    got = rules_store.get_proposal("a", proposal.id)
    assert got is not None and got.stale_evidence is True


def test_flag_rules_needing_review_ignores_graph_provenance_entry() -> None:
    # An approved graph-grounded proposal yields an active rule that inherited the
    # graph-provenance entry; flag_rules_needing_review must still surface it on a
    # version bump and ignore the extra shape.
    rule = Rule(
        id=str(uuid4()),
        agent_id="a",
        text="derived",
        mode=RuleMode.ADVISORY,
        status=RuleStatus.ACTIVE,
        source=RuleSource.DERIVED,
        evidence=[{"summary_id": "s1", "version": 1}, _GRAPH_EVIDENCE],
        created_at=_CREATED,
        updated_at=_CREATED,
    )
    rules_store.create_rule("a", rule)

    assert store.flag_rules_needing_review("a", "s1", 2) == 1
    got = rules_store.get_rule("a", rule.id)
    assert got is not None and got.needs_review is True
