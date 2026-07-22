# Shared Postgres FakeCursor / FakeConn Scaffold Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a reusable in-memory `FakeCursor` / `FakeConn` / `install_fake_postgres` scaffold under `shared.postgres.fake`, covered by unit tests, without migrating any team `_fake_postgres.py`.

**Architecture:** New sibling module `fake.py` holds the dispatch-table-driven cursor/conn and install helper. Existing `testing.py` truncate helpers stay unchanged and re-export the fake public API. Handlers are imperative (`handler(cursor, params) -> None`); matchers are callables over normalized SQL; first match wins.

**Tech Stack:** Python 3.10+, pytest, ruff, existing `shared.postgres` package layout.

**Spec:** `docs/superpowers/specs/2026-07-22-shared-postgres-fake-cursor-scaffold-design.md`

**Worktree:** `.worktrees/refactor-fake-postgres-scaffold` on branch `refactor/fake-postgres-scaffold`

## Global Constraints

- Do not modify any team `tests/_fake_postgres.py` (branding, team_assistant, agentic_team_provisioning, user_profile).
- Do not change truncate / live-Postgres helpers in `testing.py` beyond thin re-exports.
- Production store modules must not import `shared.postgres.fake`.
- No GitHub issue numbers in code, comments, commit messages, or docs (PR body only).
- DbC: every public function/class documents `Preconditions:` / `Postconditions:` / (where relevant) `Invariants:`.
- ≥90% line coverage on new `fake.py`; `make lint` and targeted pytest must pass from `backend/`.
- Work only inside the worktree path above.

## File map

| Path | Responsibility |
|---|---|
| `backend/shared/postgres/fake.py` | `unwrap_json`, `FakeCursor`, `FakeConn`, `install_fake_postgres`, type aliases |
| `backend/shared/postgres/testing.py` | Existing truncate helpers + re-export fake public symbols |
| `backend/shared/postgres/tests/test_fake.py` | Unit tests with a tiny sample dispatch table |
| `backend/shared/postgres/README.md` | Short “in-memory fake” note under testing helpers |

---

### Task 1: `unwrap_json` + `FakeCursor` dispatch core

**Files:**
- Create: `backend/shared/postgres/tests/test_fake.py`
- Create: `backend/shared/postgres/fake.py`

**Interfaces:**
- Consumes: none (new module)
- Produces:
  - `unwrap_json(value: Any) -> Any`
  - `SqlMatcher = Callable[[str], bool]`
  - `SqlHandler = Callable[["FakeCursor", tuple], None]`
  - `DispatchTable = Sequence[tuple[SqlMatcher, SqlHandler]]`
  - `FakeCursor(db, dispatch, ids=None, row_factory=None)` with `execute`, `fetchone`, `fetchall`, `set_one`, `set_all`, `rowcount`, `db`, `ids`, context manager

- [ ] **Step 1: Write the failing tests**

Create `backend/shared/postgres/tests/test_fake.py`:

