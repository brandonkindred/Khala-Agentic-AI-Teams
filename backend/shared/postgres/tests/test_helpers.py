"""Tests for shared.postgres.helpers (``best_effort_write`` and
``PostgresHelperMixin``) — all mocked, no live Postgres required."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from shared.postgres import client as client_mod
from shared.postgres.helpers import PostgresHelperMixin, best_effort_write


class _Probe(PostgresHelperMixin):
    """Minimal concrete mixin subclass for exercising shared helper methods."""


def _psycopg_installed() -> bool:
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return False
    return True


@contextmanager
def _conn_context(conn):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


class _FakePool:
    """Fake pool whose ``connection()`` commits on clean exit, rolls back on exception."""

    def __init__(self):
        self.conn = MagicMock()
        self._conn_cm = _conn_context(self.conn)

    def connection(self):
        return self._conn_cm


def _fake_cursor():
    cur = MagicMock()
    cur.__enter__ = lambda self=cur: cur
    cur.__exit__ = lambda *a: False
    return cur


def _install_fake_pool(monkeypatch, cursor):
    monkeypatch.setenv("POSTGRES_HOST", "postgres")
    fake = _FakePool()
    fake.conn.cursor.return_value = cursor
    monkeypatch.setattr(client_mod, "_get_or_create_pool", lambda database=None: fake)
    return fake


# ---------------------------------------------------------------------------
# _fetch_one
# ---------------------------------------------------------------------------


def test_fetch_one_returns_row_on_hit(monkeypatch):
    cur = _fake_cursor()
    cur.fetchone.return_value = {"id": 1, "name": "acme"}
    fake = _install_fake_pool(monkeypatch, cur)

    result = _Probe()._fetch_one("SELECT * FROM t WHERE id = %s", (1,))

    assert result == {"id": 1, "name": "acme"}
    cur.execute.assert_called_once_with("SELECT * FROM t WHERE id = %s", (1,))
    fake.conn.commit.assert_called_once()


def test_fetch_one_returns_none_on_miss(monkeypatch):
    cur = _fake_cursor()
    cur.fetchone.return_value = None
    _install_fake_pool(monkeypatch, cur)

    result = _Probe()._fetch_one("SELECT * FROM t WHERE id = %s", (999,))

    assert result is None


def test_fetch_one_raises_when_postgres_disabled(monkeypatch):
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    with pytest.raises(RuntimeError, match="POSTGRES_HOST is not set"):
        _Probe()._fetch_one("SELECT 1")


@pytest.mark.skipif(not _psycopg_installed(), reason="psycopg not installed")
def test_fetch_one_uses_dict_row_cursor(monkeypatch):
    from psycopg.rows import dict_row

    cur = _fake_cursor()
    cur.fetchone.return_value = {"id": 1}
    fake = _install_fake_pool(monkeypatch, cur)

    _Probe()._fetch_one("SELECT 1")

    fake.conn.cursor.assert_called_once_with(row_factory=dict_row)


# ---------------------------------------------------------------------------
# _fetch_all
# ---------------------------------------------------------------------------


def test_fetch_all_returns_all_rows(monkeypatch):
    cur = _fake_cursor()
    cur.fetchall.return_value = [{"id": 1}, {"id": 2}]
    _install_fake_pool(monkeypatch, cur)

    result = _Probe()._fetch_all("SELECT * FROM t")

    assert result == [{"id": 1}, {"id": 2}]
    cur.execute.assert_called_once_with("SELECT * FROM t", ())


def test_fetch_all_returns_empty_list_when_no_rows(monkeypatch):
    cur = _fake_cursor()
    cur.fetchall.return_value = []
    _install_fake_pool(monkeypatch, cur)

    assert _Probe()._fetch_all("SELECT * FROM t WHERE 1=0") == []


def test_fetch_all_raises_when_postgres_disabled(monkeypatch):
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    with pytest.raises(RuntimeError, match="POSTGRES_HOST is not set"):
        _Probe()._fetch_all("SELECT 1")


# ---------------------------------------------------------------------------
# _execute
# ---------------------------------------------------------------------------


def test_execute_returns_rowcount_on_match(monkeypatch):
    cur = _fake_cursor()
    cur.rowcount = 3
    _install_fake_pool(monkeypatch, cur)

    result = _Probe()._execute("UPDATE t SET name = %s WHERE id = %s", ("x", 1))

    assert result == 3
    cur.execute.assert_called_once_with("UPDATE t SET name = %s WHERE id = %s", ("x", 1))


def test_execute_returns_zero_rowcount_when_unmatched(monkeypatch):
    cur = _fake_cursor()
    cur.rowcount = 0
    _install_fake_pool(monkeypatch, cur)

    assert _Probe()._execute("UPDATE t SET name = %s WHERE id = %s", ("x", 999)) == 0


def test_execute_uses_plain_cursor_not_dict_row(monkeypatch):
    cur = _fake_cursor()
    cur.rowcount = 1
    fake = _install_fake_pool(monkeypatch, cur)

    _Probe()._execute("DELETE FROM t WHERE id = %s", (1,))

    fake.conn.cursor.assert_called_once_with()


def test_execute_raises_when_postgres_disabled(monkeypatch):
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    with pytest.raises(RuntimeError, match="POSTGRES_HOST is not set"):
        _Probe()._execute("DELETE FROM t WHERE id = %s", (1,))


# ---------------------------------------------------------------------------
# _transaction
# ---------------------------------------------------------------------------


def test_transaction_shares_one_cursor_across_multiple_statements(monkeypatch):
    cur = _fake_cursor()
    cur.fetchone.return_value = {"id": 1}
    fake = _install_fake_pool(monkeypatch, cur)

    with _Probe()._transaction() as tx_cur:
        tx_cur.execute("INSERT INTO t (id) VALUES (%s)", (1,))
        tx_cur.execute("SELECT * FROM t WHERE id = %s", (1,))
        row = tx_cur.fetchone()

    assert row == {"id": 1}
    assert cur.execute.call_count == 2
    # One connection/cursor acquired for the whole block, not one per statement.
    fake.conn.cursor.assert_called_once()
    fake.conn.commit.assert_called_once()
    fake.conn.rollback.assert_not_called()


@pytest.mark.skipif(not _psycopg_installed(), reason="psycopg not installed")
def test_transaction_uses_dict_row_cursor(monkeypatch):
    from psycopg.rows import dict_row

    cur = _fake_cursor()
    fake = _install_fake_pool(monkeypatch, cur)

    with _Probe()._transaction():
        pass

    fake.conn.cursor.assert_called_once_with(row_factory=dict_row)


def test_transaction_rolls_back_on_exception(monkeypatch):
    cur = _fake_cursor()
    fake = _install_fake_pool(monkeypatch, cur)

    with pytest.raises(RuntimeError, match="boom"):
        with _Probe()._transaction() as tx_cur:
            tx_cur.execute("INSERT INTO t (id) VALUES (%s)", (1,))
            raise RuntimeError("boom")

    fake.conn.rollback.assert_called_once()
    fake.conn.commit.assert_not_called()


def test_transaction_raises_when_postgres_disabled(monkeypatch):
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    with pytest.raises(RuntimeError, match="POSTGRES_HOST is not set"):
        with _Probe()._transaction():
            pass


# ---------------------------------------------------------------------------
# best_effort_write
#
# The whole contract is BEHAVIOURAL (invoke, swallow, log, never re-raise), so
# there is nothing to assert about it except by running it. Every team store
# whose persistence is a safety net rather than a prerequisite routes its writes
# through here, so a regression that let an exception escape would fail an
# address-comments or review run for a bookkeeping write that was never allowed
# to matter.
# ---------------------------------------------------------------------------


def test_best_effort_write_invokes_the_write() -> None:
    """The happy path still actually calls through -- a helper that swallowed
    everything INCLUDING the call would pass every failure test below."""
    calls: list[int] = []

    best_effort_write("my_table", "record_thing", lambda: calls.append(1))

    assert calls == [1]


def test_best_effort_write_returns_none_on_success() -> None:
    """No return channel: callers must not branch on a result that isn't there.

    The callable returns a TRUTHY value, so a regression that forwarded it
    (``return write_fn()``) is detected. A callable returning ``None`` would
    make this assertion hold under exactly that regression.
    """
    assert best_effort_write("my_table", "record_thing", lambda: "leaked") is None


def test_best_effort_write_swallows_and_logs_the_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing write is logged with a traceback and NOT re-raised."""

    def _boom() -> None:
        raise RuntimeError("db down")

    with caplog.at_level("WARNING", logger="shared.postgres.helpers"):
        assert best_effort_write("my_table", "record_thing", _boom) is None

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    # The label is what an operator greps for, so pin its exact composition.
    assert warnings[0].getMessage() == "my_table: record_thing failed"
    # exc_info is the point of the log line: without it the swallowed failure
    # leaves no way to tell WHY the write failed.
    assert warnings[0].exc_info is not None
    assert warnings[0].exc_info[0] is RuntimeError


