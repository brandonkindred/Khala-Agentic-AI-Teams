"""Tests for the cognition retrieval / digest builder (Step 4).

Two layers, matching the rest of the package:

* **Pure** tests of digest assembly, budget enforcement, ordering, rendering
  helpers, and env parsing run with no Postgres (the store fetchers and the
  compaction hook are monkeypatched).
* A **live-Postgres** end-to-end test is skipped automatically when
  ``POSTGRES_HOST`` is unset, using the same schema-provision + truncate autouse
  fixture as ``test_store.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest

from agent_cognition.memory import retrieval, store
from agent_cognition.models import EventKind, MemoryEvent, PeriodSummary, Scale

_UTC = timezone.utc
_MID_DAY = datetime(2026, 6, 1, 12, 0, tzinfo=_UTC)
_DAY = (datetime(2026, 6, 1, tzinfo=_UTC), datetime(2026, 6, 2, tzinfo=_UTC))
_WEEK = (datetime(2026, 6, 1, tzinfo=_UTC), datetime(2026, 6, 8, tzinfo=_UTC))
_MONTH = (datetime(2026, 6, 1, tzinfo=_UTC), datetime(2026, 7, 1, tzinfo=_UTC))
_CREATED = datetime(2026, 6, 2, tzinfo=_UTC)


def _event(
    agent_id: str = "a",
    *,
    seq: int = 1,
    salience: float = 0.0,
    occurred_at: datetime = _MID_DAY,
    kind: EventKind = EventKind.OBSERVATION,
    content: str = "",
) -> MemoryEvent:
    return MemoryEvent(
        id=str(uuid4()),
        agent_id=agent_id,
        kind=kind,
        content=content,
        salience=salience,
        occurred_at=occurred_at,
        source_run_id=str(uuid4()),
        source_seq=seq,
    )


def _summary(
    agent_id: str = "a",
    *,
    scale: Scale = Scale.DAY,
    window: tuple[datetime, datetime] = _DAY,
    summary: str = "",
    highlights: list | None = None,
    stale: bool = False,
) -> PeriodSummary:
    return PeriodSummary(
        id=str(uuid4()),
        agent_id=agent_id,
        scale=scale,
        period_start=window[0],
        period_end=window[1],
        summary=summary,
        highlights=highlights or [],
        stale=stale,
        created_at=_CREATED,
    )


def _wire_store(
    monkeypatch: pytest.MonkeyPatch,
    *,
    summaries: dict[Scale, PeriodSummary] | None = None,
    events: list[MemoryEvent] | None = None,
) -> dict[str, Any]:
    """Monkeypatch the store fetchers; return a dict capturing the recent-events call."""
    summaries = summaries or {}
    captured: dict[str, Any] = {}

    # The builder gathers per-scale context via fetch_summaries (newest first),
    # skipping stale rows. The map holds one non-stale summary per scale.
    def _fetch_summaries(agent_id: str, scale: Scale, limit=None, offset: int = 0):
        s = summaries.get(scale)
        return [s] if s is not None else []

    monkeypatch.setattr(store, "fetch_summaries", _fetch_summaries)

    def _fetch_recent(agent_id: str, top_n: int, by_salience: bool = True):
        captured["agent_id"] = agent_id
        captured["top_n"] = top_n
        captured["by_salience"] = by_salience
        return list(events or [])

    monkeypatch.setattr(store, "fetch_recent_events", _fetch_recent)

    # When a closed DAY summary is present the builder bounds the live section via
    # fetch_events_for_period instead; return the same events through that seam so
    # tests don't have to care which path runs.
    def _fetch_period(agent_id: str, period_start, period_end, *, snapshot=None):
        captured["period_start"] = period_start
        captured["period_end"] = period_end
        return list(events or [])

    monkeypatch.setattr(store, "fetch_events_for_period", _fetch_period)
    return captured


def _no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any attempt to construct/compact via the LLM fail loudly."""
    monkeypatch.setattr(
        retrieval, "get_client", lambda *a, **k: pytest.fail("LLM should not be used")
    )
    monkeypatch.setattr(
        retrieval, "compact_text", lambda *a, **k: pytest.fail("compaction should not run")
    )


# ===========================================================================
# Preconditions (DbC)
# ===========================================================================
def test_empty_agent_id_raises() -> None:
    with pytest.raises(AssertionError):
        retrieval.build_memory_digest("", 100)


