# Design: Agent Studio drafts store and schema

Date: 2026-08-07

## Goal

Implement the user-scoped `agent_studio_drafts` persistence layer (Postgres when
configured; in-memory for local/dev otherwise) holding handoff state plus partial
Stage-1 work as an opaque payload blob. Drafts can be created, loaded, renamed,
and deleted in the store with per-user isolation — ready for HTTP routes in the
follow-on issue.

## Context

Epic: Agent Studio drafts API and Stage-1 authoring UI spine. This is sub-issue
(1/5): store and schema only.

UX contract: `docs/design/agent-studio-ux-spec.md` §3.5 / §5 item 4. List returns
lightweight summaries; get returns the full draft; pagination default 50 / max
100; ordered most-recent `updated_at` first; all ops scoped to the authenticated
user id.

Existing Agent Studio conversation persistence (`store.py` / `pg_store.py` /
`postgres.SCHEMA`) is the twin-backend pattern to mirror. Conversations are
**not** user-scoped today; drafts are a separate concern.

Out of scope: HTTP routes, Angular UI, manifest identity, wiring into
`AgentStudioService`, optional `?q=` name filter.

## Decisions

| Topic | Choice |
|---|---|
| Payload shape | Opaque `payload_json` / `payload: dict` — server does not interpret stage fields |
| Backends | Twin modules mirroring conversations: in-memory + Postgres, shared public surface |
| Create vs update | Create only when id omitted; update only if owned by `user_id`; missing/wrong user → `None` / `False` |
| Package layout | Parallel twin modules at package root (not a `drafts/` subpackage) |
| SCHEMA | Extend existing `agent_studio` `TeamSchema` (already registered from unified API lifespan) |
| In-memory eviction | No LRU — process-lifetime store for local/dev; document in module docstring |
| Service wiring | Factory only (`get_draft_store`); not injected into `AgentStudioService` yet |

## Schema

Extend `backend/agents/agent_team_studio/agent_studio/postgres/__init__.py`:

```sql
CREATE TABLE IF NOT EXISTS agent_studio_drafts (
    draft_id     TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    name         TEXT NOT NULL,
    payload_json JSONB NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_agent_studio_drafts_user_updated
    ON agent_studio_drafts (user_id, updated_at DESC);
```

Append `agent_studio_drafts` to `table_names` (after conversation tables is fine;
no FK dependency).

## Models

Add to `models.py`:

- `AgentStudioDraftSummary` — `draft_id: str`, `name: str`, `updated_at: str`
  (ISO-8601)
- `AgentStudioDraft` — summary fields plus `payload: dict[str, Any]` (opaque
  handoff + stage blob) and `created_at: str` for completeness on full get

Server owns `draft_id`, `created_at`, and `updated_at`. On create, `name`
defaults to a timestamp string when omitted; `payload` defaults to `{}`.

## Store API

Both `AgentStudioDraftStore` (in-memory, `drafts_store.py`) and
`PostgresAgentStudioDraftStore` (`drafts_pg_store.py`) expose:

| Method | Behavior |
|---|---|
| `create(user_id, *, name=None, payload=None) → AgentStudioDraft` | Mint `uuid4`; default name/payload; set timestamps |
| `update(user_id, draft_id, *, name=None, payload=None) → AgentStudioDraft \| None` | Patch provided fields if owned; bump `updated_at`; else `None` |
| `get(user_id, draft_id) → AgentStudioDraft \| None` | Full record or `None` |
| `list_summaries(user_id, *, limit=50, offset=0) → list[AgentStudioDraftSummary]` | Clamp limit to `[1, 100]`; `offset ≥ 0` (clamp negative to 0); `ORDER BY updated_at DESC` |
| `rename(user_id, draft_id, name) → AgentStudioDraftSummary \| None` | Name-only; `None` if missing/wrong user |
| `delete(user_id, draft_id) → bool` | `True` if deleted; `False` if missing/wrong user |

`drafts_runtime.py` provides `get_draft_store()`: Postgres when
`is_postgres_enabled()`, else in-memory; same `ImportError` fallback as
conversation runtime.

In-memory store is thread-safe via `threading.Lock`. Postgres store uses
`get_conn`, `Json(...)`, and `@timed_query` like `pg_store.py`. Every SQL
predicate includes `user_id`.

## Tenancy and errors

- Wrong-user access is indistinguishable from not-found (`None` / `False`).
- Preconditions (raise `ValueError`): non-empty `user_id`; non-empty `name` when
  provided / on rename; `payload` is a `dict` when provided.
- Routes (#5701) map `None`/`False` → 404 and `ValueError` → 400.

## Files

| Action | Path |
|---|---|
| Modify | `agent_studio/models.py` |
| Modify | `agent_studio/postgres/__init__.py` |
| Create | `agent_studio/drafts_store.py` |
| Create | `agent_studio/drafts_pg_store.py` |
| Create | `agent_studio/drafts_runtime.py` |
| Create | `agent_studio/tests/test_drafts_store.py` |
| Create | `agent_studio/tests/test_drafts_pg_store.py` |
| Create | this design doc |

No changes to `unified_api/main.py` (SCHEMA already registered), conversation
stores, or HTTP routes.

## Testing

`test_drafts_store.py` (always-on unit):

- Create → get full payload round-trip
- Update / rename / delete happy path
- Tenancy: user A cannot get, update, rename, list, or delete user B's draft
- Pagination: default 50, clamp max 100, offset slicing, most-recent-first order
- Preconditions: empty `user_id` / empty rename name / non-dict payload raise

`test_drafts_pg_store.py` (live Postgres, skip when unset — same pattern as
`test_pg_store.py`): register SCHEMA, truncate, exercise the same tenancy and
pagination cases against `PostgresAgentStudioDraftStore`.

## Non-goals

- HTTP route group `/api/agent-studio/drafts`
- Frontend client / Save-Load header UX
- Interpreting or validating `stage1AgentDraft` / roster / handoff fields
- Optional list name filter (`?q=`)
- Auto-save or TTL eviction policies
