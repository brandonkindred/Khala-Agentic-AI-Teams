# shared.postgres

Shared Postgres schema registration for Khala agent teams. Sibling to
`shared.temporal`: each team declares its tables once, and the team's
FastAPI lifespan applies them at startup when `POSTGRES_HOST` is set.

## Why

Before this module, Postgres DDL lived in three places:

1. `backend/job_service/db.py::ensure_schema()` — one hand-rolled call in a lifespan
2. `backend/unified_api/postgres_encrypted_credentials.py` — `CREATE TABLE IF NOT EXISTS` run on **every** read/write
3. `docker/postgres/init/*.sql` — fires **only** on first container init; silent after that

SQLite-backed teams (branding, startup_advisor, user_agent_founder,
team_assistant, agentic_team_provisioning, blogging) had no Postgres
story at all. `shared.postgres` unifies all of this behind one pattern.

## The pattern

### 1. Each team exports a `TeamSchema` as pure data

`backend/agents/<team>/postgres/__init__.py`:

```python
from shared.postgres import TeamSchema

SCHEMA = TeamSchema(
    team="branding",
    database=None,  # None = default POSTGRES_DB
    statements=[
        """CREATE TABLE IF NOT EXISTS branding_clients (
            id TEXT PRIMARY KEY,
            data JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        """CREATE INDEX IF NOT EXISTS idx_branding_clients_created ON branding_clients(created_at)""",
    ],
)
```

The module must be a **pure declaration**. No `ensure_team_schema`
call, no connection attempts, no top-level side effects.

### 2. The team's lifespan calls `register_team_schemas`

`backend/agents/<team>/api/main.py`:

```python
from contextlib import asynccontextmanager

from shared.postgres import close_pool, register_team_schemas
from branding_team.postgres import SCHEMA

@asynccontextmanager
async def _lifespan(app: FastAPI):
    try:
        register_team_schemas(SCHEMA)
    except Exception:
        logger.exception("branding postgres schema registration failed")
    yield
    try:
        close_pool()
    except Exception:
        pass
```

`register_team_schemas` is a no-op when `POSTGRES_HOST` is unset, so
local dev runs without Postgres keep working.

## Pattern A vs Pattern B

`shared.temporal` uses **Pattern A** — `temporal/__init__.py` calls
`start_team_worker(...)` at module-import time, which launches a
daemon thread. That works because:

- Temporal workers run in a background thread, so import-time kicks
  never block the main flow.
- Worker startup failures are caught inside the thread.

`shared.postgres` uses **Pattern B** — the team exports only data,
and the lifespan calls `register_team_schemas` explicitly. This is
required because:

- DDL is synchronous blocking I/O. Importing `branding_team.postgres`
  from a unit test, linter, or sibling tool would otherwise open a
  pooled connection and run `CREATE TABLE`.
- Lifespan ordering matters: logging and env vars must be initialized
  before DDL runs, which only Pattern B guarantees.
- Startup errors surface as lifespan log lines, not opaque
  `ModuleNotFoundError` chains.

## Environment

| Var | Default | Purpose |
|---|---|---|
| `POSTGRES_HOST` | (unset) | Gates everything — no host means no registration. |
| `POSTGRES_PORT` | `5432` | |
| `POSTGRES_USER` | `postgres` | |
| `POSTGRES_PASSWORD` | (empty) | |
| `POSTGRES_DB` | `postgres` | Default database; overridden per-team via `TeamSchema.database`. |
| `POSTGRES_POOL_MIN_SIZE` | `2` | Minimum connections kept in each per-database pool. |
| `POSTGRES_POOL_MAX_SIZE` | `10` | Maximum connections per pool (clamped to ≥ min). |
| `POSTGRES_SLOW_QUERY_MS` | `100` | `@timed_query` logs at INFO above this threshold, DEBUG below it. |

## Connection pooling

`get_conn()` acquires a connection from a process-wide `psycopg_pool.ConnectionPool`
lazily created per database name on first use. Commits on clean exit,
rolls back on exception, and returns the connection to the pool. Use it
for both startup DDL and hot-path CRUD — there is no need for a
dedicated pool per team anymore. Call `close_pool()` at shutdown to
close every pool this process opened.

Pool sizing is process-wide via env vars above. For high-throughput
teams, bump `POSTGRES_POOL_MAX_SIZE` in that team's container env
rather than adding a second pool.

## API

```python
from shared.postgres import (
    TeamSchema,              # dataclass — the data contract
    is_postgres_enabled,     # bool gate
    register_team_schemas,   # no-op when disabled; else runs DDL
    ensure_team_schema,      # raises if disabled; forces DDL run
    get_conn,                # context manager (pooled, database override)
    close_pool,              # lifespan shutdown — closes every pool
    register_all_team_schemas,  # CLI / test-harness helper
    TEAM_POSTGRES_MODULES,   # registry dict
    Json,                    # psycopg.types.json.Json re-export for JSONB inserts
    dict_row,                # psycopg.rows.dict_row re-export for cursor(row_factory=...)
    timed_query,             # @timed_query decorator for store methods
)
from shared.postgres.testing import truncate_team_tables, truncate_all_teams, real_postgres_schema
```