def test_negative_budget_raises() -> None:
    with pytest.raises(AssertionError):
        retrieval.build_memory_digest("a", -1)


# ===========================================================================
# Empty / zero paths
# ===========================================================================
def test_zero_budget_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_llm(monkeypatch)
    # Store is never consulted for a zero budget.
    monkeypatch.setattr(
        store, "fetch_summaries", lambda *a, **k: pytest.fail("no store read for 0 budget")
    )
    assert retrieval.build_memory_digest("a", 0) == ""


def test_empty_history_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire_store(monkeypatch, summaries={}, events=[])
    _no_llm(monkeypatch)
    assert retrieval.build_memory_digest("a", 100) == ""


# ===========================================================================
# Digest assembly + ordering
# ===========================================================================
def test_mid_period_includes_closed_week_month_and_live_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No closed day summary yet (mid-day), but week + month exist and there are
    # in-progress events — the digest must not be empty.
    summaries = {
        Scale.MONTH: _summary(scale=Scale.MONTH, window=_MONTH, summary="month recap"),
        Scale.WEEK: _summary(scale=Scale.WEEK, window=_WEEK, summary="week recap"),
    }
    events = [_event(content="just happened", salience=0.9)]
    _wire_store(monkeypatch, summaries=summaries, events=events)
    _no_llm(monkeypatch)

    digest = retrieval.build_memory_digest("a", 1000)

    assert "month recap" in digest
    assert "week recap" in digest
    assert "just happened" in digest
    assert "## Long-term memory" in digest
    assert "## Recent activity" in digest


def test_section_and_scale_ordering(monkeypatch: pytest.MonkeyPatch) -> None:
    summaries = {
        Scale.MONTH: _summary(scale=Scale.MONTH, window=_MONTH, summary="M"),
        Scale.WEEK: _summary(scale=Scale.WEEK, window=_WEEK, summary="W"),
        Scale.DAY: _summary(scale=Scale.DAY, window=_DAY, summary="D"),
    }
    events = [_event(content="E")]
    _wire_store(monkeypatch, summaries=summaries, events=events)
    _no_llm(monkeypatch)

    digest = retrieval.build_memory_digest("a", 1000)

    # broadest → narrowest, then recent activity last.
    assert digest.index("[month]") < digest.index("[week]") < digest.index("[day]")
    assert digest.index("## Long-term memory") < digest.index("## Recent activity")


def test_recent_events_request_uses_salience_and_preserves_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The store returns events already ordered (salience DESC); the digest must
    # preserve that order verbatim.
    events = [
        _event(content="high", salience=0.9),
        _event(content="mid", salience=0.5),
        _event(content="low", salience=0.1),
    ]
    captured = _wire_store(monkeypatch, summaries={}, events=events)
    _no_llm(monkeypatch)

    digest = retrieval.build_memory_digest("a", 1000)

    assert captured["by_salience"] is True
    assert digest.index("high") < digest.index("mid") < digest.index("low")


def test_summaries_only_renders_without_recent_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summaries = {Scale.DAY: _summary(scale=Scale.DAY, summary="only a day")}
    _wire_store(monkeypatch, summaries=summaries, events=[])
    _no_llm(monkeypatch)

    digest = retrieval.build_memory_digest("a", 1000)

    assert "## Long-term memory" in digest
    assert "## Recent activity" not in digest


def test_events_only_renders_without_long_term_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_store(monkeypatch, summaries={}, events=[_event(content="solo")])
    _no_llm(monkeypatch)

    digest = retrieval.build_memory_digest("a", 1000)

    assert "## Recent activity" in digest
    assert "## Long-term memory" not in digest


# ===========================================================================
# In-progress event window (bounded to what closed summaries don't cover)
# ===========================================================================
def test_live_events_bounded_to_after_latest_closed_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With a closed day summary present, the live section is sourced from
    # fetch_events_for_period starting at that day's period_end (not the unbounded
    # fetch_recent_events, which would resurface already-summarized history).
    day = _summary(scale=Scale.DAY, window=_DAY, summary="yesterday")
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        store, "fetch_summaries", lambda agent_id, scale, **k: [day] if scale is Scale.DAY else []
    )
    monkeypatch.setattr(
        store,
        "fetch_recent_events",
        lambda *a, **k: pytest.fail("must not use the unbounded fetch when a day summary exists"),
    )

    def _fetch_period(agent_id: str, period_start, period_end, *, snapshot=None):
        captured["period_start"] = period_start
        captured["period_end"] = period_end
        return [_event(content="today", salience=0.4)]

    monkeypatch.setattr(store, "fetch_events_for_period", _fetch_period)
    _no_llm(monkeypatch)

    digest = retrieval.build_memory_digest("a", 1000)

    assert captured["period_start"] == day.period_end
    assert captured["period_end"] == retrieval._FAR_FUTURE
    assert "today" in digest