@pytest.mark.parametrize(
    "exc",
    [ValueError("bad"), KeyError("missing"), TypeError("wrong")],
    ids=["value", "key", "type"],
)
def test_best_effort_write_swallows_any_exception_subclass(exc: Exception) -> None:
    """`except Exception` means EVERY ordinary failure, not just database ones:
    a bug in the caller's own closure must not surface as a run failure either."""

    def _boom() -> None:
        raise exc

    assert best_effort_write("my_table", "op", _boom) is None


@pytest.mark.parametrize(
    "exc",
    [KeyboardInterrupt(), SystemExit(1), BaseException("raw")],
    ids=["keyboard-interrupt", "system-exit", "base"],
)
def test_best_effort_write_does_not_swallow_base_exceptions(exc: BaseException) -> None:
    """`BaseException` is deliberately OUT of scope.

    Ctrl-C and interpreter shutdown are not "the write failed" -- swallowing
    them would make a store's bookkeeping write the one place a shutdown signal
    goes to die, leaving the process wedged mid-teardown. The bare `except
    Exception` (not `except BaseException`) is what keeps that true, and this
    test is what stops it being "helpfully" widened.
    """

    def _boom() -> None:
        raise exc

    with pytest.raises(type(exc)):
        best_effort_write("my_table", "op", _boom)
