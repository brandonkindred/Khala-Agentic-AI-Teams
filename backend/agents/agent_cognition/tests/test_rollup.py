"""Tests for the cognition rollup engine (Step 3).

Two layers, mirroring the rest of the package:

* **Pure** tests of the calendar boundary math, rendering helpers, env
  parsing, and period enumeration (monkeypatched store) run with no Postgres.
* **Live-Postgres** tests of the end-to-end engine are skipped automatically
  when ``POSTGRES_HOST`` is unset, using the same schema-provision + truncate
  autouse fixture as ``test_store.py`` and a canned (fake) LLM client.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest
from psycopg.types.json import Json

from agent_cognition.memory import rollup, store
from agent_cognition.models import EventKind, MemoryEvent, PeriodSummary, Scale
from agent_cognition.postgres import SCHEMA
from llm_service.interface import LLMClient
from shared_postgres import is_postgres_enabled, register_team_schemas
from shared_postgres.testing import truncate_team_tables

_UTC = timezone.utc


def _dt(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=_UTC)


# ---------------------------------------------------------------------------
# Fake LLM client — deterministic, records calls.
# ---------------------------------------------------------------------------
class CannedLLM(LLMClient):
    """Returns a fixed structured digest; records prompts for assertions."""

    def __init__(self, summary: str = "rolled up", highlights: list[str] | None = None) -> None:
        self._summary = summary
        self._highlights = highlights if highlights is not None else ["h1"]
        self.json_calls: list[dict[str, Any]] = []
        self.text_calls: list[str] = []

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: str | None = None,
        tools: list | None = None,
        think: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.json_calls.append({"prompt": prompt, "system_prompt": system_prompt})
        return {"summary": self._summary, "highlights": list(self._highlights)}

    def complete(
        self,
        prompt: str,
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        system_prompt: str | None = None,
        tools: list | None = None,
        think: bool = False,
    ) -> str:
        # Used only by compact_text when the input is over budget.
        self.text_calls.append(prompt)
        return "COMPACTED"


# ===========================================================================
# Pure tests (no Postgres) — boundary math, helpers, env, enumeration.
# ===========================================================================
def test_day_bounds() -> None:
    assert rollup._day_bounds(_dt(2026, 5, 4, 15)) == (_dt(2026, 5, 4, 0), _dt(2026, 5, 5, 0))


def test_week_bounds_iso_monday_for_midweek_date() -> None:
    # 2026-05-06 is a Wednesday; its ISO week starts Monday 2026-05-04.
    start, end = rollup._week_bounds(_dt(2026, 5, 6))
    assert start == _dt(2026, 5, 4, 0)
    assert end == _dt(2026, 5, 11, 0)
    assert start.weekday() == 0
    assert (end - start).days == 7


def test_month_bounds() -> None:
    assert rollup._month_bounds(_dt(2026, 5, 20)) == (_dt(2026, 5, 1, 0), _dt(2026, 6, 1, 0))
    # December rolls into the next year.
    assert rollup._month_bounds(_dt(2026, 12, 31)) == (_dt(2026, 12, 1, 0), _dt(2027, 1, 1, 0))


def test_year_bounds() -> None:
    assert rollup._year_bounds(_dt(2026, 5, 20)) == (_dt(2026, 1, 1, 0), _dt(2027, 1, 1, 0))


def test_period_bounds_dispatch_and_is_closed() -> None:
    assert rollup._period_bounds(Scale.DAY, _dt(2026, 5, 4)) == rollup._day_bounds(_dt(2026, 5, 4))
    assert rollup._period_bounds(Scale.YEAR, _dt(2026, 5, 4)) == rollup._year_bounds(
        _dt(2026, 5, 4)
    )
    assert rollup._is_closed(_dt(2026, 5, 4), _dt(2026, 5, 4)) is True  # end == now → closed
    assert rollup._is_closed(_dt(2026, 5, 5), _dt(2026, 5, 4)) is False


def test_render_events_and_children_text() -> None:
    ev = MemoryEvent(
        id="e1",
        agent_id="a",
        kind=EventKind.OUTCOME,
        content="shipped pr",
        occurred_at=_dt(2026, 5, 4),
        source_run_id="r1",
        source_seq=1,
    )
    rendered = rollup._render_events_text([ev])
    assert "outcome" in rendered and "shipped pr" in rendered

    child = PeriodSummary(
        id="s1",
        agent_id="a",
        scale=Scale.DAY,
        period_start=_dt(2026, 5, 4, 0),
        period_end=_dt(2026, 5, 5, 0),
        summary="a good day",
        highlights=["won"],
        created_at=_dt(2026, 5, 5),
    )
    assert "a good day" in rollup._render_children_text([child])
    assert "won" in rollup._render_children_text([child])


def test_max_covers_through() -> None:
    assert rollup._max_covers_through([]) is None
    a = PeriodSummary(
        id="s1",
        agent_id="a",
        scale=Scale.DAY,
        period_start=_dt(2026, 5, 4, 0),
        period_end=_dt(2026, 5, 5, 0),
        covers_through=_dt(2026, 5, 4, 9),
        created_at=_dt(2026, 5, 5),
    )
    b = PeriodSummary(  # no covers_through → falls back to period_end
        id="s2",
        agent_id="a",
        scale=Scale.DAY,
        period_start=_dt(2026, 5, 5, 0),
        period_end=_dt(2026, 5, 6, 0),
        created_at=_dt(2026, 5, 6),
    )
    assert rollup._max_covers_through([a, b]) == _dt(2026, 5, 6, 0)


def test_read_positive_int_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_COGNITION_ROLLUP_INPUT_CHARS", raising=False)
    assert rollup._input_char_budget() == rollup._DEFAULT_INPUT_CHARS
    monkeypatch.setenv("AGENT_COGNITION_ROLLUP_INPUT_CHARS", "555")
    assert rollup._input_char_budget() == 555
    monkeypatch.setenv("AGENT_COGNITION_ROLLUP_INPUT_CHARS", "not-an-int")
    assert rollup._input_char_budget() == rollup._DEFAULT_INPUT_CHARS
    monkeypatch.setenv("AGENT_COGNITION_ROLLUP_INPUT_CHARS", "0")  # non-positive → default
    assert rollup._input_char_budget() == rollup._DEFAULT_INPUT_CHARS
    monkeypatch.setenv("AGENT_COGNITION_ROLLUP_MAX_LOOKBACK_DAYS", "12")
    assert rollup._max_lookback_days() == 12


def test_ensure_rollups_current_empty_agent_id_raises() -> None:
    with pytest.raises(AssertionError):
        rollup.ensure_rollups_current("", _dt(2026, 5, 10))


def test_ensure_rollups_current_naive_now_raises() -> None:
    with pytest.raises(AssertionError):
        rollup.ensure_rollups_current("a", datetime(2026, 5, 10, 12))  # naive


def test_periods_to_process_skips_open_period(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_COGNITION_ROLLUP_MAX_LOOKBACK_DAYS", "3")
    monkeypatch.setattr(store, "fetch_stale_summaries", lambda agent_id, scale: [])
    monkeypatch.setattr(store, "fetch_summaries", lambda agent_id, scale: [])
    now = _dt(2026, 5, 10, 9)
    periods = rollup._periods_to_process("a", Scale.DAY, now)
    starts = {p[0] for p in periods}
    # The current (open) day is excluded; the prior closed day is present.
    assert rollup._day_bounds(now)[0] not in starts
    assert _dt(2026, 5, 9, 0) in starts


# ===========================================================================
# Live-Postgres tests — end-to-end engine with a canned LLM.
# ===========================================================================
pg = pytest.mark.skipif(
    not is_postgres_enabled(),
    reason="POSTGRES_HOST not set; skipping live-Postgres rollup tests",
)


@pytest.fixture(autouse=True)
def _provision_schema() -> None:
    if not is_postgres_enabled():
        return
    register_team_schemas(SCHEMA)
    truncate_team_tables(SCHEMA)


@pytest.fixture()
def canned(monkeypatch: pytest.MonkeyPatch) -> CannedLLM:
    """A canned LLM wired in as the cognition client, with a short lookback."""
    client = CannedLLM()
    monkeypatch.setattr(rollup, "get_client", lambda key: client)
    monkeypatch.setenv("AGENT_COGNITION_ROLLUP_MAX_LOOKBACK_DAYS", "40")
    return client


def _event(
    agent_id: str,
    occurred_at: datetime,
    *,
    seq: int = 1,
    content: str = "did a thing",
) -> MemoryEvent:
    return MemoryEvent(
        id=str(uuid4()),
        agent_id=agent_id,
        kind=EventKind.OBSERVATION,
        content=content,
        occurred_at=occurred_at,
        source_run_id=str(uuid4()),
        source_seq=seq,
    )


def _insert_proposal(agent_id: str, evidence: list[dict[str, Any]]) -> str:
    pid = uuid4().hex
    with store._conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO agent_cognition_rule_proposals (id, agent_id, action, evidence)
               VALUES (%s, %s, %s, %s)""",
            (pid, agent_id, "add", Json(evidence)),
        )
    return pid