def test_live_events_sorted_by_salience_and_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    # fetch_events_for_period yields (occurred_at, id) ASC; the builder must
    # re-rank by salience DESC and apply the top-N cap.
    monkeypatch.setenv("AGENT_COGNITION_DIGEST_EVENT_TOP_N", "2")
    day = _summary(scale=Scale.DAY, window=_DAY)
    window_events = [
        _event(content="lo", salience=0.1, occurred_at=datetime(2026, 6, 2, 1, tzinfo=_UTC)),
        _event(content="hi", salience=0.9, occurred_at=datetime(2026, 6, 2, 2, tzinfo=_UTC)),
        _event(content="mid", salience=0.5, occurred_at=datetime(2026, 6, 2, 3, tzinfo=_UTC)),
    ]
    monkeypatch.setattr(
        store, "fetch_summaries", lambda agent_id, scale, **k: [day] if scale is Scale.DAY else []
    )
    monkeypatch.setattr(store, "fetch_events_for_period", lambda *a, **k: list(window_events))
    _no_llm(monkeypatch)

    digest = retrieval.build_memory_digest("a", 1000)

    assert "hi" in digest and "mid" in digest  # top-2 by salience
    assert "lo" not in digest  # capped out
    assert digest.index("hi") < digest.index("mid")  # salience DESC


def test_cold_start_uses_unbounded_recent_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    # No closed day summary yet → every retained event is uncovered, so the
    # unbounded recent-events fetch is used.
    monkeypatch.setattr(store, "fetch_summaries", lambda *a, **k: [])
    monkeypatch.setattr(
        store,
        "fetch_events_for_period",
        lambda *a, **k: pytest.fail("cold start must use the unbounded fetch"),
    )
    captured: dict[str, Any] = {}

    def _fetch_recent(agent_id: str, top_n: int, by_salience: bool = True):
        captured["by_salience"] = by_salience
        return [_event(content="all-history")]

    monkeypatch.setattr(store, "fetch_recent_events", _fetch_recent)
    _no_llm(monkeypatch)

    digest = retrieval.build_memory_digest("a", 1000)

    assert captured["by_salience"] is True
    assert "all-history" in digest


# ===========================================================================
# Stale-summary handling
# ===========================================================================
def test_stale_latest_summary_falls_back_to_non_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The newest day summary is stale (a late writeback invalidated it); the
    # digest must skip it and surface the latest still-current summary instead.
    stale_day = _summary(scale=Scale.DAY, window=_DAY, summary="STALE today", stale=True)
    older_day = _summary(
        scale=Scale.DAY,
        window=(datetime(2026, 5, 31, tzinfo=_UTC), datetime(2026, 6, 1, tzinfo=_UTC)),
        summary="current yesterday",
    )
    monkeypatch.setattr(
        store,
        "fetch_summaries",
        lambda agent_id, scale, **k: [stale_day, older_day] if scale is Scale.DAY else [],
    )
    monkeypatch.setattr(store, "fetch_events_for_period", lambda *a, **k: [])
    _no_llm(monkeypatch)

    digest = retrieval.build_memory_digest("a", 1000)

    assert "current yesterday" in digest
    assert "STALE today" not in digest


def test_all_recent_summaries_stale_contributes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every scanned summary is stale → that scale contributes no long-range
    # context (rather than surfacing invalidated memory).
    monkeypatch.setattr(
        store,
        "fetch_summaries",
        lambda agent_id, scale, **k: [_summary(scale=scale, summary="x", stale=True)],
    )
    monkeypatch.setattr(store, "fetch_recent_events", lambda *a, **k: [_event(content="live")])
    _no_llm(monkeypatch)

    digest = retrieval.build_memory_digest("a", 1000)

    assert "## Long-term memory" not in digest
    assert "live" in digest  # cold-start event path still runs (no non-stale day)


