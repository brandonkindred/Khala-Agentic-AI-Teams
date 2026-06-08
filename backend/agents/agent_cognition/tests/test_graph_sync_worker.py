"""Tests for the knowledge-graph sync worker — faked Graphiti, no live Neo4j/DB.

The worker is the first live driver of the graph layer. These tests drive it with
a fake Graphiti (recording ``add_episode`` calls) and monkeypatched store reads so
the ingestion logic, watermark advancement, and gating are covered without a
database or the ``graphiti_core`` dependency.
"""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timezone

import pytest

from agent_cognition.graph import sync_worker
from agent_cognition.memory.store import RecordedEvent
from agent_cognition.models import EventKind, MemoryEvent, PeriodSummary, Scale


@pytest.fixture(autouse=True)
def _fake_episode_type(monkeypatch):
    """Inject a fake ``graphiti_core.nodes.EpisodeType`` for the ingest imports."""
    mod = types.ModuleType("graphiti_core.nodes")

    class EpisodeType:
        text = "text"

    mod.EpisodeType = EpisodeType
    monkeypatch.setitem(sys.modules, "graphiti_core", types.ModuleType("graphiti_core"))
    monkeypatch.setitem(sys.modules, "graphiti_core.nodes", mod)
    yield


class _FakeGraphiti:
    def __init__(self):
        self.episodes: list[dict] = []

    async def add_episode(self, **kwargs):
        self.episodes.append(kwargs)


def _event(eid: str, content: str, occurred: datetime) -> MemoryEvent:
    return MemoryEvent(
        id=eid,
        agent_id="agent-1",
        kind=EventKind.OBSERVATION,
        content=content,
        occurred_at=occurred,
        source_run_id=f"run-{eid}",
        source_seq=0,
    )


def _summary(sid: str, scale: Scale, created: datetime) -> PeriodSummary:
    return PeriodSummary(
        id=sid,
        agent_id="agent-1",
        scale=scale,
        period_start=created,
        period_end=created,
        summary=f"summary {sid}",
        highlights=["h1", "h2"],
        created_at=created,
    )


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------
def test_interval_default_and_floor(monkeypatch):
    monkeypatch.delenv("AGENT_COGNITION_GRAPH_SYNC_INTERVAL_S", raising=False)
    assert sync_worker.graph_sync_interval_seconds() == 300
    monkeypatch.setenv("AGENT_COGNITION_GRAPH_SYNC_INTERVAL_S", "5")
    assert sync_worker.graph_sync_interval_seconds() == 30  # floored
    monkeypatch.setenv("AGENT_COGNITION_GRAPH_SYNC_INTERVAL_S", "garbage")
    assert sync_worker.graph_sync_interval_seconds() == 300


def test_batch_default_floor_and_garbage(monkeypatch):
    monkeypatch.delenv("AGENT_COGNITION_GRAPH_SYNC_BATCH", raising=False)
    assert sync_worker.graph_sync_batch() == 50
    monkeypatch.setenv("AGENT_COGNITION_GRAPH_SYNC_BATCH", "0")
    assert sync_worker.graph_sync_batch() == 1  # floored
    monkeypatch.setenv("AGENT_COGNITION_GRAPH_SYNC_BATCH", "nope")
    assert sync_worker.graph_sync_batch() == 50


def test_default_scope_is_both():
    assert sync_worker._agent_graph_scope("agent-1") == (True, True)


def test_render_summary_includes_highlights():
    s = _summary("s1", Scale.DAY, datetime(2026, 6, 1, tzinfo=timezone.utc))
    rendered = sync_worker._render_summary(s)
    assert "summary s1" in rendered
    assert "h1" in rendered and "h2" in rendered


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------
def test_run_graph_sync_returns_when_neo4j_disabled(monkeypatch):
    monkeypatch.delenv("NEO4J_BOLT_URL", raising=False)
    monkeypatch.setenv("POSTGRES_HOST", "postgres")
    # Should return promptly without looping or touching anything.
    asyncio.run(sync_worker.run_graph_sync())


def test_run_graph_sync_returns_when_postgres_disabled(monkeypatch):
    monkeypatch.setenv("NEO4J_BOLT_URL", "bolt://neo4j:7687")
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    asyncio.run(sync_worker.run_graph_sync())


