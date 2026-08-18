"""Unit tests for llm_service.usage_store (FakeCursor, no live Postgres)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import pytest

from llm_service import usage_store as us


class FakeCursor:
    def __init__(self, fetchone_rows=None, fetchall_rows=None, raise_on_execute=False) -> None:
        self.executed: list[tuple] = []
        self._fetchone = list(fetchone_rows or [])
        self._fetchall = fetchall_rows if fetchall_rows is not None else []
        self._raise = raise_on_execute

    def execute(self, sql, params=None):
        if self._raise:
            raise RuntimeError("boom")
        self.executed.append((sql, params))

    def executemany(self, sql, seq):
        if self._raise:
            raise RuntimeError("boom")
        self.executed.append((sql, list(seq)))

    def fetchone(self):
        return self._fetchone.pop(0) if self._fetchone else None

    def fetchall(self):
        if self._fetchall and isinstance(self._fetchall[0], list):
            return self._fetchall.pop(0)
        return self._fetchall

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Rec:
    def __init__(self, **overrides: Any) -> None:
        self.timestamp = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc).timestamp()
        self.team = "blogging"
        self.agent_key = "writer"
        self.model = "claude-opus-4-8"
        self.prompt_tokens = 10
        self.completion_tokens = 5
        self.total_tokens = 15
        self.status = "success"
        for k, v in overrides.items():
            setattr(self, k, v)


@pytest.fixture
def fake_db(monkeypatch):
    cursor = FakeCursor()

    @contextmanager
    def _pg_cursor(*, dict_rows: bool = False, database=None):
        yield cursor

    monkeypatch.setattr(us, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(us, "pg_cursor", _pg_cursor)
    monkeypatch.setattr(us, "_table_ensured", True)
    return cursor


def test_window_hours_presets() -> None:
    assert us.window_hours("24h") == 24.0
    assert us.window_hours("7d") == 168.0
    assert us.window_hours("30d") == 720.0
    assert us.window_hours("all") == 0.0
    with pytest.raises(ValueError, match="unknown window"):
        us.window_hours("1h")


def test_window_hours_accepts_numeric_hours() -> None:
    """Pre-change GET /api/llm-usage took window as hours (e.g. 1.0)."""
    assert us.window_hours("1.0") == 1.0
    assert us.window_hours("24") == 24.0
    assert us.window_hours("0") == 0.0
    assert us.window_is_unbounded("all") is True
    assert us.window_is_unbounded("0") is False
    assert us.window_is_unbounded("0.0") is False
    with pytest.raises(ValueError, match="unknown window"):
        us.window_hours("-1")
    with pytest.raises(ValueError, match="unknown window"):
        us.window_hours("inf")
    with pytest.raises(ValueError, match="unknown window"):
        us.window_hours("1e308")
    with pytest.raises(ValueError, match="unknown window"):
        us.window_hours("1e20")


def test_write_rows_noop_when_postgres_off(monkeypatch) -> None:
    monkeypatch.setattr(us, "is_postgres_enabled", lambda: False)

    @contextmanager
    def _none_cursor(*, dict_rows: bool = False, database=None):
        yield None

    monkeypatch.setattr(us, "pg_cursor", _none_cursor)
    assert us.write_rows([us.record_to_row(_Rec())]) == 0


def test_write_rows_inserts_tuple(fake_db) -> None:
    rec = _Rec()
    row = us.record_to_row(rec)
    assert len(row) == 20
    assert row[1] == "blogging"
    assert row[4] == 10
    assert row[5] == 5
    assert row[6] == 15
    assert row[7] == 0
    assert row[9] == ""
    assert row[10] == 0.0
    assert row[11] == ""
    assert row[18] == 0  # cache_read_tokens defaults to 0 when absent from the record
    assert row[19] == 0  # cache_creation_tokens defaults to 0 when absent from the record
    n = us.write_rows([row])
    assert n == 1
    sql, params = fake_db.executed[0]
    assert "INSERT INTO llm_call_records" in sql
    assert params == [row]


def test_fetch_summary_uses_one_snapshot_query(fake_db) -> None:
    """Totals, by_model, and by_agent must come from one statement.

    Separate SELECTs under READ COMMITTED can see different snapshots if the
    flusher commits between them (total_calls then disagreeing with
    sum(by_model[*].calls)). GROUPING SETS keeps one snapshot.
    """
    fake_db._fetchall = [
        {
            "bucket": "total",
            "model": None,
            "agent_key": None,
            "calls": 2,
            "prompt_tokens": 30,
            "completion_tokens": 10,
            "total_tokens": 40,
            "error_count": 1,
            "avg_latency_ms": 12.5,
        },
        {
            "bucket": "model",
            "model": "claude-opus-4-8",
            "agent_key": None,
            "calls": 2,
            "prompt_tokens": 30,
            "completion_tokens": 10,
            "total_tokens": 40,
            "error_count": 1,
            "avg_latency_ms": 12.5,
        },
        {
            "bucket": "agent",
            "model": None,
            "agent_key": "writer",
            "calls": 2,
            "prompt_tokens": 30,
            "completion_tokens": 10,
            "total_tokens": 40,
            "error_count": 0,
        },
    ]
    summary = us.fetch_summary(window="24h")
    assert len(fake_db.executed) == 1
    sql, _params = fake_db.executed[0]
    assert "GROUPING SETS" in sql
    assert summary["total_calls"] == 2
    assert summary["by_model"]["claude-opus-4-8"]["calls"] == 2
    assert summary["by_model"]["claude-opus-4-8"]["tokens"] == 40
    assert summary["by_agent"]["writer"]["calls"] == 2
    assert summary["avg_latency_ms"] == 12.5
    assert summary["by_agent"]["writer"]["tokens"] == 40


def test_fetch_summary_skips_blank_agent_and_missing_totals(fake_db) -> None:
    """Blank agent_key rows are omitted; a missing totals bucket zeros the header."""
    fake_db._fetchall = [
        {
            "bucket": "model",
            "model": "",
            "agent_key": None,
            "calls": 1,
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
            "error_count": None,
        },
        {
            "bucket": "agent",
            "model": None,
            "agent_key": "",
            "calls": 1,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 4,
            "error_count": 0,
        },
    ]
    summary = us.fetch_summary(window="all")
    assert summary["total_calls"] == 0
    assert summary["total_tokens"] == 0
    assert summary["by_model"][""]["calls"] == 1
    assert summary["by_model"][""]["total_tokens"] == 0
    assert summary["by_agent"] == {}


def test_fetch_summary_24h_and_all(fake_db) -> None:
    fake_db._fetchall = [
        {
            "bucket": "total",
            "model": None,
            "agent_key": None,
            "calls": 2,
            "prompt_tokens": 30,
            "completion_tokens": 10,
            "total_tokens": 40,
            "error_count": 1,
        },
        {
            "bucket": "model",
            "model": "claude-opus-4-8",
            "agent_key": None,
            "calls": 1,
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "error_count": 0,
        },
        {
            "bucket": "model",
            "model": "qwen3.5:cloud",
            "agent_key": None,
            "calls": 1,
            "prompt_tokens": 20,
            "completion_tokens": 5,
            "total_tokens": 25,
            "error_count": 1,
        },
    ]
    summary = us.fetch_summary(window="24h")
    assert summary["window"] == "24h"
    assert summary["window_hours"] == 24.0
    assert summary["total_calls"] == 2
    assert summary["total_prompt_tokens"] == 30
    assert summary["total_completion_tokens"] == 10
    assert summary["total_tokens"] == 40
    assert summary["avg_latency_ms"] == 0.0
    assert summary["error_count"] == 1
    assert summary["by_model"]["claude-opus-4-8"]["prompt_tokens"] == 10
    assert summary["by_model"]["qwen3.5:cloud"]["total_tokens"] == 25
    assert "calls" in summary["by_model"]["claude-opus-4-8"]
    # 24h applies a cutoff; all does not.
    cutoff_sql = fake_db.executed[0][0]
    assert "ts >=" in cutoff_sql

    fake_db.executed.clear()
    fake_db._fetchall = [
        {
            "bucket": "total",
            "model": None,
            "agent_key": None,
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "error_count": 0,
        }
    ]
    all_summary = us.fetch_summary(window="all")
    assert all_summary["window_hours"] == 0.0
    assert "ts >=" not in fake_db.executed[0][0]

    fake_db.executed.clear()
    fake_db._fetchall = [
        {
            "bucket": "total",
            "model": None,
            "agent_key": None,
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "error_count": 0,
        }
    ]
    zero_summary = us.fetch_summary(window="0")
    assert zero_summary["window"] == "0"
    assert zero_summary["window_hours"] == 0.0
    assert "ts >=" in fake_db.executed[0][0]


def test_fetch_summary_totals_cache_tokens_and_defaults_to_zero(fake_db) -> None:
    """Cache totals sum from the persisted columns; absent rows default to 0."""
    fake_db._fetchall = [
        {
            "bucket": "total",
            "model": None,
            "agent_key": None,
            "calls": 2,
            "prompt_tokens": 30,
            "completion_tokens": 10,
            "total_tokens": 40,
            "cache_read_tokens": 500,
            "cache_creation_tokens": 200,
            "error_count": 0,
        }
    ]
    summary = us.fetch_summary(window="24h")
    assert summary["total_cache_read_tokens"] == 500
    assert summary["total_cache_creation_tokens"] == 200

    fake_db.executed.clear()
    fake_db._fetchall = [
        {
            "bucket": "total",
            "model": None,
            "agent_key": None,
            "calls": 1,
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "error_count": 0,
        }
    ]
    no_cache_summary = us.fetch_summary(window="24h")
    assert no_cache_summary["total_cache_read_tokens"] == 0
    assert no_cache_summary["total_cache_creation_tokens"] == 0


def test_empty_summary_includes_zeroed_cache_totals() -> None:
    empty = us.empty_summary(window="24h", team=None)
    assert empty["total_cache_read_tokens"] == 0
    assert empty["total_cache_creation_tokens"] == 0


def test_fetch_summary_query_failure_returns_empty(fake_db) -> None:
    fake_db._raise = True
    summary = us.fetch_summary(window="24h", team="blogging")
    assert summary["total_calls"] == 0
    assert summary["by_model"] == {}
    assert summary["team"] == "blogging"
    assert summary[us.QUERY_FAILED_KEY] is True


def test_fetch_recent_oldest_to_newest_and_limit(fake_db) -> None:
    ts_new = datetime(2026, 8, 12, 13, 0, tzinfo=timezone.utc)
    ts_old = datetime(2026, 8, 12, 11, 0, tzinfo=timezone.utc)
    fake_db._fetchall = [
        {
            "ts": ts_new,
            "team": "blogging",
            "agent_key": "writer",
            "model": "m1",
            "prompt_tokens": 1,
            "completion_tokens": 2,
            "total_tokens": 3,
            "latency_ms": 10,
            "status": "success",
        },
        {
            "ts": ts_old,
            "team": "blogging",
            "agent_key": "writer",
            "model": "m2",
            "prompt_tokens": 4,
            "completion_tokens": 5,
            "total_tokens": 9,
            "latency_ms": 20,
            "status": "error",
        },
    ]
    rows = us.fetch_recent(window="24h", limit=2)
    assert len(rows) == 2
    assert rows[0]["model"] == "m2"
    assert rows[0]["timestamp"] == ts_old.timestamp()
    assert rows[0]["caller_tag"] == ""
    assert rows[0]["cost_usd"] == 0.0
    assert rows[0]["outcome"] == ""
    assert "error_type" not in rows[0]
    assert rows[1]["model"] == "m1"
    assert rows[1]["latency_ms"] == 10
    sql, params = fake_db.executed[0]
    assert "ORDER BY ts DESC" in sql
    assert "caller_tag" in sql
    assert "cost_usd" in sql
    assert "outcome" in sql
    assert params[-1] == 2


def test_record_to_row_and_fetch_recent_preserve_call_metadata(fake_db) -> None:
    rec = _Rec(
        caller_tag="writer.agent.write_draft",
        latency_ms=42,
        cost_usd=0.12,
        outcome="success",
        error_type="TimeoutError",
        job_id="job-9",
        objective="draft",
        request_id="req-1",
        task_id="task-2",
        phase="execute",
        cache_read_tokens=500,
        cache_creation_tokens=200,
    )
    row = us.record_to_row(rec)
    assert row[7] == 42
    assert row[9] == "writer.agent.write_draft"
    assert row[10] == 0.12
    assert row[11] == "success"
    assert row[12] == "TimeoutError"
    assert row[13] == "job-9"
    assert row[14] == "draft"
    assert row[15] == "req-1"
    assert row[16] == "task-2"
    assert row[17] == "execute"
    assert row[18] == 500
    assert row[19] == 200

    ts = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    fake_db._fetchall = [
        {
            "ts": ts,
            "team": "blogging",
            "agent_key": "writer",
            "model": "m1",
            "prompt_tokens": 1,
            "completion_tokens": 2,
            "total_tokens": 3,
            "latency_ms": 42,
            "status": "success",
            "caller_tag": "writer.agent.write_draft",
            "cost_usd": 0.12,
            "outcome": "success",
            "error_type": "TimeoutError",
            "job_id": "job-9",
            "objective": "draft",
            "request_id": "req-1",
            "task_id": "task-2",
            "phase": "execute",
            "cache_read_tokens": 500,
            "cache_creation_tokens": 200,
        }
    ]
    rows = us.fetch_recent(window="all", limit=1)
    assert rows == [
        {
            "timestamp": ts.timestamp(),
            "team": "blogging",
            "agent_key": "writer",
            "model": "m1",
            "caller_tag": "writer.agent.write_draft",
            "prompt_tokens": 1,
            "completion_tokens": 2,
            "total_tokens": 3,
            "latency_ms": 42,
            "status": "success",
            "cost_usd": 0.12,
            "outcome": "success",
            "cache_read_tokens": 500,
            "cache_creation_tokens": 200,
            "error_type": "TimeoutError",
            "job_id": "job-9",
            "objective": "draft",
            "request_id": "req-1",
            "task_id": "task-2",
            "phase": "execute",
        }
    ]


def test_record_to_row_sanitizes_invalid_cost() -> None:
    assert us.record_to_row(_Rec(cost_usd=-1))[10] == 0.0
    assert us.record_to_row(_Rec(cost_usd=float("inf")))[10] == 0.0
    assert us.record_to_row(_Rec(cost_usd=float("nan")))[10] == 0.0


def test_write_rows_empty_returns_zero() -> None:
    assert us.write_rows([]) == 0


def test_write_rows_cur_none(monkeypatch) -> None:
    monkeypatch.setattr(us, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(us, "_table_ensured", True)

    @contextmanager
    def _none_cursor(*, dict_rows: bool = False, database=None):
        yield None

    monkeypatch.setattr(us, "pg_cursor", _none_cursor)
    assert us.write_rows([us.record_to_row(_Rec())]) == 0


def test_write_rows_exception_returns_zero(fake_db) -> None:
    fake_db._raise = True
    assert us.write_rows([us.record_to_row(_Rec())]) == 0


def test_ensure_table_cur_none(monkeypatch) -> None:
    monkeypatch.setattr(us, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(us, "_table_ensured", False)

    @contextmanager
    def _none_cursor(*, dict_rows: bool = False, database=None):
        yield None

    monkeypatch.setattr(us, "pg_cursor", _none_cursor)
    us._ensure_table()
    assert us._table_ensured is False


def test_ensure_table_executes_ddl(monkeypatch) -> None:
    cursor = FakeCursor()

    @contextmanager
    def _pg_cursor(*, dict_rows: bool = False, database=None):
        yield cursor

    monkeypatch.setattr(us, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(us, "pg_cursor", _pg_cursor)
    monkeypatch.setattr(us, "_table_ensured", False)
    us._ensure_table()
    assert us._table_ensured is True
    assert [sql for sql, _ in cursor.executed] == list(us.USAGE_TABLE_STATEMENTS)
    assert "CREATE TABLE IF NOT EXISTS llm_call_records" in cursor.executed[0][0]
    assert "CREATE INDEX IF NOT EXISTS idx_llm_call_records_ts" in cursor.executed[1][0]
    assert "ADD COLUMN IF NOT EXISTS latency_ms" in cursor.executed[2][0]
    joined = "\n".join(sql for sql, _ in cursor.executed)
    assert "ADD COLUMN IF NOT EXISTS caller_tag" in joined
    assert "ADD COLUMN IF NOT EXISTS cost_usd" in joined
    assert "ADD COLUMN IF NOT EXISTS outcome" in joined
    assert "ADD COLUMN IF NOT EXISTS cache_read_tokens" in joined
    assert "ADD COLUMN IF NOT EXISTS cache_creation_tokens" in joined


def test_ensure_table_exception_leaves_flag_false(monkeypatch) -> None:
    cursor = FakeCursor(raise_on_execute=True)

    @contextmanager
    def _pg_cursor(*, dict_rows: bool = False, database=None):
        yield cursor

    monkeypatch.setattr(us, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(us, "pg_cursor", _pg_cursor)
    monkeypatch.setattr(us, "_table_ensured", False)
    us._ensure_table()
    assert us._table_ensured is False


def test_ensure_table_noop_when_postgres_off(monkeypatch) -> None:
    monkeypatch.setattr(us, "is_postgres_enabled", lambda: False)
    monkeypatch.setattr(us, "_table_ensured", False)
    us._ensure_table()
    assert us._table_ensured is False


def test_ensure_table_double_check_inside_lock(monkeypatch) -> None:
    """Second check under the lock skips DDL when another thread already ensured."""
    calls = {"n": 0}

    @contextmanager
    def _pg_cursor(*, dict_rows: bool = False, database=None):
        calls["n"] += 1
        yield FakeCursor()

    class _MarkEnsuredLock:
        def __enter__(self):
            us._table_ensured = True
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(us, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(us, "pg_cursor", _pg_cursor)
    monkeypatch.setattr(us, "_table_ensured", False)
    monkeypatch.setattr(us, "_ensure_lock", _MarkEnsuredLock())
    us._ensure_table()
    assert calls["n"] == 0
    assert us._table_ensured is True


def test_fetch_summary_postgres_off(monkeypatch) -> None:
    monkeypatch.setattr(us, "is_postgres_enabled", lambda: False)
    summary = us.fetch_summary(window="24h", team="blogging")
    assert summary["total_calls"] == 0
    assert summary["team"] == "blogging"
    assert summary["by_model"] == {}
    assert us.QUERY_FAILED_KEY not in summary


def test_fetch_summary_numeric_window_postgres_off(monkeypatch) -> None:
    monkeypatch.setattr(us, "is_postgres_enabled", lambda: False)
    summary = us.fetch_summary(window="1.0")
    assert summary["window"] == "1.0"
    assert summary["window_hours"] == 1.0


def test_fetch_summary_cur_none(monkeypatch) -> None:
    monkeypatch.setattr(us, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(us, "_table_ensured", True)

    @contextmanager
    def _none_cursor(*, dict_rows: bool = False, database=None):
        yield None

    monkeypatch.setattr(us, "pg_cursor", _none_cursor)
    summary = us.fetch_summary(window="7d")
    assert summary["total_calls"] == 0
    assert summary["window"] == "7d"
    assert summary[us.QUERY_FAILED_KEY] is True


def test_fetch_recent_postgres_off(monkeypatch) -> None:
    monkeypatch.setattr(us, "is_postgres_enabled", lambda: False)
    assert us.fetch_recent(window="24h") == []


def test_fetch_recent_cur_none(monkeypatch) -> None:
    monkeypatch.setattr(us, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(us, "_table_ensured", True)

    @contextmanager
    def _none_cursor(*, dict_rows: bool = False, database=None):
        yield None

    monkeypatch.setattr(us, "pg_cursor", _none_cursor)
    assert us.fetch_recent(window="24h") is None


def test_fetch_recent_naive_and_non_datetime_ts(fake_db) -> None:
    naive = datetime(2026, 8, 12, 12, 0)  # no tzinfo
    fake_db._fetchall = [
        {
            "ts": naive,
            "team": "t",
            "agent_key": "a",
            "model": "m",
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
            "status": "success",
        },
        {
            "ts": 1_724_000_000.5,
            "team": "t",
            "agent_key": "a",
            "model": "m2",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "status": "error",
        },
        {
            "ts": None,
            "team": "",
            "agent_key": "",
            "model": "",
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
            "status": None,
        },
    ]
    rows = us.fetch_recent(window="all", limit=10)
    assert len(rows) == 3
    # SQL returns newest-first; fetch_recent reverses to oldest-to-newest.
    assert rows[0]["timestamp"] == 0.0
    assert rows[0]["team"] == ""
    assert rows[0]["status"] == ""
    assert rows[1]["timestamp"] == 1_724_000_000.5
    assert rows[2]["timestamp"] == naive.replace(tzinfo=timezone.utc).timestamp()


def test_fetch_recent_exception_returns_none(fake_db) -> None:
    fake_db._raise = True
    assert us.fetch_recent(window="24h", team="blogging") is None
