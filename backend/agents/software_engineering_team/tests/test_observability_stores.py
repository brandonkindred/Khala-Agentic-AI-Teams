"""Unit tests for the guarded (no-Postgres) behaviour of the SE observability stores."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from software_engineering_team.shared import learnings_store, se_events, trace_store

# --- se_events -------------------------------------------------------------


def test_record_event_requires_type() -> None:
    """record_event rejects an empty event type."""
    with pytest.raises(ValueError):
        se_events.record_event("")


def test_record_event_noop_without_postgres() -> None:
    """record_event is a guarded no-op returning False when Postgres is unconfigured."""
    # Default test env has POSTGRES_HOST unset → guarded no-op returns False.
    assert se_events.record_event("task_created", job_id="j", task_id="t") is False


def test_fetch_events_empty_without_postgres() -> None:
    """fetch_events_since returns an empty list when Postgres is unconfigured."""
    assert se_events.fetch_events_since(datetime.now(tz=timezone.utc)) == []


def test_prune_events_noop() -> None:
    """prune_events returns 0 when Postgres is unconfigured."""
    assert se_events.prune_events(0) == 0
    assert se_events.prune_events(30) == 0


# --- learnings_store -------------------------------------------------------


def test_fingerprint_is_stable_and_normalized() -> None:
    """fingerprint is case/whitespace-normalized, 32 chars, and category-sensitive."""
    a = learnings_store.fingerprint("Build  FAILED", "missing import", "qa")
    b = learnings_store.fingerprint("build failed", "Missing Import", "QA")
    assert a == b
    assert len(a) == 32
    c = learnings_store.fingerprint("build failed", "missing import", "security")
    assert c != a


def test_upsert_requires_pattern() -> None:
    """upsert_learning rejects a blank pattern."""
    with pytest.raises(ValueError):
        learnings_store.upsert_learning(pattern="   ")


def test_upsert_noop_without_postgres() -> None:
    """upsert_learning is a no-op returning False when Postgres is unconfigured."""
    assert learnings_store.upsert_learning(pattern="p", trigger="t", counter_measure="c") is False


def test_retrieve_empty_query_returns_empty() -> None:
    """retrieve_learnings returns an empty list for a blank query."""
    assert learnings_store.retrieve_learnings("") == []
    assert learnings_store.retrieve_learnings("   ") == []


def test_retrieve_top_n_must_be_positive() -> None:
    """retrieve_learnings rejects a non-positive top_n."""
    with pytest.raises(ValueError):
        learnings_store.retrieve_learnings("anything", top_n=0)


def test_retrieve_noop_without_postgres() -> None:
    """retrieve_learnings returns an empty list when Postgres is unconfigured."""
    assert learnings_store.retrieve_learnings("some spec text") == []


def test_count_and_prune_noop() -> None:
    """count_learnings and prune_learnings return 0 when Postgres is unconfigured."""
    assert learnings_store.count_learnings() == 0
    assert learnings_store.prune_learnings(0) == 0


def test_or_tsquery_terms_builds_or_query() -> None:
    """_or_tsquery_terms lowercases, strips, dedups, drops short words, and OR-joins terms."""
    f = learnings_store._or_tsquery_terms
    # Empty / whitespace / all-too-short → empty string.
    assert f("") == ""
    assert f("   ") == ""
    assert f("a b cd") == ""  # every term < 3 chars
    # Lowercased, special chars stripped, de-duplicated (order preserved), OR-joined.
    assert f("Foo foo BAR!") == "foo | bar"
    assert f("special!char @# test") == "special | char | test"
    # Words shorter than 3 chars are dropped; digits are kept.
    assert f("the api v2 gateway") == "the | api | gateway"
    # The term limit caps how many are emitted.
    assert f("aaa bbb ccc ddd", limit=2) == "aaa | bbb"


def test_learnings_retention_days_env(monkeypatch) -> None:
    """_retention_days defaults to 365, parses overrides, and clamps garbage and negatives."""
    monkeypatch.delenv("SE_LEARNINGS_RETENTION_DAYS", raising=False)
    assert learnings_store._retention_days() == 365.0
    monkeypatch.setenv("SE_LEARNINGS_RETENTION_DAYS", "10")
    assert learnings_store._retention_days() == 10.0
    monkeypatch.setenv("SE_LEARNINGS_RETENTION_DAYS", "garbage")
    assert learnings_store._retention_days() == 365.0  # bad value → default
    monkeypatch.setenv("SE_LEARNINGS_RETENTION_DAYS", "-5")
    assert learnings_store._retention_days() == 0.0  # clamped to floor


# --- trace_store -----------------------------------------------------------


def test_trace_enabled_env(monkeypatch) -> None:
    """_trace_enabled defaults to False and follows the SE_TRACE_TO_POSTGRES flag."""
    monkeypatch.delenv("SE_TRACE_TO_POSTGRES", raising=False)
    assert trace_store._trace_enabled() is False
    monkeypatch.setenv("SE_TRACE_TO_POSTGRES", "true")
    assert trace_store._trace_enabled() is True
    monkeypatch.setenv("SE_TRACE_TO_POSTGRES", "no")
    assert trace_store._trace_enabled() is False


def test_write_trace_disabled_by_default() -> None:
    """write_trace returns False when tracing is disabled by default."""

    class _Rec:
        timestamp = 0.0
        team = "software_engineering"
        job_id = "j"

    assert trace_store.write_trace(_Rec()) is False


def test_fetch_cost_empty_without_postgres() -> None:
    """fetch_cost_since returns a zeroed summary when Postgres is unconfigured."""
    out = trace_store.fetch_cost_since(datetime.now(tz=timezone.utc))
    assert out == {"total_cost_usd": 0.0, "by_job": {}}


def test_trace_observer_ignores_non_se(monkeypatch) -> None:
    """The trace observer does not write traces for non-SE teams."""
    calls: list = []
    monkeypatch.setattr(trace_store, "write_trace", lambda rec: calls.append(rec) or True)

    class _Rec:
        team = "blogging"
        job_id = "j"

    trace_store._trace_observer(_Rec())
    assert calls == []  # other team → not written


def test_trace_retention_days_env(monkeypatch) -> None:
    """_retention_days defaults to 30, parses overrides, and falls back on garbage."""
    monkeypatch.delenv("SE_TRACE_RETENTION_DAYS", raising=False)
    assert trace_store._retention_days() == 30.0
    monkeypatch.setenv("SE_TRACE_RETENTION_DAYS", "7")
    assert trace_store._retention_days() == 7.0
    monkeypatch.setenv("SE_TRACE_RETENTION_DAYS", "garbage")
    assert trace_store._retention_days() == 30.0  # bad value → default