def test_stale_day_does_not_hide_late_events(monkeypatch: pytest.MonkeyPatch) -> None:
    # A late event lands in the latest closed day, marking that day stale. The
    # event boundary must come from the latest *non-stale* day, so the late event
    # (which occurred within the now-stale day) still surfaces as recent activity.
    stale_day = _summary(scale=Scale.DAY, window=_DAY, summary="stale", stale=True)
    prev_day = _summary(
        scale=Scale.DAY,
        window=(datetime(2026, 5, 31, tzinfo=_UTC), datetime(2026, 6, 1, tzinfo=_UTC)),
        summary="prev",
    )
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        store,
        "fetch_summaries",
        lambda agent_id, scale, **k: [stale_day, prev_day] if scale is Scale.DAY else [],
    )

    def _fetch_period(agent_id, period_start, period_end, *, snapshot=None):
        captured["period_start"] = period_start
        return [_event(content="late event", salience=0.3)]

    monkeypatch.setattr(store, "fetch_events_for_period", _fetch_period)
    _no_llm(monkeypatch)

    digest = retrieval.build_memory_digest("a", 1000)

    # Boundary is the non-stale (previous) day's end, not the stale day's end,
    # so the late event inside the stale day is included.
    assert captured["period_start"] == prev_day.period_end
    assert "late event" in digest
    assert "prev" in digest
    assert "stale" not in digest


def test_latest_non_stale_summary_pages_past_long_stale_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A run of stale summaries longer than one page must not hide the older
    # still-current summary: the scan pages until it finds a non-stale row.
    monkeypatch.setattr(retrieval, "_SUMMARY_PAGE_SIZE", 2)
    backing = [_summary(scale=Scale.DAY, summary=f"stale{i}", stale=True) for i in range(3)] + [
        _summary(scale=Scale.DAY, summary="current", stale=False)
    ]
    calls: list[tuple[int, int]] = []

    def _fetch(agent_id, scale, limit=None, offset=0):
        calls.append((limit, offset))
        return backing[offset : offset + limit] if scale is Scale.DAY else []

    monkeypatch.setattr(store, "fetch_summaries", _fetch)
    monkeypatch.setattr(store, "fetch_events_for_period", lambda *a, **k: [])
    _no_llm(monkeypatch)

    digest = retrieval.build_memory_digest("a", 1000)

    assert "current" in digest
    assert "stale" not in digest
    # Paged past the first full (all-stale) page into the second.
    assert (2, 0) in calls and (2, 2) in calls


def test_latest_non_stale_summary_stops_at_short_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An all-stale history that ends on a short page terminates (returns None)
    # without an extra empty fetch.
    monkeypatch.setattr(retrieval, "_SUMMARY_PAGE_SIZE", 2)
    backing = [_summary(scale=Scale.DAY, summary="s", stale=True)]  # 1 row < page size
    calls: list[tuple[int, int]] = []

    def _fetch(agent_id, scale, limit=None, offset=0):
        calls.append((scale, offset))
        return backing[offset : offset + limit] if scale is Scale.DAY else []

    monkeypatch.setattr(store, "fetch_summaries", _fetch)
    monkeypatch.setattr(store, "fetch_recent_events", lambda *a, **k: [_event(content="live")])
    _no_llm(monkeypatch)

    digest = retrieval.build_memory_digest("a", 1000)

    assert "## Long-term memory" not in digest
    assert "live" in digest
    # Only one DAY fetch (offset 0); the short page ended the scan.
    assert [c for c in calls if c[0] is Scale.DAY] == [(Scale.DAY, 0)]


# ===========================================================================
# Budget enforcement
# ===========================================================================
def test_under_budget_returned_verbatim_without_compaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_store(monkeypatch, summaries={}, events=[_event(content="x")])
    _no_llm(monkeypatch)  # asserts compaction / client are never touched

    digest = retrieval.build_memory_digest("a", 1000)
    assert "x" in digest


def test_over_budget_invokes_compaction(monkeypatch: pytest.MonkeyPatch) -> None:
    big = _summary(scale=Scale.DAY, summary="z" * 500)
    _wire_store(monkeypatch, summaries={Scale.DAY: big}, events=[])

    calls: list[tuple[Any, ...]] = []
    sentinel_client = object()
    monkeypatch.setattr(retrieval, "get_client", lambda key: sentinel_client)

    def _fake_compact(text: str, max_chars: int, llm: Any, label: str) -> str:
        calls.append((text, max_chars, llm, label))
        return "compacted"

    monkeypatch.setattr(retrieval, "compact_text", _fake_compact)

    # token_budget 10 → char_budget 40, well under the 500-char digest.
    digest = retrieval.build_memory_digest("a", 10)

    assert digest == "compacted"
    assert len(calls) == 1
    _text, max_chars, llm, label = calls[0]
    assert max_chars == 10 * retrieval._CHARS_PER_TOKEN
    assert llm is sentinel_client
    assert label == "memory digest"