# ---------------------------------------------------------------------------
# Event ingestion
# ---------------------------------------------------------------------------
def test_ingest_events_adds_episodes_and_advances_watermark(monkeypatch):
    t0 = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 6, 1, 11, 0, tzinfo=timezone.utc)
    rows = [
        RecordedEvent(_event("e1", "first", t0), t0),
        RecordedEvent(_event("e2", "second", t1), t1),
    ]
    monkeypatch.setattr(
        sync_worker.memory_store, "fetch_events_recorded_after", lambda *a, **k: rows
    )
    captured = {}
    monkeypatch.setattr(
        sync_worker.watermark_store,
        "upsert_watermark",
        lambda agent_id, **kw: captured.update({"agent_id": agent_id, **kw}),
    )
    graphiti = _FakeGraphiti()

    count = asyncio.run(sync_worker._ingest_events(graphiti, "agent-1", None, 50))

    assert count == 2
    assert [e["name"] for e in graphiti.episodes] == ["event:e1", "event:e2"]
    assert all(e["group_id"] == "agent-1" for e in graphiti.episodes)
    assert "[observation] first" in graphiti.episodes[0]["episode_body"]
    # Watermark advanced to the last row's (recorded_at, id) with the batch count.
    assert captured["last_event_recorded_at"] == t1
    assert captured["last_event_id"] == "e2"
    assert captured["ingested_delta"] == 2


def test_ingest_events_noop_when_empty(monkeypatch):
    monkeypatch.setattr(sync_worker.memory_store, "fetch_events_recorded_after", lambda *a, **k: [])
    called = {"upsert": False}
    monkeypatch.setattr(
        sync_worker.watermark_store,
        "upsert_watermark",
        lambda *a, **k: called.update(upsert=True),
    )
    graphiti = _FakeGraphiti()
    count = asyncio.run(sync_worker._ingest_events(graphiti, "agent-1", None, 50))
    assert count == 0
    assert graphiti.episodes == []
    assert called["upsert"] is False


def test_ingest_events_uses_existing_watermark_cursor(monkeypatch):
    t = datetime(2026, 6, 2, tzinfo=timezone.utc)
    seen = {}

    def _fetch(agent_id, *, after_recorded_at, after_id, limit):
        seen.update(after_recorded_at=after_recorded_at, after_id=after_id, limit=limit)
        return [RecordedEvent(_event("e9", "x", t), t)]

    monkeypatch.setattr(sync_worker.memory_store, "fetch_events_recorded_after", _fetch)
    monkeypatch.setattr(sync_worker.watermark_store, "upsert_watermark", lambda *a, **k: None)

    wm = types.SimpleNamespace(
        last_event_recorded_at=t,
        last_event_id="e8",
        last_summary_created_at=None,
        last_summary_id=None,
    )
    asyncio.run(sync_worker._ingest_events(_FakeGraphiti(), "agent-1", wm, 25))
    assert seen == {"after_recorded_at": t, "after_id": "e8", "limit": 25}


# ---------------------------------------------------------------------------
# Summary ingestion
# ---------------------------------------------------------------------------
def test_ingest_summaries_adds_episodes_and_advances_watermark(monkeypatch):
    c0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    c1 = datetime(2026, 6, 2, tzinfo=timezone.utc)
    summaries = [_summary("s1", Scale.DAY, c0), _summary("s2", Scale.WEEK, c1)]
    monkeypatch.setattr(
        sync_worker.memory_store, "fetch_summaries_created_after", lambda *a, **k: summaries
    )
    captured = {}
    monkeypatch.setattr(
        sync_worker.watermark_store,
        "upsert_watermark",
        lambda agent_id, **kw: captured.update(kw),
    )
    graphiti = _FakeGraphiti()
    count = asyncio.run(sync_worker._ingest_summaries(graphiti, "agent-1", None, 50))

    assert count == 2
    assert [e["name"] for e in graphiti.episodes] == ["summary:s1", "summary:s2"]
    assert graphiti.episodes[0]["source_description"] == "day rollup summary"
    assert captured["last_summary_created_at"] == c1
    assert captured["last_summary_id"] == "s2"
    assert captured["ingested_delta"] == 2


def test_ingest_summaries_noop_when_empty(monkeypatch):
    monkeypatch.setattr(
        sync_worker.memory_store, "fetch_summaries_created_after", lambda *a, **k: []
    )
    upserts = []
    monkeypatch.setattr(
        sync_worker.watermark_store, "upsert_watermark", lambda *a, **k: upserts.append(k)
    )
    count = asyncio.run(sync_worker._ingest_summaries(_FakeGraphiti(), "agent-1", None, 50))
    assert count == 0
    assert upserts == []


