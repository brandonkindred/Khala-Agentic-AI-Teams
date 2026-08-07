# Design: Agentic API split (2/3) — testing / pipeline

Date: 2026-08-07

## Goal

Extract interactive testing mode, test-chat, and test-pipeline endpoints from
`agentic_team_provisioning/api/main.py` into a dedicated router and domain
service so pipeline/test orchestration no longer lives as business logic in the
god module.

This is story #5709 under epic #5688 (three-slice API split). Slice 1 (#5708)
already extracted teams/roster and conversations.

## Context

After slice 1, `api/main.py` still owns processes, jobs, questions, assets,
forms, mode, test-chat, and test-pipeline (~1145 lines). The densest remaining
HTTP logic is test-chat + Temporal/thread pipeline dispatch.

Pattern established in slice 1: `api/routes/*` + `api/services/*`, hub
globals on `main`, function-local `from …api import main as _main`, re-export
monkeypatched helper names from `main`.

Sibling: #5710 (assets/forms + thin hub + docs).

## Decisions

| Topic | Choice |
|---|---|
| Approach | Mirror slice 1: thin router + `api/services/testing.py` + hub dereference |
| Scope | Mode + test-chat + test-pipeline (not pipeline-only) |
| Layout | Single pair: `api/routes/testing.py` + `api/services/testing.py` |
| Collaborator access | Hub dereference; `_test_store` / `_pipeline_runner` / `_store` stay on `main` |
| Helpers moved | `_find_agent_in_roster`, `_dispatch_pipeline_run`, and `_temporal_enabled` if only used by dispatch |
| Re-exports from `main` | `_find_agent_in_roster` (required — tests monkeypatch it); `_dispatch_pipeline_run` re-exported and called via `_main` so hub patches remain possible |
| Temporal vs thread | Preserve existing `_dispatch_pipeline_run` contract (single `temporal_owned` flag; no silent Temporal→thread downgrade) |
| URL contracts | Unchanged paths, status codes, response models |
| Docs / architecture | Deferred to #5710 |
| Tests | Keep patching `main`; extend scaffold for testing mounts/wiring |

## Module layout

```
agentic_team_provisioning/api/
  main.py                 # remaining inline routes; hub globals; include_router(testing)
  routes/
    testing.py            # APIRouter: mode + test-chat + test-pipeline
  services/
    testing.py            # handler bodies + roster lookup + pipeline dispatch
```

### Endpoints moved

| Method | Path |
|---|---|
| PUT | `/teams/{team_id}/mode` |
| POST | `/teams/{team_id}/test-chat/sessions` |
| GET | `/teams/{team_id}/test-chat/sessions` |
| GET | `/teams/{team_id}/test-chat/sessions/{session_id}` |
| PUT | `/teams/{team_id}/test-chat/sessions/{session_id}/name` |
| DELETE | `/teams/{team_id}/test-chat/sessions/{session_id}` |
| POST | `/teams/{team_id}/test-chat/sessions/{session_id}/messages` |
| GET | `/teams/{team_id}/test-chat/sessions/{session_id}/export` |
| PUT | `/teams/{team_id}/test-chat/messages/{message_id}/rating` |
| GET | `/teams/{team_id}/test-chat/quality-scores` |
| POST | `/teams/{team_id}/test-pipeline/runs` |
| GET | `/teams/{team_id}/test-pipeline/runs` |
| GET | `/teams/{team_id}/test-pipeline/runs/{run_id}` |
| POST | `/teams/{team_id}/test-pipeline/runs/{run_id}/input` |
| POST | `/teams/{team_id}/test-pipeline/runs/{run_id}/cancel` |

## Call flow

1. Unified API / `TestClient(main.app)` hits `main.app`.
2. Mounted `testing` `APIRouter` handles the path.
3. Route handler calls `services.testing`.
4. Service bodies resolve collaborators via function-local
   `from agent_team_studio.agentic_team_provisioning.api import main as _main`,
   then use `_main._store`, `_main._test_store`, `_main._pipeline_runner`,
   `_main._find_agent_in_roster`, `_main._dispatch_pipeline_run` as appropriate.

### Import-cycle rules

- Route module defines `router = APIRouter()` at import time; imports service at
  module scope.
- Service imports `main` **inside** functions only.
- `main` mounts routers **last** (with existing teams/conversations mounts).

### Re-exports

Move `_find_agent_in_roster` and `_dispatch_pipeline_run` into
`services/testing.py`, then re-export both from `main` so:

- `monkeypatch.setattr(main, "_find_agent_in_roster", …)` keeps working
  (`test_chat_session_starter_prompt_errors.py`).
- Handlers call dispatch through `_main._dispatch_pipeline_run` after the
  re-export binds, matching the roster-helper pattern from slice 1.

`_test_store` and `_pipeline_runner` remain defined on `main` (tests patch
methods on those objects).

## Errors

Preserve existing `HTTPException` status/detail behavior for unknown team,
missing session/run, roster agent not found, tenancy on ratings, pipeline
dispatch failures, etc. No new error taxonomy.

## Testing

Gate on existing suites (no intentional contract breaks):

- `tests/test_chat_session_starter_prompt_errors.py`
- `tests/test_send_test_chat_message_atomicity.py`
- `tests/test_rate_test_chat_message_tenancy.py`
- `tests/test_list_endpoints_team_404.py`
- `tests/test_temporal_dispatch.py`
- `tests/test_pipeline_runner.py` / `tests/test_pipeline_store.py` (still valid)

Extend `tests/test_api_router_scaffold.py` (or sibling) to assert representative
testing paths are mounted and that a service call resolves `_test_store` through
`main`.

Baseline before change: 59 passed across the chat/pipeline/temporal/list suites
listed above.

## Out of scope

- Assets/forms extraction and final thin-hub / architecture docs (#5710)
- Processes, jobs, questions, agent-environments (remain inline)
- Persona founder adapter changes
- URL prefix changes
- Temporal / Docker redesign
- Updating tests to patch `services.testing` instead of `main`

## Acceptance criteria

- [ ] `api/routes/testing.py` and `api/services/testing.py` own former `main`
      logic for mode, test-chat, and test-pipeline
- [ ] Pipeline start/status/cancel/input contracts unchanged (Temporal vs thread
      behavior intact)
- [ ] `main.py` has no inline handlers for those endpoint groups
- [ ] Related API tests pass