def _insert_rule(agent_id: str, evidence: list[dict[str, Any]]) -> str:
    rid = uuid4().hex
    with store._conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO agent_cognition_rules
               (id, agent_id, text, mode, status, source, evidence)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (rid, agent_id, "be careful", "advisory", "active", "derived", Json(evidence)),
        )
    return rid


def _proposal_stale(agent_id: str) -> bool:
    with store._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT stale_evidence FROM agent_cognition_rule_proposals WHERE agent_id = %s",
            (agent_id,),
        )
        return bool(cur.fetchone()[0])


def _rule_needs_review(agent_id: str) -> bool:
    with store._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT needs_review FROM agent_cognition_rules WHERE agent_id = %s",
            (agent_id,),
        )
        return bool(cur.fetchone()[0])


@pg
def test_empty_history_is_a_no_op(canned: CannedLLM) -> None:
    report = rollup.ensure_rollups_current("a", _dt(2026, 5, 10))
    assert report.recomputed == {}
    assert report.amended == {}
    assert canned.json_calls == []  # no LLM calls
    assert store.fetch_summaries("a", Scale.DAY) == []


@pg
def test_single_day_rollup_then_idempotent_rerun(canned: CannedLLM) -> None:
    store.append_event("a", _event("a", _dt(2026, 5, 4)))
    # `now` is past June 1 so the day, its ISO week (ends 05-11), and its month
    # (ends 06-01) are all closed and get summarized.
    now = _dt(2026, 6, 2)

    r1 = rollup.ensure_rollups_current("a", now)
    assert r1.recomputed.get("day") == 1
    assert r1.recomputed.get("week") == 1
    assert r1.recomputed.get("month") == 1
    summary = store.get_existing_summary("a", Scale.DAY, _dt(2026, 5, 4, 0))
    assert summary is not None and summary.version == 1 and summary.summary == "rolled up"

    # Rerun with unchanged state: nothing missing or stale → no work, no dup.
    r2 = rollup.ensure_rollups_current("a", now)
    assert r2.recomputed == {} and r2.amended == {}
    again = store.get_existing_summary("a", Scale.DAY, _dt(2026, 5, 4, 0))
    assert again is not None and again.id == summary.id and again.version == 1
    assert len(store.fetch_summaries("a", Scale.DAY)) == 1


