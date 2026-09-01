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


def test_upsert_batch_empty_is_noop() -> None:
    """upsert_learnings_batch short-circuits to 0 for an empty entry list."""
    assert learnings_store.upsert_learnings_batch([]) == 0


def test_upsert_batch_rejects_blank_pattern() -> None:
    """upsert_learnings_batch rejects a blank pattern anywhere in the batch."""
    entries = [
        learnings_store.LearningEntry(pattern="ok"),
        learnings_store.LearningEntry(pattern="   "),
    ]
    with pytest.raises(ValueError):
        learnings_store.upsert_learnings_batch(entries)


def test_upsert_batch_noop_without_postgres() -> None:
    """upsert_learnings_batch is a no-op returning 0 when Postgres is unconfigured."""
    entries = [
        learnings_store.LearningEntry(pattern="p1"),
        learnings_store.LearningEntry(pattern="p2"),
    ]
    assert learnings_store.upsert_learnings_batch(entries) == 0


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
    """_trace_enabled defaults to True (unset) and follows explicit SE_TRACE_TO_POSTGRES overrides."""
    monkeypatch.delenv("SE_TRACE_TO_POSTGRES", raising=False)
    assert trace_store._trace_enabled() is True
    monkeypatch.setenv("SE_TRACE_TO_POSTGRES", "true")
    assert trace_store._trace_enabled() is True
    monkeypatch.setenv("SE_TRACE_TO_POSTGRES", "no")
    assert trace_store._trace_enabled() is False


class _TraceRec:
    """Minimal write_trace-shaped stub shared by the tests below."""

    timestamp = 0.0
    team = "software_engineering"
    job_id = "j"


def test_write_trace_noop_without_postgres_when_enabled_by_default(monkeypatch) -> None:
    """write_trace returns False when Postgres is unconfigured, even though the sink is
    enabled by default (unset SE_TRACE_TO_POSTGRES) — pg_cursor yields no cursor."""
    monkeypatch.delenv("SE_TRACE_TO_POSTGRES", raising=False)
    monkeypatch.delenv(
        "POSTGRES_HOST", raising=False
    )  # force the documented "Postgres disabled" no-op path

    assert trace_store.write_trace(_TraceRec()) is False


def test_write_trace_disabled_explicitly(monkeypatch) -> None:
    """write_trace returns False when the sink is explicitly opted out."""
    monkeypatch.setenv("SE_TRACE_TO_POSTGRES", "false")

    assert trace_store.write_trace(_TraceRec()) is False


def test_fetch_cost_empty_without_postgres() -> None:
    """fetch_cost_since returns a zeroed summary when Postgres is unconfigured."""
    out = trace_store.fetch_cost_since(datetime.now(tz=timezone.utc))
    assert out == {"total_cost_usd": 0.0, "by_job": {}}


def test_trace_observer_ignores_non_se(monkeypatch) -> None:
    """The trace observer does not enqueue traces for non-SE teams."""
    from software_engineering_team.shared import trace_flusher

    trace_flusher._reset_for_test()

    class _Rec:
        team = "blogging"
        job_id = "j"

    trace_flusher._trace_observer(_Rec())
    assert trace_flusher._buffer_size() == 0  # other team → not enqueued
    trace_flusher._reset_for_test()


def test_record_to_row_cache_tokens() -> None:
    """_record_to_row carries cache_read/cache_creation tokens, defaulting to 0 not NULL."""

    class _RecWithRead:
        timestamp = 0.0
        team = "software_engineering"
        job_id = "j"
        cache_read_tokens = 42
        cache_creation_tokens = 0

    class _RecWithCreation:
        timestamp = 0.0
        team = "software_engineering"
        job_id = "j"
        cache_read_tokens = 0
        cache_creation_tokens = 17

    class _RecWithNeither:
        timestamp = 0.0
        team = "software_engineering"
        job_id = "j"

    row_read = trace_store._record_to_row(_RecWithRead())
    row_creation = trace_store._record_to_row(_RecWithCreation())
    row_neither = trace_store._record_to_row(_RecWithNeither())

    # Column order: ..., total_tokens, cache_read_tokens, cache_creation_tokens, cost_usd, ...
    assert row_read[10:12] == (42, 0)
    assert row_creation[10:12] == (0, 17)
    assert row_neither[10:12] == (0, 0)  # missing attrs -> 0, never NULL


def test_trace_retention_days_env(monkeypatch) -> None:
    """_retention_days defaults to 30, parses overrides, and falls back on garbage."""
    monkeypatch.delenv("SE_TRACE_RETENTION_DAYS", raising=False)
    assert trace_store._retention_days() == 30.0
    monkeypatch.setenv("SE_TRACE_RETENTION_DAYS", "7")
    assert trace_store._retention_days() == 7.0
    monkeypatch.setenv("SE_TRACE_RETENTION_DAYS", "garbage")
    assert trace_store._retention_days() == 30.0  # bad value → default
