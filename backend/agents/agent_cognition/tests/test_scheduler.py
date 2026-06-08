"""Tests for the cognition scheduler — no live Postgres, all store calls faked."""

from __future__ import annotations

import asyncio

import pytest

from agent_cognition import scheduler


# ---------------------------------------------------------------------------
# Env + scope helpers
# ---------------------------------------------------------------------------
def test_interval_default_floor_garbage(monkeypatch):
    monkeypatch.delenv("AGENT_COGNITION_SCHEDULER_INTERVAL_S", raising=False)
    assert scheduler.scheduler_interval_seconds() == 3600
    monkeypatch.setenv("AGENT_COGNITION_SCHEDULER_INTERVAL_S", "5")
    assert scheduler.scheduler_interval_seconds() == 60  # floored
    monkeypatch.setenv("AGENT_COGNITION_SCHEDULER_INTERVAL_S", "junk")
    assert scheduler.scheduler_interval_seconds() == 3600


def test_default_retention_days():
    assert scheduler._agent_retention_days("a") == 90


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------
def test_returns_when_postgres_disabled(monkeypatch):
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    asyncio.run(scheduler.run_cognition_scheduler())  # returns immediately


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
def test_run_one_agent_sequences_pipeline(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(
        scheduler.rollup, "ensure_rollups_current", lambda a, now: order.append(f"rollup:{a}")
    )
    monkeypatch.setattr(
        scheduler.reflection, "reflect", lambda a, now: order.append(f"reflect:{a}")
    )
    monkeypatch.setattr(
        scheduler.store, "prune_events", lambda a, days: order.append(f"prune:{a}:{days}")
    )
    asyncio.run(scheduler._run_one_agent("agent-1"))
    assert order == ["rollup:agent-1", "reflect:agent-1", "prune:agent-1:90"]


def test_run_once_iterates_and_isolates_failures(monkeypatch):
    monkeypatch.setattr(
        scheduler.watermark_store, "list_agent_ids_with_events", lambda: ["a", "b", "c"]
    )
    handled: list[str] = []

    async def _one(agent_id):
        if agent_id == "b":
            raise ValueError("b is broken")
        handled.append(agent_id)

    monkeypatch.setattr(scheduler, "_run_one_agent", _one)
    asyncio.run(scheduler._run_once())
    # a and c still processed despite b failing.
    assert handled == ["a", "c"]


# ---------------------------------------------------------------------------
# Loop body
# ---------------------------------------------------------------------------
def _drive(monkeypatch, run_once_impl):
    monkeypatch.setenv("POSTGRES_HOST", "postgres")
    monkeypatch.setattr(scheduler, "_run_once", run_once_impl)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scheduler.run_cognition_scheduler(interval_s=0))


def test_loop_runs_then_cancels(monkeypatch):
    calls = {"n": 0}

    async def _run_once():
        calls["n"] += 1
        if calls["n"] >= 2:
            raise asyncio.CancelledError

    _drive(monkeypatch, _run_once)
    assert calls["n"] == 2


def test_loop_swallows_storage_unavailable(monkeypatch):
    calls = {"n": 0}

    async def _run_once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise scheduler.AgentCognitionStorageUnavailable("down")
        raise asyncio.CancelledError

    _drive(monkeypatch, _run_once)
    assert calls["n"] == 2


def test_loop_swallows_generic_error(monkeypatch):
    calls = {"n": 0}

    async def _run_once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("boom")
        raise asyncio.CancelledError

    _drive(monkeypatch, _run_once)
    assert calls["n"] == 2