@pg
def test_missed_day_backfilled_on_next_pass(canned: CannedLLM) -> None:
    store.append_event("a", _event("a", _dt(2026, 5, 4)))
    rollup.ensure_rollups_current("a", _dt(2026, 5, 10))
    assert store.get_existing_summary("a", Scale.DAY, _dt(2026, 5, 4, 0)) is not None

    # A later day's events appear; the empty in-between day stays unsummarized.
    store.append_event("a", _event("a", _dt(2026, 5, 6)))
    r2 = rollup.ensure_rollups_current("a", _dt(2026, 5, 10))
    assert r2.recomputed.get("day") == 1  # only 2026-05-06; 2026-05-05 is empty
    assert store.get_existing_summary("a", Scale.DAY, _dt(2026, 5, 6, 0)) is not None
    assert store.get_existing_summary("a", Scale.DAY, _dt(2026, 5, 5, 0)) is None


@pg
def test_month_built_from_days_not_weeks_no_cross_month_bleed(canned: CannedLLM) -> None:
    # 2026-01-29 (Thu) and 2026-02-01 (Sun) fall in the SAME ISO week
    # (Mon 2026-01-26 .. Sun 2026-02-01) but DIFFERENT calendar months.
    agent = "a"
    store.append_event(agent, _event(agent, _dt(2026, 1, 29), seq=1))
    store.append_event(agent, _event(agent, _dt(2026, 1, 29), seq=2))
    store.append_event(agent, _event(agent, _dt(2026, 2, 1), seq=1))
    store.append_event(agent, _event(agent, _dt(2026, 2, 1), seq=2))
    store.append_event(agent, _event(agent, _dt(2026, 2, 1), seq=3))

    rollup.ensure_rollups_current(agent, _dt(2026, 3, 2))  # Jan & Feb months closed

    jan = store.get_existing_summary(agent, Scale.MONTH, _dt(2026, 1, 1, 0))
    feb = store.get_existing_summary(agent, Scale.MONTH, _dt(2026, 2, 1, 0))
    assert jan is not None and feb is not None
    # Months aggregate DAYS: January sees only Jan-29's two events, February
    # only Feb-1's three — no bleed across the straddling ISO week.
    assert jan.source_count == 2
    assert feb.source_count == 3
    # The straddling week DID aggregate across the boundary (it consumes days).
    week = store.get_existing_summary(agent, Scale.WEEK, _dt(2026, 1, 26, 0))
    assert week is not None and week.source_count == 5


