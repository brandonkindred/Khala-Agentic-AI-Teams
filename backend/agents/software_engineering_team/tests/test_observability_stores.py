"""Unit tests for the guarded (no-Postgres) behaviour of the SE observability stores."""

from __future__ import annotations

from contextlib import contextmanager
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


# --- trace_store cache-token persistence (single-row + batch, no live Postgres) --------


class _FakeCursor:
    """Records every execute/executemany call; no live Postgres involved.

    Mirrors psycopg's arity check: a statement whose ``%s`` count does not match
    the row width is a hard error, not a silently-recorded call. Without this the
    fake would happily accept a row that real psycopg rejects, and since both
    write paths swallow exceptions (DEBUG log, no raise) the drift would surface
    only as silently-dropped trace rows in production.

    Prior art: ``llm_service/tests/test_usage_store.py`` carries a near-identical
    recording cursor + ``pg_cursor``-patching fixture. The shared scaffold in
    ``shared.postgres.fake`` does not cover this need — it is dispatch-table
    style, patches ``get_conn`` rather than ``pg_cursor``, and offers neither
    call recording nor raise injection — which is why both suites hand-roll a
    spy. If a third copy is ever needed, converge them into a shared recording
    cursor rather than adding another.
    """

    def __init__(self, raise_on_execute: bool = False) -> None:
        self.executed: list[tuple] = []
        self._raise = raise_on_execute

    @staticmethod
    def _check_arity(sql, params) -> None:
        expected = sql.count("%s")
        actual = len(params or ())
        assert expected == actual, f"SQL expects {expected} params, row has {actual}"

    def execute(self, sql, params=None):
        if self._raise:
            raise RuntimeError("boom")
        self._check_arity(sql, params)
        self.executed.append((sql, params))

    def executemany(self, sql, seq):
        if self._raise:
            raise RuntimeError("boom")
        rows = list(seq)
        for row in rows:
            self._check_arity(sql, row)
        self.executed.append((sql, rows))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _rec(**overrides):
    """A minimal telemetry-record stand-in; overrides layer on top of a base call."""

    class _R:
        timestamp = datetime.now(tz=timezone.utc).timestamp()
        team = "software_engineering"
        agent_key = "backend"
        job_id = "j1"
        task_id = "t1"
        phase = "execution"
        model = "m"
        prompt_tokens = 10
        completion_tokens = 5
        total_tokens = 15
        cost_usd = 0.01
        latency_ms = 100
        status = "success"
        outcome = "success"
        objective = "o"
        request_id = "r1"

    r = _R()
    for k, v in overrides.items():
        setattr(r, k, v)
    return r


class _RecNoCacheAttrs:
    """A telemetry record with no cache-token attributes at all (missing entirely,
    not just zero) — used to pin the never-raise/defaults-to-0 contract."""

    timestamp = datetime.now(tz=timezone.utc).timestamp()
    team = "software_engineering"
    job_id = "j1"


@pytest.fixture
def _fake_cursor(monkeypatch):
    """Enable tracing and swap trace_store.pg_cursor for a recording FakeCursor.

    Returns a factory: call it with no args for the default (non-raising) cursor,
    or ``raise_on_execute=True`` to get a cursor that raises on execute/executemany
    (for the never-raise-on-DB-failure path).
    """
    monkeypatch.setenv("SE_TRACE_TO_POSTGRES", "true")

    def _make(raise_on_execute: bool = False) -> _FakeCursor:
        cursor = _FakeCursor(raise_on_execute)

        @contextmanager
        def _pg_cursor(*, dict_rows: bool = False, database=None):
            yield cursor

        monkeypatch.setattr(trace_store, "pg_cursor", _pg_cursor)
        return cursor

    return _make


# INSERT param order (see trace_store._INSERT_SQL / _record_to_row):
# params[10] = cache_read_tokens, params[11] = cache_creation_tokens.


def test_insert_sql_placeholder_count_matches_row_width() -> None:
    """``_INSERT_SQL``'s ``%s`` count must equal the tuple width ``_record_to_row`` builds.

    Adding a column to the statement without a matching value (or vice versa) makes
    every real INSERT fail — and because both write paths swallow exceptions, that
    failure is invisible: trace rows are silently dropped and the cost endpoint
    reports zero. This pins the two halves of the contract against each other
    directly, so drift fails here with a legible message rather than as a
    swallowed error inside a write-path test.
    """
    assert trace_store._INSERT_SQL.count("%s") == len(trace_store._record_to_row(_rec()))


