"""Offline coverage for the code-review history store (no live Postgres needed).

Exercises the disabled fast-paths and the best-effort exception handling so the
store's behaviour is verified even on a run without a database.
"""

from __future__ import annotations

import coding_team.review_history_store as store


def test_writes_are_noop_when_postgres_disabled(monkeypatch) -> None:
    monkeypatch.setattr(store, "is_postgres_enabled", lambda: False)
    # None of these touch the database; none raise; reads return [].
    store.record_review_start("j", "o", "r", 1, "u", "alice")
    store.update_review("j", status="running")
    assert store.list_reviews("o", "r") == []


def test_writes_swallow_db_errors_and_reads_degrade(monkeypatch) -> None:
    monkeypatch.setattr(store, "is_postgres_enabled", lambda: True)

    def _boom(*_a, **_kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "get_conn", _boom)
    # Best-effort: a DB failure is logged, never raised.
    store.record_review_start("j", "o", "r", 1, "u", "alice")
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