@pg
def test_retained_late_event_recomputes_period_and_parents(canned: CannedLLM) -> None:
    agent = "a"
    store.append_event(agent, _event(agent, _dt(2026, 5, 4)))
    # Past June 1 so day, week, and month are all closed (and thus recomputed).
    now = _dt(2026, 6, 2)
    rollup.ensure_rollups_current(agent, now)

    # A late event lands in the already-summarized day; writeback marks stale.
    store.append_event(agent, _event(agent, _dt(2026, 5, 4, 13), seq=2))
    retained = store.mark_period_stale(agent, _dt(2026, 5, 4, 13))
    assert retained is True  # events not pruned → recompute regime

    r2 = rollup.ensure_rollups_current(agent, now)
    assert r2.recomputed.get("day") == 1
    assert r2.recomputed.get("week") == 1
    assert r2.recomputed.get("month") == 1
    day = store.get_existing_summary(agent, Scale.DAY, _dt(2026, 5, 4, 0))
    assert day is not None and day.version == 2 and day.source_count == 2 and day.stale is False


@pg
def test_pruned_late_event_amends_not_rebuilds(canned: CannedLLM) -> None:
    agent = "a"
    day_start = _dt(2026, 5, 4, 0)
    store.append_event(agent, _event(agent, _dt(2026, 5, 4), content="ORIGINAL WORK"))
    now = _dt(2026, 5, 10)
    rollup.ensure_rollups_current(agent, now)
    base = store.get_existing_summary(agent, Scale.DAY, day_start)
    assert base is not None and base.source_count == 1

    # Prune the day's (folded) events, then a late event arrives.
    assert store.prune_events(agent, 0) == 1
    pruned = store.get_existing_summary(agent, Scale.DAY, day_start)
    assert pruned is not None and pruned.events_pruned is True

    store.append_event(agent, _event(agent, _dt(2026, 5, 4, 14), seq=2, content="LATE WORK"))
    retained = store.mark_period_stale(agent, _dt(2026, 5, 4, 14))
    assert retained is False  # pruned → amend regime

    canned.json_calls.clear()
    r2 = rollup.ensure_rollups_current(agent, now)
    assert r2.amended.get("day") == 1
    assert "day" not in r2.recomputed  # day was amended, not rebuilt

    revised = store.get_existing_summary(agent, Scale.DAY, day_start)
    assert revised is not None
    assert revised.source_count == 2  # base (1) + late (1), not reset to 1
    assert revised.covers_through == _dt(2026, 5, 4, 14)
    # mark_period_stale bumped 1 → 2; the amend leaves it at 2 (no double-bump).
    assert revised.version == 2
    # The amend prompt preserved the base summary (not a rebuild from the late row).
    amend_prompts = [c["prompt"] for c in canned.json_calls if "BASE SUMMARY" in c["prompt"]]
    assert amend_prompts and "rolled up" in amend_prompts[0] and "LATE WORK" in amend_prompts[0]


