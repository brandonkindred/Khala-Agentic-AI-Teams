# Design: Agent Studio drafts HTTP routes

Date: 2026-08-07

## Goal

Expose `/api/agent-studio/drafts` so Save/Load draft is satisfiable from the API
alone: list summaries, get full draft, create, update, rename, delete — all
scoped to the current user id.

## Context

Follow-on to the merged `agent_studio_drafts` store (in-memory + Postgres twins,
`get_draft_store()`). UX contract lives in `docs/design/agent-studio-ux-spec.md`
§3.5 / §5 item 4. Existing Studio routes are Temporal-backed conversations and
agents under `unified_api/routes/agent_studio.py` (tag `agent-studio`); drafts
are sync store CRUD and do not use Temporal.

Out of scope: Angular / API client, conversation/clone/save-agent changes,
optional `?q=` name filter, flat camelCase UX body mapping.

## Decisions

| Topic | Choice |
|---|---|
| User id | Pluggable FastAPI dependency `get_current_user_id()`; default returns `user_profile` `DEFAULT_USER_ID` (`"default"`); tests override |
| Create vs update | Create-only `POST /drafts`; full update via `PUT /drafts/{draft_id}` (intentional divergence from UX single upsert POST) |
| Request body | Opaque envelope `{ name?, payload? }` — no stage-field interpretation |
| Package layout | Extend existing `agent_studio.py` router (not a new sub-router) |
| Responses | Create/update/rename return `AgentStudioDraftSummary`; get returns full `AgentStudioDraft`; list returns summaries |
| Errors | `ValueError` → 400; store `None`/`False` → 404 (wrong-user ≡ not-found) |

## Endpoint surface

| Method | Path | Behavior |
|---|---|---|
| `POST` | `/api/agent-studio/drafts` | Create — body `SaveDraftRequest` → store `create`; returns summary |
| `PUT` | `/api/agent-studio/drafts/{draft_id}` | Update — body `SaveDraftRequest` → store `update`; 404 if missing/wrong user; returns summary |
| `GET` | `/api/agent-studio/drafts` | List summaries — `limit` default 50 (max 100), `offset` ≥ 0; store clamps; `updated_at DESC` |
| `GET` | `/api/agent-studio/drafts/{draft_id}` | Full draft or 404 |
| `PATCH` | `/api/agent-studio/drafts/{draft_id}` | Rename — body `RenameDraftRequest`; 404 / 400 as above |
| `DELETE` | `/api/agent-studio/drafts/{draft_id}` | Delete — 404 if missing; 200 `{ "draft_id", "status": "deleted" }` |

## Auth

```python
def get_current_user_id() -> str:
    """Resolve the caller user id for tenancy.

    Postconditions:
        * Returns a non-empty user id string. Default implementation returns
          ``DEFAULT_USER_ID`` until real auth is wired.
    """
    from user_profile.store import DEFAULT_USER_ID
    return DEFAULT_USER_ID
```

Every drafts handler takes `user_id: Annotated[str, Depends(get_current_user_id)]`
and passes it to the store. Document in the router module docstring that the
security gateway remains abuse scanning only; tenancy is enforced by this
dependency + store scoping.

## Models

Add to `agent_team_studio.agent_studio.models`:

- `SaveDraftRequest` — `name: str | None = None`, `payload: dict[str, Any] | None = None`
- `RenameDraftRequest` — `name: str` with `min_length=1`

Reuse existing `AgentStudioDraft` / `AgentStudioDraftSummary` as response models.

## Handler wiring

- Import `get_draft_store` from `agent_team_studio.agent_studio.drafts_runtime`.
- Sync `def` handlers (FastAPI threadpool), same style as other Studio routes.
- Map create/update results to summaries: `AgentStudioDraftSummary(draft_id=…, name=…, updated_at=…)`.
- Query params on list: `limit: int = 50`, `offset: int = 0` (store clamps).

## Testing

Create `backend/unified_api/tests/test_agent_studio_drafts_routes.py`:

- Mini FastAPI app + include `agent_studio` router; `TestClient`
- Use a fresh `AgentStudioDraftStore()` via monkeypatch of `get_draft_store` (or
  the routes-module binding)
- Override `get_current_user_id` for `"alice"` / `"bob"` tenancy cases
- Cover: create → list → get → put → patch → delete happy path
- Cover: 404 for missing id and cross-user get/put/patch/delete
- Cover: 400 for empty rename name
- Cover: list pagination query params (limit clamp behavior via store)

## Files

| Action | Path |
|---|---|
| Modify | `backend/unified_api/routes/agent_studio.py` |
| Modify | `backend/agents/agent_team_studio/agent_studio/models.py` |
| Create | `backend/unified_api/tests/test_agent_studio_drafts_routes.py` |
| Create | this design doc |

No `main.py` / `config.py` changes (router already mounted).

## Non-goals

- Frontend `AgentStudioApiService` / Save-Load UI
- Temporal wrappers for drafts
- Interpreting `stage1AgentDraft` / roster fields
- Optional list `?q=` filter
- Real JWT/session auth (dependency is the seam)