def test_write_trace_persists_cache_read_tokens(_fake_cursor) -> None:
    """write_trace (single-row path) carries cache_read_tokens through to the INSERT params."""
    cursor = _fake_cursor()
    assert trace_store.write_trace(_rec(cache_read_tokens=42, cache_creation_tokens=0)) is True
    sql, params = cursor.executed[0]
    assert "cache_read_tokens" in sql
    assert "cache_creation_tokens" in sql
    assert params[10:12] == (42, 0)


def test_write_trace_persists_cache_creation_tokens(_fake_cursor) -> None:
    """write_trace (single-row path) carries cache_creation_tokens through to the INSERT params."""
    cursor = _fake_cursor()
    assert trace_store.write_trace(_rec(cache_read_tokens=0, cache_creation_tokens=17)) is True
    sql, params = cursor.executed[0]
    assert "cache_creation_tokens" in sql
    assert params[10:12] == (0, 17)


def test_write_trace_writes_zero_for_no_cache_usage(_fake_cursor) -> None:
    """A record reporting neither cache reads nor creation writes 0 for both, never NULL."""
    cursor = _fake_cursor()
    assert trace_store.write_trace(_rec(cache_read_tokens=0, cache_creation_tokens=0)) is True
    _, params = cursor.executed[0]
    assert params[10:12] == (0, 0)


def test_write_trace_never_raises_on_missing_cache_fields(_fake_cursor) -> None:
    """The never-raise contract holds even when the record has no cache attrs at all."""
    cursor = _fake_cursor()
    assert trace_store.write_trace(_RecNoCacheAttrs()) is True
    _, params = cursor.executed[0]
    assert params[10:12] == (0, 0)


def test_write_rows_persists_cache_read_tokens_batch(_fake_cursor) -> None:
    """write_rows (batch path) carries cache_read_tokens through identically to write_trace."""
    cursor = _fake_cursor()
    row = trace_store._record_to_row(_rec(cache_read_tokens=42, cache_creation_tokens=0))
    assert trace_store.write_rows([row]) == 1
    sql, rows = cursor.executed[0]
    assert "cache_read_tokens" in sql
    assert "cache_creation_tokens" in sql
    assert rows[0][10:12] == (42, 0)


def test_write_rows_persists_cache_creation_tokens_batch(_fake_cursor) -> None:
    """write_rows (batch path) carries cache_creation_tokens through identically to write_trace."""
    cursor = _fake_cursor()
    row = trace_store._record_to_row(_rec(cache_read_tokens=0, cache_creation_tokens=17))
    assert trace_store.write_rows([row]) == 1
    sql, rows = cursor.executed[0]
    assert "cache_creation_tokens" in sql
    assert rows[0][10:12] == (0, 17)


def test_write_rows_writes_zero_for_no_cache_usage_batch(_fake_cursor) -> None:
    """write_rows (batch path) writes 0/0 for a record reporting neither, never NULL."""
    cursor = _fake_cursor()
    row = trace_store._record_to_row(_RecNoCacheAttrs())
    assert trace_store.write_rows([row]) == 1
    _, rows = cursor.executed[0]
    assert rows[0][10:12] == (0, 0)


def test_write_trace_never_raises_on_cursor_failure(_fake_cursor) -> None:
    """A DB failure on the single-row path degrades to False, never raises.

    Without this, dropping ``write_trace``'s ``except Exception`` guard would let a
    DB error propagate into the llm_service call observer with the suite still green.
    """
    cursor = _fake_cursor(raise_on_execute=True)
    assert trace_store.write_trace(_rec(cache_read_tokens=5, cache_creation_tokens=0)) is False
    assert cursor.executed == []  # the failed INSERT left nothing recorded


def test_write_rows_never_raises_on_cursor_failure(_fake_cursor) -> None:
    """A DB failure on the batch path degrades to 0, never raises (mirrors write_trace)."""
    cursor = _fake_cursor(raise_on_execute=True)
    row = trace_store._record_to_row(_rec(cache_read_tokens=5, cache_creation_tokens=0))
    assert trace_store.write_rows([row]) == 0
    # All-or-nothing: a failed batch records no partial write.
    assert cursor.executed == []