```python
"""Unit tests for ``shared.postgres.fake`` (no live Postgres)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from shared.postgres.fake import FakeConn, FakeCursor, install_fake_postgres, unwrap_json


def test_unwrap_json_plain_value():
    assert unwrap_json({"a": 1}) == {"a": 1}
    assert unwrap_json("x") == "x"
    assert unwrap_json(None) is None


def test_unwrap_json_obj_wrapper():
    wrapped = SimpleNamespace(obj={"k": "v"})
    assert unwrap_json(wrapped) == {"k": "v"}


def _sample_dispatch():
    def match_insert(norm: str) -> bool:
        return norm.startswith("insert into items")

    def handle_insert(cur: FakeCursor, params: tuple) -> None:
        item_id, payload = params
        cur.db.setdefault("items", {})[item_id] = unwrap_json(payload)
        cur.rowcount = 1

    def match_select_one(norm: str) -> bool:
        return norm.startswith("select payload from items where id")

    def handle_select_one(cur: FakeCursor, params: tuple) -> None:
        (item_id,) = params
        row = cur.db.get("items", {}).get(item_id)
        cur.set_one({"payload": row} if row is not None else None)

    def match_select_all(norm: str) -> bool:
        return norm.startswith("select payload from items")

    def handle_select_all(cur: FakeCursor, params: tuple) -> None:
        cur.set_all([{"payload": v} for v in cur.db.get("items", {}).values()])

    def match_next_id(norm: str) -> bool:
        return norm.startswith("insert into notes")

    def handle_next_id(cur: FakeCursor, params: tuple) -> None:
        (text,) = params
        note_id = next(cur.ids)
        cur.db.setdefault("notes", []).append({"id": note_id, "text": text})
        cur.set_one((note_id,))
        cur.rowcount = 1

    return [
        (match_insert, handle_insert),
        (match_select_one, handle_select_one),
        (match_select_all, handle_select_all),
        (match_next_id, handle_next_id),
    ]


def test_first_match_wins_and_fetchone():
    db: dict = {}
    cur = FakeCursor(db, _sample_dispatch())
    cur.execute("INSERT INTO items VALUES (%s, %s)", ("a", SimpleNamespace(obj={"n": 1})))
    assert cur.rowcount == 1
    assert db["items"]["a"] == {"n": 1}

    cur.execute("SELECT payload FROM items WHERE id = %s", ("a",))
    assert cur.fetchone() == {"payload": {"n": 1}}


def test_fetchall_and_whitespace_normalization():
    db = {"items": {"a": 1, "b": 2}}
    cur = FakeCursor(db, _sample_dispatch())
    cur.execute(
        """
        SELECT   payload
        FROM items
        """
    )
    assert cur.fetchall() == [{"payload": 1}, {"payload": 2}]


def test_unmatched_sql_raises_with_original_string():
    cur = FakeCursor({}, _sample_dispatch())
    sql = "DELETE FROM items WHERE id = %s"
    with pytest.raises(AssertionError, match=r"unexpected SQL in fake cursor"):
        cur.execute(sql, ("a",))


def test_shared_ids_across_executes():
    db: dict = {}
    cur = FakeCursor(db, _sample_dispatch())
    cur.execute("INSERT INTO notes VALUES (%s)", ("one",))
    assert cur.fetchone() == (1,)
    cur.execute("INSERT INTO notes VALUES (%s)", ("two",))
    assert cur.fetchone() == (2,)
    assert [n["id"] for n in db["notes"]] == [1, 2]


def test_cursor_context_manager():
    cur = FakeCursor({}, _sample_dispatch())
    with cur as entered:
        assert entered is cur
```

- [ ] **Step 2: Run tests to verify they fail**

Run from `backend/`:

```bash
cd backend && python -m pytest shared/postgres/tests/test_fake.py -v
```

Expected: FAIL with `ModuleNotFoundError` / import error for `shared.postgres.fake`.

- [ ] **Step 3: Implement `fake.py` core**

Create `backend/shared/postgres/fake.py`:

```python
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
```

Note: Task 1 tests do not yet call `FakeConn` / `install_fake_postgres` beyond imports. Keep those symbols in the module so Task 2 tests import cleanly; if you prefer strict TDD, you may stub `FakeConn` / `install_fake_postgres` as `raise NotImplementedError` until Task 2 — either is fine as long as Task 1 tests pass.

- [ ] **Step 4: Run Task 1 tests**

```bash
cd backend && python -m pytest shared/postgres/tests/test_fake.py -v -k "not install and not FakeConn and not nested"
```

Or simply run the whole file if Task 1 only contains the tests shown above (no install tests yet):

```bash
cd backend && python -m pytest shared/postgres/tests/test_fake.py -v
```

Expected: all Task 1 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/shared/postgres/fake.py backend/shared/postgres/tests/test_fake.py
git commit -m "$(cat <<'EOF'
Add FakeCursor scaffold and unwrap_json for shared postgres tests.

