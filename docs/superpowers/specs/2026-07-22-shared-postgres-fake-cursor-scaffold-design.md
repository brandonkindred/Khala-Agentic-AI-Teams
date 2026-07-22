# Design: Shared Postgres FakeCursor / FakeConn scaffold

Date: 2026-07-22

## Goal

Extract the common in-memory Postgres test scaffold used by branding, team_assistant, agentic_team_provisioning, and user_profile into `shared.postgres`, so later migrations can supply only a SQL→handler dispatch table instead of re-copying cursor/conn/install boilerplate.

This change adds the scaffold and unit tests only. No team `tests/_fake_postgres.py` is migrated yet.

## Context

Four team test suites each implement nearly identical helpers:

- `_unwrap_json` for psycopg `Json` wrappers (`.obj`)
- `_FakeCursor` with SQL normalize + dispatch + `rowcount` / `fetchone` / `fetchall`
- `_FakeConn` with `cursor(row_factory=None)` as a context-manager-friendly stub
- `install_fake_postgres(monkeypatch)` that patches module-level `get_conn`

They differ only in backing `db` shape and SQL handler tables. Live-DB truncate helpers already live in `shared.postgres.testing`; the fake scaffold is a different concern and belongs in a sibling module.

## Decisions

| Topic | Choice |
|---|---|
| Package home | `backend/shared/postgres/` (not a revived `agents/shared_postgres`) |
| Module layout | New sibling `fake.py`; leave `testing.py` truncate helpers unchanged; thin re-exports from `testing.py` |
| Install API | `install_fake_postgres(monkeypatch, *, modules, dispatch, db=None, id_start=1, attr="get_conn")` |
| Dispatch shape | Ordered `(matcher, handler)` where matcher is `Callable[[str], bool]` |
| Handler contract | Imperative: `handler(cursor, params) -> None` mutates cursor / `cursor.db` |
| Scope | Scaffold + unit tests only; no team migrations |

## Module layout

```
backend/shared/postgres/
  fake.py          # NEW — FakeCursor, FakeConn, install_fake_postgres, unwrap_json
  testing.py       # existing truncate helpers + thin re-exports of fake public API
  tests/
    test_fake.py   # NEW — scaffold unit tests with a tiny sample dispatch table
```

Import paths:

```python
from shared.postgres.fake import FakeCursor, FakeConn, install_fake_postgres, unwrap_json
# also available via:
from shared.postgres.testing import FakeCursor, FakeConn, install_fake_postgres, unwrap_json
```

## Public API

### Types

- `SqlMatcher = Callable[[str], bool]` — receives normalized SQL
- `SqlHandler = Callable[[FakeCursor, tuple], None]`
- `DispatchTable = Sequence[tuple[SqlMatcher, SqlHandler]]` — first match wins

### `unwrap_json(value)`

If `hasattr(value, "obj")`, return `value.obj`; otherwise return `value`.

### `FakeCursor`

Constructed with `db`, `dispatch`, optional `ids=itertools.count(1)`, optional `row_factory`.

- Context manager (`__enter__` / `__exit__`)
- `execute(sql, params=())`:
  1. Normalize: `" ".join(sql.split()).lower()`
  2. Coerce `params` to `tuple`
  3. Walk `dispatch`; invoke first matching handler
  4. On miss: raise `AssertionError(f"unexpected SQL in fake cursor: {sql!r}")` using the original SQL string
- `fetchone()` / `fetchall()` / `rowcount` (default `0`)
- Mutators: `set_one(row)`, `set_all(rows)`
- Exposes `db`, `ids`, and the `row_factory` passed into `cursor(...)` for handlers that care

### `FakeConn`

- Holds shared `db`, `dispatch`, `ids`
- `cursor(row_factory=None)` returns a new `FakeCursor` sharing that state

### `install_fake_postgres`

```python
def install_fake_postgres(
    monkeypatch,
    *,
    modules: Sequence[Any],
    dispatch: DispatchTable,
    db: dict[str, Any] | None = None,
    id_start: int = 1,
    attr: str = "get_conn",
) -> dict[str, Any]:
```

Behavior:

1. Use provided `db` or default to a new `{}`
2. Build `ids = itertools.count(id_start)`
3. Define a context-manager fake `get_conn(database=None)` that yields `FakeConn(db, dispatch, ids)`
4. For each module in `modules`, `monkeypatch.setattr(mod, attr, fake_get_conn)`
5. Return the backing `db`

Teams with conditional patches (e.g. branding’s `hasattr` / already-imported API state) pass only the modules they want, or use `FakeConn` directly and write a thin local installer.

## Behavior invariants

- Normalization and unmatched-SQL error text match the existing team fakes
- `ids` is shared across all cursors from one install / conn (serial inserts)
- Handlers own all domain semantics (inserts, merges, ownership checks)
- `fake.py` is test-only; production stores must not import it

## Out of scope

- Migrating any team’s `tests/_fake_postgres.py` onto the scaffold
- Changing truncate / live-Postgres test helpers beyond re-exports
- Building a SQL parser or repository abstraction

## Testing

New `backend/shared/postgres/tests/test_fake.py` with a small sample dispatch table covering:

1. `unwrap_json` for plain values and `.obj` wrappers
2. First-match-wins matcher order
3. Handler sets `rowcount` + `fetchone` / `fetchall`
4. Unmatched SQL raises `AssertionError` with original SQL
5. `install_fake_postgres` patches listed modules’ `get_conn` and returns shared `db`
6. Shared `ids` counter across executes
7. Nested context-manager usage: `with get_conn() as conn, conn.cursor() as cur:`

Acceptance checks: `make test` and `make lint` pass from `backend/`.

## Docs

Add a short section in `backend/shared/postgres/README.md` under testing helpers pointing at `shared.postgres.fake` and showing a minimal dispatch + install example.

## Success criteria

- Scaffold exists under `shared.postgres.fake` with the API above
- Unit tests exercise the scaffold directly
- No team fake files change in this work
- Lint and tests pass from `backend/`
