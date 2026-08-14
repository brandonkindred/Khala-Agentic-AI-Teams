"""Offline coverage for the code-review history store (no live Postgres needed).

Exercises the disabled fast-paths and the best-effort exception handling so the
store's behaviour is verified even on a run without a database.
"""

from __future__ import annotations

from datetime import datetime

import software_engineering_team.review_history_store as store


def test_writes_are_noop_when_postgres_disabled(monkeypatch) -> None:
    monkeypatch.setattr(store, "is_postgres_enabled", lambda: False)
    # None of these touch the database; none raise; reads return [].
    # record_review_start still returns its server-clock created_at (used by the UI
    # for a live duration) even when persistence is disabled.
    started = store.record_review_start("j", "o", "r", 1, "u", "alice")
    assert isinstance(started, datetime)
    store.update_review("j", status="running")
    assert store.list_reviews("o", "r") == []


def test_writes_swallow_db_errors_and_reads_degrade(monkeypatch) -> None:
    monkeypatch.setattr(store, "is_postgres_enabled", lambda: True)

    def _boom(*_a, **_kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "get_conn", _boom)
    # Best-effort: a DB failure is logged, never raised — and the created_at is still
    # returned so the caller has a start time even when the write fails.
    assert isinstance(store.record_review_start("j", "o", "r", 1, "u", "alice"), datetime)
    store.update_review(
        "j",
        status="completed",
        status_text="done",
        review_summary={"event": "COMMENT"},
        error=None,
        completed=True,
    )
    # The read degrades to an empty list (exercises the pr_number filter + limit clamp).
    assert store.list_reviews("o", "r", 1, limit=10) == []


def test_update_refuses_non_allowlisted_column(monkeypatch) -> None:
    monkeypatch.setattr(store, "is_postgres_enabled", lambda: True)
    # Shrink the allowlist so a normally-valid column is now disallowed; the guard
    # must refuse to build the query (no get_conn call) rather than proceed.
    monkeypatch.setattr(store, "_UPDATABLE_COLUMNS", frozenset({"status"}))

    def _no_conn(*_a, **_kw):
        raise AssertionError("get_conn must not be called when the guard trips")

    monkeypatch.setattr(store, "get_conn", _no_conn)
    # status_text is no longer allowlisted -> the guard logs and returns early.
    store.update_review("j", status="running", status_text="x")


def test_get_review_none_when_postgres_disabled(monkeypatch) -> None:
    monkeypatch.setattr(store, "is_postgres_enabled", lambda: False)
    assert store.get_review("j") is None


def test_get_review_degrades_on_db_error(monkeypatch) -> None:
    monkeypatch.setattr(store, "is_postgres_enabled", lambda: True)

    def _boom(*_a, **_kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "get_conn", _boom)
    # A DB failure is logged, never raised; the read degrades to None.
    assert store.get_review("j") is None


def test_append_transcript_entries_noop_when_postgres_disabled(monkeypatch) -> None:
    monkeypatch.setattr(store, "is_postgres_enabled", lambda: False)

    def _no_conn(*_a, **_kw):
        raise AssertionError("get_conn must not be called when Postgres is disabled")

    monkeypatch.setattr(store, "get_conn", _no_conn)
    # Never raises, never touches the database; reports failure so the caller
    # (the transcript flusher) knows to requeue rather than treat it as written.
    assert store.append_review_transcript_entries("j", [{"stage": "chunk_review"}]) is False


def test_append_transcript_entries_noop_for_empty_list(monkeypatch) -> None:
    monkeypatch.setattr(store, "is_postgres_enabled", lambda: True)

    def _no_conn(*_a, **_kw):
        raise AssertionError("get_conn must not be called for an empty batch")

    monkeypatch.setattr(store, "get_conn", _no_conn)
    assert store.append_review_transcript_entries("j", []) is False


def test_append_transcript_entries_swallows_db_errors(monkeypatch) -> None:
    monkeypatch.setattr(store, "is_postgres_enabled", lambda: True)

    def _boom(*_a, **_kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "get_conn", _boom)
    # Best-effort: a DB failure (including a foreign-key violation for an
    # unknown job_id) is logged, never raised, and reported as False.
    assert store.append_review_transcript_entries("j", [{"stage": "chunk_review"}]) is False


def test_unpersisted_transcript_entries_skips_known_ids() -> None:
    from software_engineering_team.review_history_store import unpersisted_transcript_entries

    existing = [{"entry_id": "a", "prompt": "old"}, {"prompt": "legacy"}]
    incoming = [
        {"entry_id": "a", "prompt": "retry"},
        {"entry_id": "b", "prompt": "new"},
        {"prompt": "no-id"},
    ]
    assert unpersisted_transcript_entries(existing, incoming) == [
        {"entry_id": "b", "prompt": "new"},
        {"prompt": "no-id"},
    ]


def test_get_review_transcript_none_when_postgres_disabled(monkeypatch) -> None:
    monkeypatch.setattr(store, "is_postgres_enabled", lambda: False)
    assert store.get_review_transcript("j") is None


def test_get_review_transcript_degrades_on_db_error(monkeypatch) -> None:
    monkeypatch.setattr(store, "is_postgres_enabled", lambda: True)

    def _boom(*_a, **_kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "get_conn", _boom)
    assert store.get_review_transcript("j") is None


def test_get_review_transcript_returns_entries_on_success(monkeypatch) -> None:
    monkeypatch.setattr(store, "is_postgres_enabled", lambda: True)
    entries = [{"stage": "chunk_review", "prompt": "p", "response": "r"}]

    class _FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def execute(self, *_a, **_kw):
            pass

        def fetchone(self):
            return {"entries": entries}

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def cursor(self, *_a, **_kw):
            return _FakeCursor()

    monkeypatch.setattr(store, "get_conn", lambda: _FakeConn())
    assert store.get_review_transcript("j1") == entries


def test_get_review_returns_row_on_success(monkeypatch) -> None:
    monkeypatch.setattr(store, "is_postgres_enabled", lambda: True)
    row = {"job_id": "j1", "owner": "o", "repo": "r", "pr_number": 7, "status": "completed"}

    class _FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def execute(self, *_a, **_kw):
            pass

        def fetchone(self):
            return row

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def cursor(self, *_a, **_kw):
            return _FakeCursor()

    monkeypatch.setattr(store, "get_conn", lambda: _FakeConn())
    # The success path returns exactly the row the cursor yields.
    assert store.get_review("j1") == row
