# Design: Agentic API split (1/3) — teams + conversations

Date: 2026-08-07

## Goal

Extract teams CRUD (including roster) and conversation endpoints from
`agentic_team_provisioning/api/main.py` into dedicated routers and domain
services so those concerns no longer live as business logic inside the god
module.

This is story #5708 under epic #5688 (three-slice API split).

## Context

`api/main.py` is ~1770 lines and owns teams, roster, processes, conversations,
jobs, questions, assets, forms, test-chat, and test-pipeline. Non-HTTP domain
code already lives in `assistant/`, `runtime/`, `testing/`, etc.; HTTP is still
monolithic.

Closest in-repo template: `branding_team/api/` — thin `main` hub, `api/routes/*`
`APIRouter` modules, domain bodies in sibling modules, call-time `main`
dereference so tests keep `monkeypatch.setattr(main, …)`.

Sibling stories: #5709 (testing/pipeline), #5710 (assets/forms + thin hub + docs).

## Decisions

| Topic | Choice |
|---|---|
| Approach | Thin routers + `api/services/` domain modules + hub globals on `main` |
| Teams scope | CRUD **and** roster (agents list/manifests, validation, from-registry add/update/delete) |
| Conversations scope | create, send message, set process, get, list-by-team |
| Layout naming | Explicit `api/services/` (not branding’s flat `api/conversation.py`) |
| Collaborator access | Hub dereference: services import `main` inside functions |
| Shared helpers on hub | `_store`, `_agent`, `_save_agents_from_llm`, `_after_process_saved`, `_get_team_or_404` stay on `main` |
| Roster-only helpers | Move into `services/teams.py` (e.g. `_roster_agent_from_manifest`, registry unregister/reregister/cleanup) |
| Processes / jobs / questions / agent-envs | Stay inline in `main` this slice |
| URL contracts | Unchanged paths, status codes, response models |
| Docs / architecture | Deferred to #5710 |
| Tests | Keep patching `main`; no intentional contract or patch-target rewrites |

## Module layout

```
agentic_team_provisioning/api/
  main.py                      # app factory, globals, remaining inline routes, include_router
  routes/
    __init__.py
    teams.py                   # APIRouter: CRUD + roster
    conversations.py           # APIRouter: conversation endpoints
  services/
    __init__.py
    teams.py                   # team/roster handler bodies + roster-local helpers
    conversations.py           # conversation handler bodies + _build_state_response
```

### Teams router endpoints

| Method | Path |
|---|---|
| POST | `/teams` |
| GET | `/teams` |
| GET | `/teams/{team_id}` |
| GET | `/teams/{team_id}/agents` |
| GET | `/teams/{team_id}/agents/manifests` |
| GET | `/teams/{team_id}/roster/validation` |
| POST | `/teams/{team_id}/agents/from-registry` |
| DELETE | `/teams/{team_id}/agents/{agent_name:path}` |
| PUT | `/teams/{team_id}/agents/{agent_name:path}` |

### Conversations router endpoints

| Method | Path |
|---|---|
| POST | `/conversations` |
| POST | `/conversations/{conversation_id}/messages` |
| PUT | `/conversations/{conversation_id}/process` |
| GET | `/conversations/{conversation_id}` |
| GET | `/teams/{team_id}/conversations` |

## Call flow

1. Unified API / `TestClient(main.app)` hits `main.app`.
2. Mounted `APIRouter` handles the path.
3. Route handler calls into `services.teams` / `services.conversations`.
4. Service bodies resolve collaborators via function-local
   `from agent_team_studio.agentic_team_provisioning.api import main as _main`,
   then use `_main._store`, `_main._agent`, `_main._save_agents_from_llm`,
   `_main._get_team_or_404`, etc.

### Import-cycle rules

- Route modules define `router = APIRouter()` at import time; import services at
  module scope.
- Services (and routes if needed) import `main` **inside** functions, not at
  module top level.
- `main` mounts routers **last**, after globals and remaining inline routes are
  defined (same order as branding).

### Re-exports

Move roster-only helpers into `services/teams.py`, but **re-export any name
tests currently patch on `main`**. Known patch targets that must remain
reachable as `main.<name>` after the move:

- `_roster_agent_from_manifest` (`test_registry_roster.py`)
- `_save_agents_from_llm` (stays defined on `main`; conversation tests patch it)

`_find_agent_in_roster` is patched by test-chat tests and stays with remaining
inline routes (or stays on `main`) until a later slice.

## Errors

Preserve existing `HTTPException` status/detail behavior:

- 404 for missing team/conversation
- 403 for cross-team process attach on set-process
- 503 on registry failure during conversation flows
- create-team provision rollback behavior unchanged

No new error taxonomy in this slice.

## Testing

Gate on existing suites (no intentional contract breaks):

- `tests/test_create_team_rollback.py`
- `tests/test_registry_roster.py`
- `tests/test_agent_manifests_endpoint.py`
- `tests/test_conversation_registry_failure.py`
- `tests/test_set_conversation_process.py`

Plus any other tests that exercise the moved routes. Baseline before change:
59 passed across the five files above.

## Out of scope

- Testing/pipeline and test-chat extraction (#5709)
- Assets/forms extraction and final thin-hub / architecture docs (#5710)
- Processes, jobs, questions, agent-environments (remain inline)
- URL prefix changes
- UI cutover
- Temporal / Docker provisioning redesign
- Updating tests to patch `services.*` instead of `main`

## Acceptance criteria

- [ ] `api/routes/teams.py` and `api/routes/conversations.py` exist and are
      mounted from `main`
- [ ] `api/services/teams.py` and `api/services/conversations.py` own the moved
      logic
- [ ] `main.py` has no inline handlers for the teams/roster/conversation endpoint
      groups listed above
- [ ] Existing teams/conversation API tests pass
