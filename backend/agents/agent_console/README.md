# agent_console

Phase 3 data layer for the Agent Console Runner. Lives **in-process** inside
the unified API (like `agent_registry`) — it is not a team container.

## What it owns

Two Postgres tables in the shared Khala database (namespaced `agent_console_`):

- `agent_console_saved_inputs` — user-saved payloads per agent, unique on
  `(agent_id, name)`.
- `agent_console_runs` — one row per invocation, indexed on
  `(agent_id, created_at DESC)` for history pagination.

Plus three in-process helpers:

- `resolve_author()` — reads the shared `AuthorProfile` to tag every row with
  a stable handle (placeholder for real auth).
- `unified_json_diff()` — pretty-prints both sides and runs
  `difflib.unified_diff`.
- `run_pruner()` — background asyncio task that keeps the newest N runs per
  agent.

## API surface

Routes are mounted on the unified API from
`backend/unified_api/routes/`:

| Route | Purpose |
|---|---|
| `POST /api/agents/{id}/invoke?saved_input_id=...` | Phase 2 invoke, now also persists to `agent_console_runs`. |
| `GET  /api/agents/{id}/runs?limit&cursor` | Paginated run history (newest-first). |
| `GET  /api/agents/runs/{run_id}` | One run with full input/output/logs. |
| `DELETE /api/agents/runs/{run_id}` | Delete one run. |
| `GET  /api/agents/{id}/saved-inputs` | List saved inputs for an agent. |
| `POST /api/agents/{id}/saved-inputs` | Create; 409 on duplicate name. |
| `GET  /api/agents/saved-inputs/{saved_id}` | Fetch one. |
| `PUT  /api/agents/saved-inputs/{saved_id}` | Update name/body/description. |
| `DELETE /api/agents/saved-inputs/{saved_id}` | Delete. |
| `POST /api/agents/diff` | Unified-diff two payloads (run, saved input, or inline). |

## Storage contract

Reuses `shared_postgres` exactly like blogging and branding:

- `SCHEMA: TeamSchema` exported from `agent_console.postgres` (pure data).
- `register_team_schemas(SCHEMA)` called once from the unified API lifespan.
- `get_conn()` for queries; `@timed_query(store="agent_console", op=...)` for
  slow-query logging.
- When `POSTGRES_HOST` is unset, every store method raises
  `AgentConsoleStorageUnavailable`, which the routes translate to HTTP 503
  with a clear detail. The Runner UI shows an empty-state banner in that
  case instead of erroring.

## Retention

Background pruner trims `agent_console_runs` down to the newest
`AGENT_CONSOLE_RUNS_RETENTION` rows per `agent_id` every
`AGENT_CONSOLE_PRUNE_INTERVAL_S` seconds. Defaults: **200** rows, **3600**
seconds. Uses a single round-trip `DELETE ... WHERE id IN (SELECT ... window
function)` so the prune is O(1) in round trips regardless of the backlog.

## Author handle

`resolve_author()` wraps `blogging.author_profile.load_author_profile()`
with priority:

1. `identity.short_name`
2. `identity.full_name`
3. `"anonymous"`

Cached via `lru_cache(maxsize=1)`. Never raises — a broken or missing profile
falls back to `"anonymous"` so the invoke path is never blocked. When real
auth arrives, replace `resolve_author()` with a session-derived user id;
existing rows retain their current handle.

## Tests

Hermetic (no Postgres):

```bash
cd backend
python3 -m pytest agents/agent_console/tests/test_diff.py agents/agent_console/tests/test_author.py --asyncio-mode=auto
```

Live Postgres (skipped when `POSTGRES_HOST` unset):

```bash
POSTGRES_HOST=localhost POSTGRES_USER=postgres POSTGRES_PASSWORD=postgres \
POSTGRES_DB=postgres python3 -m pytest agents/agent_console/tests/ --asyncio-mode=auto
```

Route tests stub the store with an in-memory fake so `test_saved_inputs_route`
and `test_diff_route` never need live Postgres.

## Env vars

| Var | Default | Purpose |
|---|---|---|
| `AGENT_CONSOLE_RUNS_RETENTION` | `200` | Rows kept per agent_id. |
| `AGENT_CONSOLE_PRUNE_INTERVAL_S` | `3600` | Pruner cadence in seconds (min 60). |