@pg
def test_pruned_day_marked_stale_without_surviving_events_is_a_no_op(canned: CannedLLM) -> None:
    agent = "a"
    store.append_event(agent, _event(agent, _dt(2026, 5, 4)))
    now = _dt(2026, 5, 10)
    rollup.ensure_rollups_current(agent, now)
    assert store.prune_events(agent, 0) == 1  # day's events gone, events_pruned latched

    # The period is flagged stale but no late event actually survived the prune.
    store.mark_period_stale(agent, _dt(2026, 5, 4, 12))
    canned.json_calls.clear()
    report = rollup.ensure_rollups_current(agent, now)
    assert report.amended == {}  # nothing to amend
    assert "day" not in report.recomputed  # and never rebuilt from an empty set
    assert canned.json_calls == []


@pg
def test_second_amend_excludes_already_folded_events(canned: CannedLLM) -> None:
    # A second late event arriving before the next prune must NOT re-fold the
    # first late event (which still lingers in the table post-amend): the amend
    # bounds its input by the summary's fold point.
    agent = "a"
    day_start = _dt(2026, 5, 4, 0)
    now = _dt(2026, 5, 10)
    store.append_event(agent, _event(agent, _dt(2026, 5, 4), content="ORIGINAL"))
    rollup.ensure_rollups_current(agent, now)
    assert store.prune_events(agent, 0) == 1  # ORIGINAL pruned; events_pruned latched

    # First late event → first amend (source_count 1 → 2). It is NOT pruned.
    store.append_event(agent, _event(agent, _dt(2026, 5, 4, 12), seq=2, content="SECOND"))
    store.mark_period_stale(agent, _dt(2026, 5, 4, 12))
    rollup.ensure_rollups_current(agent, now)
    assert store.get_existing_summary(agent, Scale.DAY, day_start).source_count == 2

    # Second late event arrives before any prune; SECOND still sits in the table.
    store.append_event(agent, _event(agent, _dt(2026, 5, 4, 14), seq=3, content="THIRD"))
    store.mark_period_stale(agent, _dt(2026, 5, 4, 14))
    canned.json_calls.clear()
    rollup.ensure_rollups_current(agent, now)

    revised = store.get_existing_summary(agent, Scale.DAY, day_start)
    assert revised.source_count == 3  # 2 + THIRD only, NOT 4 (SECOND not re-folded)
    amend_prompt = next(c["prompt"] for c in canned.json_calls if "BASE SUMMARY" in c["prompt"])
    assert "THIRD" in amend_prompt and "SECOND" not in amend_prompt