# ---------------------------------------------------------------------------
# Per-agent + full pass orchestration
# ---------------------------------------------------------------------------
def test_sync_one_agent_combines_events_and_summaries(monkeypatch):
    monkeypatch.setattr(sync_worker.watermark_store, "get_watermark", lambda a: None)

    async def _fake_events(g, a, w, b):
        return 3

    async def _fake_summaries(g, a, w, b):
        return 2

    monkeypatch.setattr(sync_worker, "_ingest_events", _fake_events)
    monkeypatch.setattr(sync_worker, "_ingest_summaries", _fake_summaries)
    total = asyncio.run(sync_worker._sync_one_agent(_FakeGraphiti(), "agent-1", 50))
    assert total == 5


def test_sync_one_agent_honors_scope(monkeypatch):
    monkeypatch.setattr(sync_worker.watermark_store, "get_watermark", lambda a: None)
    monkeypatch.setattr(sync_worker, "_agent_graph_scope", lambda a: (False, True))

    async def _boom_events(*a, **k):
        raise AssertionError("events should be skipped")

    async def _fake_summaries(g, a, w, b):
        return 7

    monkeypatch.setattr(sync_worker, "_ingest_events", _boom_events)
    monkeypatch.setattr(sync_worker, "_ingest_summaries", _fake_summaries)
    total = asyncio.run(sync_worker._sync_one_agent(_FakeGraphiti(), "agent-1", 50))
    assert total == 7


def test_sync_once_iterates_agents(monkeypatch):
    monkeypatch.setattr(
        sync_worker.watermark_store, "list_agent_ids_with_events", lambda: ["a", "b", "c"]
    )
    graphiti = _FakeGraphiti()
    monkeypatch.setattr(sync_worker, "get_graphiti", lambda: graphiti)

    async def _fake_one(g, agent_id, batch):
        assert g is graphiti
        return 1

    monkeypatch.setattr(sync_worker, "_sync_one_agent", _fake_one)
    total = asyncio.run(sync_worker._sync_once(50))
    assert total == 3


# ---------------------------------------------------------------------------
# run_graph_sync loop body (enabled path)
# ---------------------------------------------------------------------------
def _enable_backends(monkeypatch):
    monkeypatch.setenv("NEO4J_BOLT_URL", "bolt://neo4j:7687")
    monkeypatch.setenv("POSTGRES_HOST", "postgres")

    async def _noop_indices():
        return True

    monkeypatch.setattr(sync_worker, "register_graph_indices", _noop_indices)


def _drive_loop(monkeypatch, sync_once_impl):
    """Run run_graph_sync(interval_s=0) until sync_once_impl cancels it."""
    monkeypatch.setattr(sync_worker, "_sync_once", sync_once_impl)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(sync_worker.run_graph_sync(interval_s=0, batch=5))


def test_run_loop_runs_iteration_then_logs_and_cancels(monkeypatch):
    _enable_backends(monkeypatch)
    calls = {"n": 0}

    async def _sync_once(batch):
        assert batch == 5
        calls["n"] += 1
        if calls["n"] >= 2:
            raise asyncio.CancelledError
        return 4  # non-zero → exercises the "ingested" log line

    _drive_loop(monkeypatch, _sync_once)
    assert calls["n"] == 2


def test_run_loop_swallows_storage_unavailable(monkeypatch):
    _enable_backends(monkeypatch)
    calls = {"n": 0}

    async def _sync_once(batch):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sync_worker.AgentCognitionStorageUnavailable("down")
        raise asyncio.CancelledError

    _drive_loop(monkeypatch, _sync_once)
    assert calls["n"] == 2  # continued past the storage outage


def test_run_loop_swallows_generic_exception(monkeypatch):
    _enable_backends(monkeypatch)
    calls = {"n": 0}

    async def _sync_once(batch):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("boom")
        raise asyncio.CancelledError

    _drive_loop(monkeypatch, _sync_once)
    assert calls["n"] == 2  # continued past the unexpected error


def test_run_loop_continues_when_index_build_fails(monkeypatch):
    _enable_backends(monkeypatch)

    async def _boom_indices():
        raise RuntimeError("no neo4j yet")

    monkeypatch.setattr(sync_worker, "register_graph_indices", _boom_indices)

    async def _sync_once(batch):
        raise asyncio.CancelledError

    _drive_loop(monkeypatch, _sync_once)
