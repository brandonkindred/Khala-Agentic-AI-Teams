"""Tests for the shared cross-worker ``advisory_lock`` context manager.

Covers the primitive directly (process-lock-only, Postgres-enabled, and
degrade-on-exception paths); the call-site tests in
``test_coding_team_review_pr.py`` cover ``_pr_review_admission`` and
``_issue_creation_lock`` delegating to it correctly.
"""

from __future__ import annotations

import contextlib
import threading
import time
from unittest.mock import MagicMock

import pytest

from software_engineering_team.api.advisory_lock import advisory_lock


def test_advisory_lock_process_only_when_postgres_unconfigured(monkeypatch) -> None:
    """With Postgres unconfigured, the body runs under the process lock alone."""
    import shared.postgres as shared_postgres

    monkeypatch.setattr(shared_postgres, "is_postgres_enabled", lambda: False)
    lock = threading.Lock()
    entered = False
    with advisory_lock(lock, "ns", "key"):
        entered = True
        assert lock.locked()
    assert entered
    assert not lock.locked()


def test_advisory_lock_takes_pg_advisory_lock_when_postgres_enabled(monkeypatch) -> None:
    """With Postgres configured, entering the lock issues exactly one
    pg_advisory_xact_lock call keyed on (namespace, key)."""
    import shared.postgres as shared_postgres

    conn = MagicMock()

    @contextlib.contextmanager
    def _fake_conn():
        yield conn

    monkeypatch.setattr(shared_postgres, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(shared_postgres, "get_conn", _fake_conn)
    with advisory_lock(threading.Lock(), "some_namespace", "some-key"):
        pass
    conn.execute.assert_called_once_with(
        "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
        ("some_namespace", "some-key"),
    )


def test_advisory_lock_releases_pg_connection_when_body_raises(monkeypatch) -> None:
    """When Postgres acquisition succeeds but the body raises, the connection's
    context manager is still exited (via the ExitStack) — the advisory-lock
    transaction is not leaked — and the body's exception propagates unchanged."""
    import shared.postgres as shared_postgres

    conn = MagicMock()

    class _RecordingConnCtx:
        def __init__(self, target: MagicMock) -> None:
            self._target = target
            self.exited = False
            self.exit_exc_type: type[BaseException] | None = None

        def __enter__(self) -> MagicMock:
            return self._target

        def __exit__(self, exc_type, exc, tb) -> bool:
            self.exited = True
            self.exit_exc_type = exc_type
            return False

    conn_ctx = _RecordingConnCtx(conn)
    monkeypatch.setattr(shared_postgres, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(shared_postgres, "get_conn", lambda: conn_ctx)

    with pytest.raises(ValueError, match="boom"):
        with advisory_lock(threading.Lock(), "ns", "key"):
            raise ValueError("boom")

    conn.execute.assert_called_once_with(
        "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
        ("ns", "key"),
    )
    assert conn_ctx.exited
    assert conn_ctx.exit_exc_type is ValueError


def test_advisory_lock_degrades_when_postgres_unavailable(monkeypatch) -> None:
    """A failing advisory-lock acquisition degrades to the process-local lock
    alone (logged) — must never raise or block the caller."""
    import shared.postgres as shared_postgres

    monkeypatch.setattr(shared_postgres, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(shared_postgres, "get_conn", MagicMock(side_effect=RuntimeError("pg down")))
    with advisory_lock(threading.Lock(), "ns", "key"):
        pass  # must not raise


def test_advisory_lock_process_lock_released_on_body_exception(monkeypatch) -> None:
    """The process lock is released even when the body raises, and the
    exception propagates unchanged."""
    import shared.postgres as shared_postgres

    monkeypatch.setattr(shared_postgres, "is_postgres_enabled", lambda: False)
    lock = threading.Lock()
    with pytest.raises(ValueError, match="boom"):
        with advisory_lock(lock, "ns", "key"):
            raise ValueError("boom")
    assert not lock.locked()


def test_advisory_lock_serializes_within_process() -> None:
    """Two concurrent callers sharing the same process lock cannot overlap."""
    lock = threading.Lock()
    order: list[str] = []
    entered = threading.Event()
    release = threading.Event()
    contending = threading.Event()

    def first() -> None:
        with advisory_lock(lock, "ns", "key"):
            entered.set()
            release.wait(5)
            order.append("first-exit")

    def second() -> None:
        contending.set()
        with advisory_lock(lock, "ns", "key"):
            order.append("second-enter")

    t1 = threading.Thread(target=first)
    t1.start()
    assert entered.wait(5)
    t2 = threading.Thread(target=second)
    t2.start()
    # The second thread is running and about to block on the (held) lock; the
    # brief window then lets it (wrongly) acquire if mutual exclusion is broken.
    assert contending.wait(5)
    time.sleep(0.05)
    assert "second-enter" not in order
    release.set()
    t1.join(5)
    t2.join(5)
    assert order == ["first-exit", "second-enter"]
