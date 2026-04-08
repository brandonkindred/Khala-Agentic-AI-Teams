# shared_postgres

Shared Postgres schema registration for Strands agent teams. Sibling to
`shared_temporal/`: each team declares its tables once, and the team's
FastAPI lifespan applies them at startup when `POSTGRES_HOST` is set.

## Why

Before this module, Postgres DDL lived in three places:

1. `backend/job_service/db.py::ensure_schema()` — one hand-rolled call in a lifespan
2. `backend/unified_api/postgres_encrypted_credentials.py` — `CREATE TABLE IF NOT EXISTS` run on **every** read/write
3. `docker/postgres/init/*.sql` — fires **only** on first container init; silent after that

SQLite-backed teams (branding, startup_advisor, user_agent_founder,
team_assistant, agentic_team_provisioning, blogging) had no Postgres
story at all. `shared_postgres` unifies all of this behind one pattern.

## The pattern

### 1. Each team exports a `TeamSchema` as pure data

`backend/agents/<team>/postgres/__init__.py`:

```python
from shared_postgres import TeamSchema

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

from shared_postgres import close_pool, register_team_schemas
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

`shared_temporal` uses **Pattern A** — `temporal/__init__.py` calls
`start_team_worker(...)` at module-import time, which launches a
daemon thread. That works because:

- Temporal workers run in a background thread, so import-time kicks
  never block the main flow.
- Worker startup failures are caught inside the thread.

`shared_postgres` uses **Pattern B** — the team exports only data,
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

## API

```python
from shared_postgres import (
    TeamSchema,              # dataclass — the data contract
    is_postgres_enabled,     # bool gate
    register_team_schemas,   # no-op when disabled; else runs DDL
    ensure_team_schema,      # raises if disabled; forces DDL run
    get_conn,                # context manager (database override)
    close_pool,              # lifespan shutdown
    register_all_team_schemas,  # CLI / test-harness helper
    TEAM_POSTGRES_MODULES,   # registry dict
)
```

## The registry

`TEAM_POSTGRES_MODULES` in `registry.py` maps each team slug to its
`<team>.postgres` dotted path. `register_all_team_schemas()` imports
each module lazily and applies its `SCHEMA`. The unified API does
**not** call this — each team container registers its own schema from
its own lifespan. `register_all_team_schemas` exists for CLI
migrations and test harnesses.

## See also

- `backend/agents/shared_temporal/README.md` — sibling module for
  Temporal workflow registration.
- `backend/job_service/db.py` — original `ensure_schema()` pattern this
  module generalizes.
