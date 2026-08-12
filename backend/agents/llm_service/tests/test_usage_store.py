"""Unit tests for llm_service.usage_store (FakeCursor, no live Postgres)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import pytest

from llm_service import usage_store as us


class FakeCursor:
    def __init__(
        self, fetchone_rows=None, fetchall_rows=None, raise_on_execute=False
    ) -> None:
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
    assert len(row) == 8
    assert row[1] == "blogging"
    assert row[4] == 10
    assert row[5] == 5
    assert row[6] == 15
    n = us.write_rows([row])
    assert n == 1
    sql, params = fake_db.executed[0]
    assert "INSERT INTO llm_call_records" in sql
    assert params == [row]


def test_fetch_summary_24h_and_all(fake_db) -> None:
    fake_db._fetchone = [
        {
            "total_calls": 2,
            "total_prompt_tokens": 30,
            "total_completion_tokens": 10,
            "total_tokens": 40,
            "error_count": 1,
        }
    ]
    fake_db._fetchall = [
        [
            {
                "model": "claude-opus-4-8",
                "calls": 1,
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
            {
                "model": "qwen3.5:cloud",
                "calls": 1,
                "prompt_tokens": 20,
                "completion_tokens": 5,
                "total_tokens": 25,
            },
        ],
        [],  # by_agent query
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
    fake_db._fetchone = [
        {
            "total_calls": 0,
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
            "total_tokens": 0,
            "error_count": 0,
        }
    ]
    fake_db._fetchall = [[], []]
    all_summary = us.fetch_summary(window="all")
    assert all_summary["window_hours"] == 0.0
    assert "ts >=" not in fake_db.executed[0][0]


def test_fetch_summary_query_failure_returns_empty(fake_db) -> None:
    fake_db._raise = True
    summary = us.fetch_summary(window="24h", team="blogging")
    assert summary["total_calls"] == 0
    assert summary["by_model"] == {}
    assert summary["team"] == "blogging"


def test_fetch_recent_newest_first_and_limit(fake_db) -> None:
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
            "status": "error",
        },
    ]
    rows = us.fetch_recent(window="24h", limit=2)
    assert len(rows) == 2
    assert rows[0]["model"] == "m1"
    assert rows[0]["timestamp"] == ts_new.timestamp()
    assert rows[1]["model"] == "m2"
    sql, params = fake_db.executed[0]
    assert "ORDER BY ts DESC" in sql
    assert params[-1] == 2