EOF
)"
```

---

### Task 2: `FakeConn` + `install_fake_postgres` coverage

**Files:**
- Modify: `backend/shared/postgres/tests/test_fake.py`
- Modify: `backend/shared/postgres/fake.py` (only if stubs remain from Task 1)

**Interfaces:**
- Consumes: `FakeCursor`, `FakeConn`, `install_fake_postgres`, `_sample_dispatch` from Task 1
- Produces: tests locking install patching, shared db/ids, nested context managers

- [ ] **Step 1: Append failing install / conn tests**

Append to `backend/shared/postgres/tests/test_fake.py`:

```python
def test_fake_conn_shares_db_and_ids():
    db: dict = {}
    conn = FakeConn(db, _sample_dispatch())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO notes VALUES (%s)", ("a",))
        assert cur.fetchone() == (1,)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO notes VALUES (%s)", ("b",))
        assert cur.fetchone() == (2,)
    assert db["notes"][0]["id"] == 1
    assert db["notes"][1]["id"] == 2


def test_install_fake_postgres_patches_modules(monkeypatch):
    class StoreMod:
        get_conn = object()

    class OtherMod:
        get_conn = object()

    store = StoreMod()
    other = OtherMod()
    db = install_fake_postgres(
        monkeypatch,
        modules=[store, other],
        dispatch=_sample_dispatch(),
        db={"items": {}},
    )
    assert db == {"items": {}}

    with store.get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO items VALUES (%s, %s)",
            ("x", {"z": 9}),
        )
        assert cur.rowcount == 1

    with other.get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT payload FROM items WHERE id = %s", ("x",))
        assert cur.fetchone() == {"payload": {"z": 9}}


def test_install_uses_default_empty_db(monkeypatch):
    class Mod:
        get_conn = None

    mod = Mod()
    db = install_fake_postgres(monkeypatch, modules=[mod], dispatch=_sample_dispatch())
    assert db == {}
    with mod.get_conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO items VALUES (%s, %s)", ("k", {"v": 1}))
    assert db["items"]["k"] == {"v": 1}


def test_cursor_preserves_row_factory():
    conn = FakeConn({}, _sample_dispatch())
    sentinel = object()
    cur = conn.cursor(row_factory=sentinel)
    assert cur.row_factory is sentinel
```

- [ ] **Step 2: Run tests**

```bash
cd backend && python -m pytest shared/postgres/tests/test_fake.py -v
```

Expected: PASS (implementation already complete from Task 1). If any fail, fix `FakeConn` / `install_fake_postgres` to match the contracts above.

- [ ] **Step 3: Commit**

```bash
git add backend/shared/postgres/tests/test_fake.py backend/shared/postgres/fake.py
git commit -m "$(cat <<'EOF'
Cover FakeConn and install_fake_postgres with unit tests.

EOF
)"
```

---

### Task 3: Re-exports, README, lint, and suite verification

**Files:**
- Modify: `backend/shared/postgres/testing.py`
- Modify: `backend/shared/postgres/README.md`

**Interfaces:**
- Consumes: public symbols from `shared.postgres.fake`
- Produces: `from shared.postgres.testing import FakeCursor, FakeConn, install_fake_postgres, unwrap_json` works; README documents the fake

- [ ] **Step 1: Re-export from `testing.py`**

At the end of `backend/shared/postgres/testing.py`, after the existing helpers, add:

```python
# Re-export the in-memory fake scaffold so callers can import from either
# ``shared.postgres.testing`` or ``shared.postgres.fake``.
from shared.postgres.fake import (  # noqa: E402
    FakeConn,
    FakeCursor,
    install_fake_postgres,
    unwrap_json,
)