## In-memory fake (unit tests)

For store unit tests that must not hit live Postgres, use the dispatch-table
scaffold in `shared.postgres.fake` (also re-exported from
`shared.postgres.testing`):

````python
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
````

Teams keep their SQL handler tables locally; this module only owns cursor/conn
install boilerplate. Live-Postgres truncate helpers (`truncate_team_tables`)
remain separate.

## `TeamSchema.table_names`

When a team owns tables that tests need to reset between runs, populate
`TeamSchema.table_names` with the explicit list. Example:

```python
SCHEMA = TeamSchema(
    team="branding",
    statements=[ "CREATE TABLE IF NOT EXISTS branding_clients (...)", ... ],
    table_names=["branding_clients", "branding_brands", "branding_sessions"],
)
```

Test fixtures then call `truncate_team_tables(SCHEMA)` between tests —
no fragile regex parsing of the DDL.

## Real-Postgres pytest fixture

`shared.postgres.testing.real_postgres_schema(schema, *, scope="module", autouse=True)`
builds a ready-to-use pytest fixture for a team's `TeamSchema`:
it skips the test when `POSTGRES_HOST` is unset, registers the schema,
truncates before yielding and again on teardown **when run without
pytest-xdist**. The returned fixture defaults to `autouse=True`, so assigning
it is enough — tests in scope do not need to request it by name. Pass
`autouse=False` when you want an explicitly requested fixture instead. It
wraps the same `register_team_schemas` / `truncate_team_tables` calls that
hand-rolled real-Postgres fixtures previously duplicated (skip when
`POSTGRES_HOST` is unset, register schema, truncate around the test).
A team can opt a test module into real Postgres with one line instead of
re-deriving that boilerplate — `branding_team/tests/test_store.py` is the
canonical consumer:

```python
from branding_team.postgres import SCHEMA as BRANDING_SCHEMA
from shared.postgres.testing import real_postgres_schema

pytestmark = pytest.mark.integration

# Default module scope + autouse=True; use scope="function" when tests assert
# global row counts (as branding_team/tests/test_store.py does). Pass
# autouse=False for an explicitly requested fixture.
_branding_schema = real_postgres_schema(BRANDING_SCHEMA)
```

Like every other real-Postgres test in this repo, it assumes Postgres is
already reachable via `POSTGRES_HOST` — a CI `services:` container or a local
`docker compose -f docker/docker-compose.yml up -d postgres` — it does not
spin up a Postgres server itself (no `testcontainers` dependency).

**Under pytest-xdist (`-n`, any count) setup and teardown truncation are
skipped entirely**, not attempted per-worker: xdist instantiates a
module/session-scoped fixture independently per worker process, and its
default scheduling doesn't guarantee every test from one module lands on the
same worker, so an uncoordinated `TRUNCATE` from one worker could wipe rows a
sibling worker is still exercising. Coordinating a single truncate after every
worker finishes needs a real `pytest_sessionfinish` hook registered from a
`conftest.py` (that hook fires once in the xdist controller, which a bare
fixture can never reach) — until a team needs that guarantee, tests sharing a
schema under `-n` should isolate via unique row identifiers instead (the
existing convention — see `branding_team/tests/test_conversation_store.py`'s
`_brand_id()` / `uuid4`-suffixed data). Expect rows to accumulate across
repeated `-n` runs against a persistent local Postgres; CI's per-job
ephemeral Postgres container makes this a non-issue there.

## Observability

Wrap store methods with `@timed_query(store="<team>")`. Example:

```python
from shared.postgres import timed_query, get_conn

class BrandingStore:
    @timed_query(store="branding")
    def save_client(self, client):
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(...)
```

Logs go to the `shared.postgres.metrics` logger:
`store=branding op=save_client duration_ms=12 status=ok` at DEBUG, or
`status=ok slow=true` at INFO for slow queries.

## Tests

CI runs `shared/postgres/tests/` against a `postgres:16-alpine` service
container; the job runs `register_all_team_schemas()` first to catch
cross-team DDL conflicts before any per-team test. Local contributors
run `docker compose -f docker/docker-compose.yml up -d postgres` and
export the `POSTGRES_*` vars to hit the same code path.

Tests that don't need live Postgres mock `get_conn` via `monkeypatch`
as before — nothing forces them to connect.

## The registry

`TEAM_POSTGRES_MODULES` in `registry.py` maps each team slug to its
`<team>.postgres` dotted path. `register_all_team_schemas()` imports
each module lazily and applies its `SCHEMA`. The unified API does
**not** call this — each team container registers its own schema from
its own lifespan. `register_all_team_schemas` exists for CLI
migrations and test harnesses.

## See also

- `backend/shared/temporal/README.md` — sibling module for
  Temporal workflow registration.
- `backend/job_service/db.py` — original `ensure_schema()` pattern this
  module generalizes.