def test_compaction_overshoot_is_hard_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    big = _summary(scale=Scale.DAY, summary="z" * 500)
    _wire_store(monkeypatch, summaries={Scale.DAY: big}, events=[])
    monkeypatch.setattr(retrieval, "get_client", lambda key: object())
    # compact_text is best-effort and may overshoot — the safety net must clamp.
    monkeypatch.setattr(retrieval, "compact_text", lambda *a, **k: "y" * 999)

    digest = retrieval.build_memory_digest("a", 10)
    assert len(digest) == 10 * retrieval._CHARS_PER_TOKEN


# ===========================================================================
# Rendering helpers (pure)
# ===========================================================================
def test_render_summary_line_with_and_without_highlights() -> None:
    with_hl = retrieval._render_summary_line(
        Scale.WEEK, _summary(scale=Scale.WEEK, summary="text", highlights=["a", "b"])
    )
    assert with_hl == "[week] text | highlights: a; b"

    without_hl = retrieval._render_summary_line(
        Scale.DAY, _summary(scale=Scale.DAY, summary="text")
    )
    assert without_hl == "[day] text"


def test_render_events_format() -> None:
    ev = _event(content="did a thing", salience=0.75, kind=EventKind.ACTION)
    line = retrieval._render_events([ev])
    assert line == f"[{_MID_DAY.isoformat()}] action (salience=0.75): did a thing"


# ===========================================================================
# Env tunables
# ===========================================================================
def test_event_top_n_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_COGNITION_DIGEST_EVENT_TOP_N", raising=False)
    assert retrieval._event_top_n() == retrieval._DEFAULT_EVENT_TOP_N
    monkeypatch.setenv("AGENT_COGNITION_DIGEST_EVENT_TOP_N", "7")
    assert retrieval._event_top_n() == 7
    monkeypatch.setenv("AGENT_COGNITION_DIGEST_EVENT_TOP_N", "not-an-int")
    assert retrieval._event_top_n() == retrieval._DEFAULT_EVENT_TOP_N
    monkeypatch.setenv("AGENT_COGNITION_DIGEST_EVENT_TOP_N", "0")  # non-positive → default
    assert retrieval._event_top_n() == retrieval._DEFAULT_EVENT_TOP_N


# ===========================================================================
# Live-Postgres end-to-end (skipped without POSTGRES_HOST)
# ===========================================================================
class _LoudLLM:
    """Stand-in that fails if used — the e2e budget is large enough to skip compaction."""

    def __getattr__(self, _name: str):  # pragma: no cover - defensive
        raise AssertionError("LLM should not be used in the e2e digest test")


@pytest.mark.skipif(
    not __import__("shared_postgres").is_postgres_enabled(),
    reason="POSTGRES_HOST not set; skipping live-Postgres retrieval test",
)
def test_build_digest_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_cognition.postgres import SCHEMA
    from shared_postgres import register_team_schemas
    from shared_postgres.testing import truncate_team_tables

    register_team_schemas(SCHEMA)
    truncate_team_tables(SCHEMA)
    monkeypatch.setattr(retrieval, "get_client", lambda *a, **k: _LoudLLM())

    agent_id = f"agent-{uuid4()}"
    store.append_event(agent_id, _event(agent_id, content="shipped the thing", salience=0.8))
    store.upsert_summary(
        agent_id,
        _summary(agent_id, scale=Scale.WEEK, window=_WEEK, summary="weekly recap"),
        computed_at=datetime(2099, 1, 1, tzinfo=_UTC),
    )

    digest = retrieval.build_memory_digest(agent_id, 2000)

    assert "weekly recap" in digest
    assert "shipped the thing" in digest
    assert "## Long-term memory" in digest
    assert "## Recent activity" in digest

    # An agent with no memory yields an empty digest.
    assert retrieval.build_memory_digest(f"empty-{uuid4()}", 2000) == ""
