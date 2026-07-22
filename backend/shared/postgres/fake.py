"""In-memory FakeCursor / FakeConn scaffold for team store unit tests.

Teams supply a SQL→handler dispatch table; this module owns normalize,
rowcount/fetch semantics, Json unwrap, and ``get_conn`` monkeypatch install.
Live-Postgres truncate helpers remain in ``shared.postgres.testing``.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from typing import Any

SqlMatcher = Callable[[str], bool]
SqlHandler = Callable[["FakeCursor", tuple], None]
DispatchTable = Sequence[tuple[SqlMatcher, SqlHandler]]


def unwrap_json(value: Any) -> Any:
    """Unwrap a psycopg ``Json``-like wrapper to its plain object.

    Preconditions:
        None — any value is accepted.
    Postconditions:
        If ``value`` has an ``obj`` attribute, return ``value.obj``; otherwise
        return ``value`` unchanged.
    """
    if hasattr(value, "obj"):
        return value.obj
    return value


def _normalize_sql(sql: str) -> str:
    return " ".join(sql.split()).lower()


class FakeCursor:
    """Dispatch-table cursor matching the shape of team ``_fake_postgres`` fakes.

    Preconditions:
        ``db`` is a mutable mapping shared with the owning ``FakeConn``.
        ``dispatch`` is an ordered sequence of ``(matcher, handler)`` pairs;
        matchers receive normalized SQL and return bool; first True wins.
        ``ids`` if provided is an iterator of int-like ids; otherwise a
        ``itertools.count(1)`` is used.
    Postconditions:
        ``execute`` either invokes exactly one matching handler or raises
        ``AssertionError`` naming the original SQL.
    Invariants:
        ``fetchone`` / ``fetchall`` return the values last set by handlers
        (via ``set_one`` / ``set_all`` or direct attribute writes).
    """

    def __init__(
        self,
        db: dict[str, Any],
        dispatch: DispatchTable,
        ids: itertools.count | None = None,
        row_factory: Any = None,
    ) -> None:
        self.db = db
        self._dispatch = dispatch
        self.ids = ids if ids is not None else itertools.count(1)
        self.row_factory = row_factory
        self.rowcount = 0
        self._one: Any = None
        self._all: list = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def set_one(self, row: Any) -> None:
        """Record the next ``fetchone`` result.

        Preconditions:
            None.
        Postconditions:
            ``fetchone()`` returns ``row``.
        """
        self._one = row

    def set_all(self, rows: list) -> None:
        """Record the next ``fetchall`` result.

        Preconditions:
            ``rows`` is a list (may be empty).
        Postconditions:
            ``fetchall()`` returns ``rows``.
        """
        self._all = rows

    def execute(self, sql: str, params: Any = ()) -> None:
        """Normalize SQL, coerce params, and dispatch to the first matcher.

        Preconditions:
            ``sql`` is a non-empty str resembling a store query.
            ``params`` is a sequence (tuple/list) or empty.
        Postconditions:
            Exactly one handler ran, or ``AssertionError`` was raised with
            the original ``sql`` string in the message.
        """
        norm = _normalize_sql(sql)
        param_tuple = tuple(params)
        for matcher, handler in self._dispatch:
            if matcher(norm):
                handler(self, param_tuple)
                return
        raise AssertionError(f"unexpected SQL in fake cursor: {sql!r}")

    def fetchone(self) -> Any:
        return self._one

    def fetchall(self) -> list:
        return self._all


class FakeConn:
    """Connection stub that vends ``FakeCursor`` instances sharing state.

    Preconditions:
        Same as ``FakeCursor`` for ``db`` / ``dispatch`` / ``ids``.
    Postconditions:
        Every ``cursor()`` shares the same ``db`` and ``ids`` iterator.
    """

    def __init__(
        self,
        db: dict[str, Any],
        dispatch: DispatchTable,
        ids: itertools.count | None = None,
    ) -> None:
        self._db = db
        self._dispatch = dispatch
        self._ids = ids if ids is not None else itertools.count(1)

    def cursor(self, row_factory: Any = None) -> FakeCursor:
        return FakeCursor(self._db, self._dispatch, self._ids, row_factory=row_factory)


def install_fake_postgres(
    monkeypatch: Any,
    *,
    modules: Sequence[Any],
    dispatch: DispatchTable,
    db: dict[str, Any] | None = None,
    id_start: int = 1,
    attr: str = "get_conn",
) -> dict[str, Any]:
    """Patch ``get_conn`` on each module to yield a shared ``FakeConn``.

    Preconditions:
        ``monkeypatch`` supports ``setattr(target, name, value)`` (pytest).
        ``modules`` is a non-empty sequence of already-imported modules that
        expose ``attr`` (default ``get_conn``).
        ``dispatch`` is a valid ``DispatchTable``.
        ``id_start`` is an int used as the first ``next(ids)`` value.
    Postconditions:
        Each module's ``attr`` is a context manager yielding ``FakeConn``
        backed by the returned ``db`` dict and a shared ``ids`` counter.
        Returns the backing ``db`` (the provided dict, or a new ``{}``).
    """
    backing = db if db is not None else {}
    ids = itertools.count(id_start)
    conn = FakeConn(backing, dispatch, ids)

    @contextmanager
    def _fake_get_conn(database: Any = None):  # noqa: ANN401
        yield conn

    for mod in modules:
        monkeypatch.setattr(mod, attr, _fake_get_conn)
    return backing


__all__ = [
    "DispatchTable",
    "FakeConn",
    "FakeCursor",
    "SqlHandler",
    "SqlMatcher",
    "install_fake_postgres",
    "unwrap_json",
]
