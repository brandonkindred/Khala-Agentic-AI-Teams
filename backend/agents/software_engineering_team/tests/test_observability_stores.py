"""Unit tests for the guarded (no-Postgres) behaviour of the SE observability stores."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from software_engineering_team.shared import learnings_store, se_events, trace_store

# --- se_events -------------------------------------------------------------


def test_record_event_requires_type() -> None:
    with pytest.raises(ValueError):
        se_events.record_event("")


def test_record_event_noop_without_postgres() -> None:
    # Default test env has POSTGRES_HOST unset → guarded no-op returns False.
    assert se_events.record_event("task_created", job_id="j", task_id="t") is False


def test_fetch_events_empty_without_postgres() -> None:
    assert se_events.fetch_events_since(datetime.now(tz=timezone.utc)) == []


def test_prune_events_noop() -> None:
    assert se_events.prune_events(0) == 0
    assert se_events.prune_events(30) == 0


# --- learnings_store -------------------------------------------------------


def test_fingerprint_is_stable_and_normalized() -> None:
    a = learnings_store.fingerprint("Build  FAILED", "missing import", "qa")
    b = learnings_store.fingerprint("build failed", "Missing Import", "QA")
    assert a == b
    assert len(a) == 32
    c = learnings_store.fingerprint("build failed", "missing import", "security")
    assert c != a


def test_upsert_requires_pattern() -> None:
    with pytest.raises(ValueError):
        learnings_store.upsert_learning(pattern="   ")


def test_upsert_noop_without_postgres() -> None:
    assert learnings_store.upsert_learning(pattern="p", trigger="t", counter_measure="c") is False


def test_retrieve_empty_query_returns_empty() -> None:
    assert learnings_store.retrieve_learnings("") == []
    assert learnings_store.retrieve_learnings("   ") == []


def test_retrieve_top_n_must_be_positive() -> None:
    with pytest.raises(ValueError):
        learnings_store.retrieve_learnings("anything", top_n=0)


def test_retrieve_noop_without_postgres() -> None:
    assert learnings_store.retrieve_learnings("some spec text") == []


def test_count_and_prune_noop() -> None:
    assert learnings_store.count_learnings() == 0
    assert learnings_store.prune_learnings(0) == 0


# --- trace_store -----------------------------------------------------------


def test_trace_enabled_env(monkeypatch) -> None:
    monkeypatch.delenv("SE_TRACE_TO_POSTGRES", raising=False)
    assert trace_store._trace_enabled() is False
    monkeypatch.setenv("SE_TRACE_TO_POSTGRES", "true")
    assert trace_store._trace_enabled() is True
    monkeypatch.setenv("SE_TRACE_TO_POSTGRES", "no")
    assert trace_store._trace_enabled() is False


def test_write_trace_disabled_by_default() -> None:
    class _Rec:
        timestamp = 0.0
        team = "software_engineering"
        job_id = "j"

    assert trace_store.write_trace(_Rec()) is False


def test_fetch_cost_empty_without_postgres() -> None:
    out = trace_store.fetch_cost_since(datetime.now(tz=timezone.utc))
    assert out == {"total_cost_usd": 0.0, "by_job": {}}


def test_trace_observer_ignores_non_se(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(trace_store, "write_trace", lambda rec: calls.append(rec) or True)

    class _Rec:
        team = "blogging"
        job_id = "j"

    trace_store._trace_observer(_Rec())
    assert calls == []  # other team → not written
