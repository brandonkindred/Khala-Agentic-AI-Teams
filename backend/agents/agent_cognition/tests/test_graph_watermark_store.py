"""Tests for the knowledge-graph watermark store.

Two layers, mirroring the other cognition store tests:

* **Precondition tests** run without a database — they prove the DbC asserts fire
  before any connection is attempted.
* **Live-Postgres tests** are skipped unless ``POSTGRES_HOST`` is set; they prove
  insert / advance / partial-update / list semantics against real Postgres.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent_cognition.graph import watermark_store
from agent_cognition.postgres import SCHEMA
from shared_postgres import is_postgres_enabled

_UTC = timezone.utc
_T0 = datetime(2026, 6, 1, 10, 0, tzinfo=_UTC)
_T1 = datetime(2026, 6, 1, 11, 0, tzinfo=_UTC)


# ---------------------------------------------------------------------------
# Precondition tests (no Postgres required — asserts fire before _conn()).
# ---------------------------------------------------------------------------
def test_get_watermark_rejects_empty_agent():
    with pytest.raises(AssertionError):
        watermark_store.get_watermark("")


def test_upsert_rejects_empty_agent():
    with pytest.raises(AssertionError):
        watermark_store.upsert_watermark("")


def test_upsert_rejects_negative_delta():
    with pytest.raises(AssertionError):
        watermark_store.upsert_watermark("agent-1", ingested_delta=-1)


def test_upsert_rejects_half_event_cursor():
    with pytest.raises(AssertionError):
        watermark_store.upsert_watermark("agent-1", last_event_recorded_at=_T0, last_event_id=None)


def test_upsert_rejects_half_summary_cursor():
    with pytest.raises(AssertionError):
        watermark_store.upsert_watermark("agent-1", last_summary_id="s1")


# ---------------------------------------------------------------------------
# Live-Postgres tests.
# ---------------------------------------------------------------------------
pg = pytest.mark.skipif(
    not is_postgres_enabled(),
    reason="POSTGRES_HOST not set; skipping live-Postgres watermark store tests",
)


@pytest.fixture(autouse=True)
def _provision_schema():
    if not is_postgres_enabled():
        return
    from shared_postgres import register_team_schemas
    from shared_postgres.testing import truncate_team_tables

    register_team_schemas(SCHEMA)
    truncate_team_tables(SCHEMA)


@pg
def test_get_watermark_none_for_unknown_agent():
    assert watermark_store.get_watermark("nobody") is None


@pg
def test_insert_and_get_roundtrip():
    watermark_store.upsert_watermark(
        "agent-1",
        last_event_recorded_at=_T0,
        last_event_id="e1",
        last_summary_created_at=_T0,
        last_summary_id="s1",
        ingested_delta=3,
    )
    wm = watermark_store.get_watermark("agent-1")
    assert wm is not None
    assert wm.last_event_recorded_at == _T0
    assert wm.last_event_id == "e1"
    assert wm.last_summary_created_at == _T0
    assert wm.last_summary_id == "s1"
    assert wm.ingested_count == 3


@pg
def test_partial_update_preserves_other_cursor_and_accumulates_count():
    # First pass advances the event cursor only.
    watermark_store.upsert_watermark(
        "agent-1", last_event_recorded_at=_T0, last_event_id="e1", ingested_delta=2
    )
    # Second pass advances only the summary cursor — the event cursor must survive.
    watermark_store.upsert_watermark(
        "agent-1", last_summary_created_at=_T1, last_summary_id="s9", ingested_delta=5
    )
    wm = watermark_store.get_watermark("agent-1")
    assert wm.last_event_recorded_at == _T0  # preserved
    assert wm.last_event_id == "e1"
    assert wm.last_summary_created_at == _T1
    assert wm.last_summary_id == "s9"
    assert wm.ingested_count == 7  # 2 + 5 accumulated


@pg
def test_list_agent_ids_with_events_distinct_sorted():
    from agent_cognition.memory import store as memory_store
    from agent_cognition.models import EventKind, MemoryEvent

    for aid, seq in (("b", 0), ("a", 0), ("a", 1)):
        memory_store.append_event(
            aid,
            MemoryEvent(
                id=f"{aid}-{seq}",
                agent_id=aid,
                kind=EventKind.OBSERVATION,
                content="x",
                occurred_at=_T0,
                source_run_id=f"r-{aid}-{seq}",
                source_seq=seq,
            ),
        )
    assert watermark_store.list_agent_ids_with_events() == ["a", "b"]