@pg
def test_fetch_unfolded_events_bounds_by_fold_point() -> None:
    # Direct store-level check: an event counts as unfolded only when its
    # (real-time) recorded_at is after the summary's computed_at fold point.
    day = (_dt(2026, 5, 4, 0), _dt(2026, 5, 5, 0))
    snapshot = _dt(2100, 1, 1)

    def _summary_row(agent: str) -> PeriodSummary:
        return PeriodSummary(
            id=str(uuid4()),
            agent_id=agent,
            scale=Scale.DAY,
            period_start=day[0],
            period_end=day[1],
            created_at=_dt(2026, 5, 20),
        )

    # Folded: fold point in the future → recorded_at <= computed_at → excluded.
    store.append_event("folded", _event("folded", _dt(2026, 5, 4), content="x"))
    store.upsert_summary("folded", _summary_row("folded"), computed_at=_dt(2099, 1, 1))
    assert store.fetch_unfolded_events("folded", Scale.DAY, day[0], day[1], snapshot=snapshot) == []

    # Unfolded: fold point in the past → recorded_at > computed_at → returned.
    store.append_event("unfolded", _event("unfolded", _dt(2026, 5, 4), content="x"))
    store.upsert_summary("unfolded", _summary_row("unfolded"), computed_at=_dt(2000, 1, 1))
    got = store.fetch_unfolded_events("unfolded", Scale.DAY, day[0], day[1], snapshot=snapshot)
    assert len(got) == 1

    # No summary → fold point is -infinity (COALESCE), so every event qualifies.
    store.append_event("nosummary", _event("nosummary", _dt(2026, 5, 4), content="x"))
    got2 = store.fetch_unfolded_events("nosummary", Scale.DAY, day[0], day[1], snapshot=snapshot)
    assert len(got2) == 1


