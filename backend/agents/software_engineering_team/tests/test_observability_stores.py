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

# Positions of the two cache-token columns in ``_INSERT_SQL`` / the tuple
# ``_record_to_row`` builds. Every cache-token assertion in this module reads
# through these (via :func:`_cache_tokens`) rather than slicing by literal, so a
# column reorder is a one-line change here — and
# ``test_insert_sql_pins_cache_column_positions`` fails loudly if these drift
# from what the statement actually declares.
_CACHE_READ_IDX = 10
_CACHE_CREATION_IDX = 11


def _cache_tokens(row) -> tuple:
    """The ``(cache_read_tokens, cache_creation_tokens)`` pair read from ``row``.

    Preconditions:
        ``row`` is a positional row tuple in ``_INSERT_SQL`` column order — one
        built by ``trace_store._record_to_row``, or the params of a recorded
        INSERT.
    Postconditions:
        Returns the two cache-token values at the pinned indices, in
        (read, creation) order. Indexes each column independently, so the pair
        does not assume the two columns stay adjacent.
    """
    return (row[_CACHE_READ_IDX], row[_CACHE_CREATION_IDX])


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

    assert _cache_tokens(row_read) == (42, 0)
    assert _cache_tokens(row_creation) == (0, 17)
    assert _cache_tokens(row_neither) == (0, 0)  # missing attrs -> 0, never NULL


def test_trace_retention_days_env(monkeypatch) -> None:
    """_retention_days defaults to 30, parses overrides, and falls back on garbage."""
    monkeypatch.delenv("SE_TRACE_RETENTION_DAYS", raising=False)
    assert trace_store._retention_days() == 30.0
    monkeypatch.setenv("SE_TRACE_RETENTION_DAYS", "7")
    assert trace_store._retention_days() == 7.0
    monkeypatch.setenv("SE_TRACE_RETENTION_DAYS", "garbage")
    assert trace_store._retention_days() == 30.0  # bad value → default


# --- trace_store cache-token persistence (single-row + batch, no live Postgres) --------


class _FakeCursorContractViolation(BaseException):
    """Raised when a statement/row pair would be rejected by real psycopg.

    Deliberately derives from ``BaseException``, not ``Exception``: both write
    paths wrap their cursor work in ``except Exception`` (DEBUG log, no raise),
    so an ``Exception`` raised by the fake would be swallowed by the code under
    test and resurface as an opaque ``IndexError`` on ``cursor.executed[0]``.
    Deriving from ``BaseException`` lets the violation propagate to pytest with
    its own message intact.
    """


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
        """Construct an empty recording cursor.

        Preconditions:
            None.
        Postconditions:
            ``self.executed`` is an empty list. ``self._raise`` is
            ``raise_on_execute`` — when true, every subsequent ``execute``/
            ``executemany`` call raises ``RuntimeError`` instead of recording.
        """
        self.executed: list[tuple] = []
        self._raise = raise_on_execute

    @staticmethod
    def _check_arity(sql: str, params, expected: int) -> None:
        """Reject a ``params``/row whose length does not match ``sql``'s own ``%s`` count.

        Preconditions:
            ``expected`` is ``sql.count("%s")``, computed once by the caller — passed
            in rather than recomputed here so a caller iterating many rows against
            the same ``sql`` (``executemany``) does the ``str.count`` scan once.
        Postconditions:
            Returns ``None`` when ``len(params or ())`` equals ``expected``.
        Raises:
            ``_FakeCursorContractViolation`` — deliberately a ``BaseException``, not an
            ``Exception`` — when the lengths differ, naming both counts.
        """
        actual = len(params or ())
        if expected != actual:
            raise _FakeCursorContractViolation(f"SQL expects {expected} params, row has {actual}")

    def execute(self, sql: str, params=None) -> None:
        """Record one ``(sql, params)`` call, or raise if ``raise_on_execute`` is set.

        Preconditions:
            ``params`` is ``None`` or a sequence whose length matches ``sql``'s ``%s``
            placeholder count.
        Postconditions:
            When not configured to raise, appends ``(sql, params)`` to ``self.executed``
            and returns ``None``.
        Raises:
            ``RuntimeError`` when ``raise_on_execute`` was set at construction, before
            any arity check or recording. ``_FakeCursorContractViolation`` when
            ``params``'s length does not match ``sql``'s placeholder count.
        """
        if self._raise:
            raise RuntimeError("boom")
        self._check_arity(sql, params, sql.count("%s"))
        self.executed.append((sql, params))

    def executemany(self, sql: str, seq) -> None:
        """Record one ``(sql, rows)`` call for a batch, or raise if ``raise_on_execute`` is set.

        Preconditions:
            Every row in ``seq`` is a sequence whose length matches ``sql``'s ``%s``
            placeholder count.
        Postconditions:
            When not configured to raise, appends ``(sql, list(seq))`` to
            ``self.executed`` and returns ``None``. ``sql.count("%s")`` is computed
            once and reused across every row in ``seq``, since it is invariant for a
            single call.
        Raises:
            ``RuntimeError`` when ``raise_on_execute`` was set at construction, before
            any row is checked or recorded. ``_FakeCursorContractViolation`` on the
            first row whose length does not match ``sql``'s placeholder count.
        """
        if self._raise:
            raise RuntimeError("boom")
        expected = sql.count("%s")
        rows = list(seq)
        for row in rows:
            self._check_arity(sql, row, expected)
        self.executed.append((sql, rows))