__all__ = [
    "FakeConn",
    "FakeCursor",
    "drop_team_tables",
    "install_fake_postgres",
    "truncate_all_teams",
    "truncate_team_tables",
    "unwrap_json",
]
```

If `testing.py` already has an `__all__`, extend it instead of replacing unrelated exports. If there is no prior `__all__`, the block above is fine — but keep `drop_team_tables` / truncate names accurate to what the module already defines.

Also update the module docstring’s first paragraph to mention both truncate helpers and the re-exported fake scaffold.

- [ ] **Step 2: Add a test that the re-export path works**

Append to `test_fake.py`:

```python
def test_reexport_from_testing():
    from shared.postgres import testing as testing_mod

    assert testing_mod.FakeCursor is FakeCursor
    assert testing_mod.FakeConn is FakeConn
    assert testing_mod.install_fake_postgres is install_fake_postgres
    assert testing_mod.unwrap_json is unwrap_json
```

- [ ] **Step 3: Document in README**

In `backend/shared/postgres/README.md`, after the existing

```python
from shared.postgres.testing import truncate_team_tables, truncate_all_teams
```

block (API section), add a subsection:

```markdown
## In-memory fake (unit tests)

For store unit tests that must not hit live Postgres, use the dispatch-table
scaffold in `shared.postgres.fake` (also re-exported from
`shared.postgres.testing`):

```python
from shared.postgres.fake import FakeCursor, install_fake_postgres, unwrap_json

def _dispatch():
    return [
        (
            lambda n: n.startswith("insert into my_table"),
            lambda cur, params: cur.db.setdefault("rows", {}).update(...),
        ),
    ]

def test_store(monkeypatch):
    import my_team.store as store_mod
    db = install_fake_postgres(
        monkeypatch,
        modules=[store_mod],
        dispatch=_dispatch(),
        db={"rows": {}},
    )
    # exercise store_mod against db ...
```

Teams keep their SQL handler tables locally; this module only owns cursor/conn
install boilerplate. Live-Postgres truncate helpers (`truncate_team_tables`)
remain separate.
```

Keep the fenced example valid markdown (nested fences: use a 4-backtick outer fence or indent the inner example if needed so the README renders).

- [ ] **Step 4: Run lint and tests from `backend/`**

```bash
cd backend && make lint
cd backend && python -m pytest shared/postgres/tests/test_fake.py shared/postgres/tests/test_shared_postgres.py -v --cov=shared.postgres.fake --cov-report=term-missing
```

Expected:
- ruff clean
- all listed tests PASS
- `shared.postgres.fake` line coverage ≥ 90%

If full `make test` is feasible in the environment, run it; otherwise at least the shared postgres suite above plus confirm no team fake files changed:

```bash
git status -- backend/agents/*/tests/_fake_postgres.py
```

Expected: clean (no modifications).

- [ ] **Step 5: Commit**

```bash
git add backend/shared/postgres/testing.py backend/shared/postgres/README.md backend/shared/postgres/tests/test_fake.py
git commit -m "$(cat <<'EOF'
Re-export postgres fake scaffold and document install usage.

EOF
)"
```

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| `fake.py` with FakeCursor / FakeConn / install / unwrap_json | 1–2 |
| Ordered callable `(matcher, handler)` dispatch | 1 |
| Normalize + AssertionError on miss | 1 |
| Imperative handlers + set_one/set_all | 1 |
| install kwargs: modules, dispatch, db, id_start, attr | 1–2 |
| Shared ids across cursors | 1–2 |
| Unit tests with sample dispatch | 1–2 |
| Re-export from testing.py | 3 |
| README note | 3 |
| No team `_fake_postgres.py` changes | Global + Task 3 status check |
| make lint / tests | 3 |

## Self-review notes

- No placeholders left in steps; full code included for tests and implementation.
- Type names (`SqlMatcher`, `DispatchTable`, `set_one`, `id_start`) are consistent across tasks.
- `attr` default `"get_conn"` is implemented even though sample tests use the default only — covered by the install signature in Task 1 code.