@pg
def test_hierarchical_day_week_month_year(
    canned: CannedLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = "a"
    store.append_event(agent, _event(agent, _dt(2025, 12, 30)))
    store.append_event(agent, _event(agent, _dt(2025, 12, 31)))
    # now in early 2026 so the 2025 year (and Dec month) are closed.
    report = rollup.ensure_rollups_current(agent, _dt(2026, 1, 5))

    assert report.recomputed.get("day") == 2
    assert report.recomputed.get("month") == 1
    assert report.recomputed.get("year") == 1
    year = store.get_existing_summary(agent, Scale.YEAR, _dt(2025, 1, 1, 0))
    month = store.get_existing_summary(agent, Scale.MONTH, _dt(2025, 12, 1, 0))
    assert year is not None and month is not None
    assert month.source_count == 2  # both December days
    assert year.source_count == 2  # year consumed the month (which consumed days)


@pg
def test_compaction_path_invoked_for_large_input(
    canned: CannedLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_COGNITION_ROLLUP_INPUT_CHARS", "30")
    store.append_event("a", _event("a", _dt(2026, 5, 4), content="x" * 200))
    rollup.ensure_rollups_current("a", _dt(2026, 5, 10))
    assert canned.text_calls  # compact_text invoked the LLM on the over-budget input


@pg
def test_evidence_flagged_on_recompute(canned: CannedLLM) -> None:
    agent = "a"
    day_start = _dt(2026, 5, 4, 0)
    store.append_event(agent, _event(agent, _dt(2026, 5, 4)))
    now = _dt(2026, 5, 10)
    rollup.ensure_rollups_current(agent, now)
    summary = store.get_existing_summary(agent, Scale.DAY, day_start)
    assert summary is not None and summary.version == 1

    # A learned rule and a pending proposal cite this summary at version 1.
    evidence = [{"summary_id": summary.id, "version": 1}]
    _insert_proposal(agent, evidence)
    _insert_rule(agent, evidence)

    # Late event bumps the summary version; the recompute supersedes the cited
    # version, so the dependent rows are flagged for review.
    store.append_event(agent, _event(agent, _dt(2026, 5, 4, 15), seq=2))
    store.mark_period_stale(agent, _dt(2026, 5, 4, 15))
    report = rollup.ensure_rollups_current(agent, now)

    assert report.evidence_flagged >= 2
    assert _proposal_stale(agent) is True
    assert _rule_needs_review(agent) is True


@pg
def test_open_period_is_not_summarized(canned: CannedLLM) -> None:
    # `now` mid-day: today's day period is still open and must be skipped.
    store.append_event("a", _event("a", _dt(2026, 5, 10, 8)))
    report = rollup.ensure_rollups_current("a", _dt(2026, 5, 10, 9))
    assert report.skipped_open >= 1
    assert store.get_existing_summary("a", Scale.DAY, _dt(2026, 5, 10, 0)) is None


# ---------------------------------------------------------------------------
# New store readers (live Postgres).
# ---------------------------------------------------------------------------
@pg
def test_fetch_stale_summaries_ascending() -> None:
    agent = "a"
    for d in (4, 6, 5):
        s = PeriodSummary(
            id=str(uuid4()),
            agent_id=agent,
            scale=Scale.DAY,
            period_start=_dt(2026, 5, d, 0),
            period_end=_dt(2026, 5, d + 1, 0),
            created_at=_dt(2026, 5, 20),
        )
        store.upsert_summary(agent, s, computed_at=_dt(2099, 1, 1))
        store.mark_period_stale(agent, _dt(2026, 5, d, 12))
    stale = store.fetch_stale_summaries(agent, Scale.DAY)
    assert [s.period_start for s in stale] == [
        _dt(2026, 5, 4, 0),
        _dt(2026, 5, 5, 0),
        _dt(2026, 5, 6, 0),
    ]


@pg
def test_fetch_summaries_in_window_is_half_open() -> None:
    agent = "a"
    for d in (4, 5, 6):
        s = PeriodSummary(
            id=str(uuid4()),
            agent_id=agent,
            scale=Scale.DAY,
            period_start=_dt(2026, 5, d, 0),
            period_end=_dt(2026, 5, d + 1, 0),
            created_at=_dt(2026, 5, 20),
        )
        store.upsert_summary(agent, s, computed_at=_dt(2099, 1, 1))
    got = store.fetch_summaries_in_window(agent, Scale.DAY, _dt(2026, 5, 4, 0), _dt(2026, 5, 6, 0))
    # start inclusive (5/4), end exclusive (5/6 excluded).
    assert [s.period_start for s in got] == [_dt(2026, 5, 4, 0), _dt(2026, 5, 5, 0)]


@pg
def test_get_existing_summary_point_read_and_miss() -> None:
    agent = "a"
    assert store.get_existing_summary(agent, Scale.DAY, _dt(2026, 5, 4, 0)) is None
    s = PeriodSummary(
        id=str(uuid4()),
        agent_id=agent,
        scale=Scale.DAY,
        period_start=_dt(2026, 5, 4, 0),
        period_end=_dt(2026, 5, 5, 0),
        created_at=_dt(2026, 5, 20),
    )
    store.upsert_summary(agent, s, computed_at=_dt(2099, 1, 1))
    got = store.get_existing_summary(agent, Scale.DAY, _dt(2026, 5, 4, 0))
    assert got is not None and got.id == s.id


@pg
def test_flag_writers_ignore_unrelated_and_fresh_evidence() -> None:
    agent = "a"
    # Proposal cites a different summary; rule cites the right summary but at a
    # version that is NOT below the new version → neither should be flagged.
    _insert_proposal(agent, [{"summary_id": "other", "version": 1}])
    _insert_rule(agent, [{"summary_id": "sid", "version": 5}])
    assert store.flag_stale_proposals(agent, "sid", 3) == 0
    assert store.flag_rules_needing_review(agent, "sid", 3) == 0
    assert _proposal_stale(agent) is False
    assert _rule_needs_review(agent) is False