def _rec(**overrides):
    """A minimal telemetry-record stand-in; overrides layer on top of a base call.

    Preconditions:
        Every key in ``overrides`` names an attribute ``_R`` already defines below
        — this is a plain ``setattr``, not a schema, so a misspelled key silently
        adds an unused attribute rather than overriding the intended one.
    Postconditions:
        Returns an object exposing every ``_R`` class attribute (the
        :class:`llm_service.telemetry.LLMCallRecord` fields ``_record_to_row``
        reads), with any ``overrides`` values applied on top.
    """

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

    Preconditions:
        ``monkeypatch`` is the pytest fixture — the substitution it installs is
        undone automatically at test teardown.
    Postconditions:
        ``SE_TRACE_TO_POSTGRES`` is set for the duration of the test, and
        ``trace_store.pg_cursor`` is replaced. Returns a factory: call it with no
        args for the default (non-raising) cursor, or ``raise_on_execute=True``
        for a cursor that raises on ``execute``/``executemany`` instead of
        recording (for the never-raise-on-DB-failure path). Each call to the
        factory installs a fresh cursor and re-patches ``pg_cursor`` to yield it.
    """
    monkeypatch.setenv("SE_TRACE_TO_POSTGRES", "true")

    def _make(raise_on_execute: bool = False) -> _FakeCursor:
        """Install a fresh ``_FakeCursor`` as ``trace_store.pg_cursor`` and return it.

        Preconditions:
            None beyond the enclosing fixture's.
        Postconditions:
            ``trace_store.pg_cursor`` is patched to a context manager matching the
            real ``pg_cursor(*, dict_rows=False, database=None)`` signature that
            yields the returned cursor. Calling it again replaces the patch with a
            new cursor — the two do not share call history.
        """
        cursor = _FakeCursor(raise_on_execute)

        @contextmanager
        def _pg_cursor(*, dict_rows: bool = False, database=None):
            """Stand-in for ``shared.postgres.pg_cursor``; yields the fake cursor.

            Preconditions:
                Signature must track the real ``pg_cursor`` — a keyword-only
                ``dict_rows`` and ``database``, both with matching defaults — so
                this fake stays a valid substitute if the real one's callers change
                how they invoke it.
            Postconditions:
                Yields ``cursor`` unconditionally; both parameters are accepted but
                unused, since the fake never distinguishes row-factory mode.
            """
            yield cursor

        monkeypatch.setattr(trace_store, "pg_cursor", _pg_cursor)
        return cursor

    return _make


def _insert_columns() -> list[str]:
    """The column names of ``_INSERT_SQL``, in statement order.

    Preconditions:
        ``_INSERT_SQL`` is a single ``INSERT INTO <table> (<cols>) VALUES (...)``
        statement whose first parenthesised group is the column list.
    Postconditions:
        Returns the column names, stripped, in the order the statement declares
        them — the order Postgres binds positional params to.
    """
    columns = trace_store._INSERT_SQL.split("(", 1)[1].split(")", 1)[0]
    return [c.strip() for c in columns.split(",")]


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


def test_insert_sql_pins_cache_column_positions() -> None:
    """The cache columns must sit at the positions the value assertions index.

    Every other test reads cache tokens by *position* (``_CACHE_READ_IDX`` /
    ``_CACHE_CREATION_IDX``, via :func:`_cache_tokens`) and checks only that the
    column names appear somewhere in the statement. That pair of assertions cannot
    see a reordering: swapping ``cache_read_tokens`` and ``cache_creation_tokens``
    in the column list leaves the params where they are, so Postgres would write
    each value into the other column while the value assertions stay green. This
    test is the sole tie between those index constants and the statement's own
    column list — it is what makes reading by index safe everywhere else.
    """
    columns = _insert_columns()
    assert len(columns) == trace_store._INSERT_SQL.count("%s")
    assert columns[_CACHE_READ_IDX] == "cache_read_tokens"
    assert columns[_CACHE_CREATION_IDX] == "cache_creation_tokens"


def test_write_trace_persists_cache_read_tokens(_fake_cursor) -> None:
    """write_trace (single-row path) carries cache_read_tokens through to the INSERT params."""
    cursor = _fake_cursor()
    assert trace_store.write_trace(_rec(cache_read_tokens=42, cache_creation_tokens=0)) is True
    sql, params = cursor.executed[0]
    assert "cache_read_tokens" in sql
    assert "cache_creation_tokens" in sql
    assert _cache_tokens(params) == (42, 0)


def test_write_trace_persists_cache_creation_tokens(_fake_cursor) -> None:
    """write_trace (single-row path) carries cache_creation_tokens through to the INSERT params."""
    cursor = _fake_cursor()
    assert trace_store.write_trace(_rec(cache_read_tokens=0, cache_creation_tokens=17)) is True
    sql, params = cursor.executed[0]
    assert "cache_creation_tokens" in sql
    assert _cache_tokens(params) == (0, 17)


def test_write_trace_writes_zero_for_no_cache_usage(_fake_cursor) -> None:
    """A record reporting neither cache reads nor creation writes 0 for both, never NULL."""
    cursor = _fake_cursor()
    assert trace_store.write_trace(_rec(cache_read_tokens=0, cache_creation_tokens=0)) is True
    _, params = cursor.executed[0]
    assert _cache_tokens(params) == (0, 0)


def test_write_trace_never_raises_on_missing_cache_fields(_fake_cursor) -> None:
    """The never-raise contract holds even when the record has no cache attrs at all."""
    cursor = _fake_cursor()
    assert trace_store.write_trace(_RecNoCacheAttrs()) is True
    _, params = cursor.executed[0]
    assert _cache_tokens(params) == (0, 0)


def test_write_rows_persists_cache_read_tokens_batch(_fake_cursor) -> None:
    """write_rows (batch path) carries cache_read_tokens through identically to write_trace."""
    cursor = _fake_cursor()
    row = trace_store._record_to_row(_rec(cache_read_tokens=42, cache_creation_tokens=0))
    assert trace_store.write_rows([row]) == 1
    sql, rows = cursor.executed[0]
    assert "cache_read_tokens" in sql
    assert "cache_creation_tokens" in sql
    assert _cache_tokens(rows[0]) == (42, 0)


def test_write_rows_persists_cache_creation_tokens_batch(_fake_cursor) -> None:
    """write_rows (batch path) carries cache_creation_tokens through identically to write_trace."""
    cursor = _fake_cursor()
    row = trace_store._record_to_row(_rec(cache_read_tokens=0, cache_creation_tokens=17))
    assert trace_store.write_rows([row]) == 1
    sql, rows = cursor.executed[0]
    assert "cache_creation_tokens" in sql
    assert _cache_tokens(rows[0]) == (0, 17)


def test_write_rows_writes_zero_for_no_cache_usage_batch(_fake_cursor) -> None:
    """write_rows (batch path) writes 0/0 for a record reporting neither, never NULL."""
    cursor = _fake_cursor()
    row = trace_store._record_to_row(_RecNoCacheAttrs())
    assert trace_store.write_rows([row]) == 1
    _, rows = cursor.executed[0]
    assert _cache_tokens(rows[0]) == (0, 0)


def test_write_trace_never_raises_on_cursor_failure(_fake_cursor) -> None:
    """A DB failure on the single-row path degrades to False, never raises.

    The return value is the whole contract here: dropping ``write_trace``'s
    ``except Exception`` guard lets the cursor's error propagate, which fails this
    test outright, and a write that somehow succeeded would return True. (Real
    atomicity is Postgres's, not something this in-memory double can attest to.)
    """
    _fake_cursor(raise_on_execute=True)
    assert trace_store.write_trace(_rec(cache_read_tokens=5, cache_creation_tokens=0)) is False


def test_write_rows_never_raises_on_cursor_failure(_fake_cursor) -> None:
    """A DB failure on the batch path degrades to 0, never raises (mirrors write_trace)."""
    _fake_cursor(raise_on_execute=True)
    row = trace_store._record_to_row(_rec(cache_read_tokens=5, cache_creation_tokens=0))
    assert trace_store.write_rows([row]) == 0
