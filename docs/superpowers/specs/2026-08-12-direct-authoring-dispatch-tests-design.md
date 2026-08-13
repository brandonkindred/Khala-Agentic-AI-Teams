# Direct authoring dispatch path tests

**Date:** 2026-08-12  
**Status:** Approved for implementation planning  
**Scope:** Agent Studio authoring CRUD tests only (`dispatch` direct branch + thin HTTP coverage)

## Problem

A dual-path dispatcher already exists in
`backend/agents/agent_team_studio/agent_studio/temporal/dispatch.py`: when
`is_temporal_enabled()` is false, `start_conversation` / `send_message` /
`clone_from_registry` / `save_agent` call `AgentStudioService` in-process via
`runtime.get_studio_service()`. Direct-path tests were left for a follow-up.

Existing tests force the Temporal branch:

- `test_temporal_dispatch.py` autouse-patches `_temporal_enabled()` to `True`
- `test_agent_studio_routes.py` does the same and stands in
  `execute_workflow_sync`

Both files comment that the direct path is “exercised separately.” Those tests
do not exist. If the `if not _temporal_enabled()` branches regress, CI will not
notice.

## Goal

Regression-cover the direct dispatch path for the authoring CRUD set
(start / send / clone / save) at two layers:

1. **Dispatch unit tests** that mirror `test_temporal_activity.py` on the new
   entrypoint (`dispatch.*` helpers with Temporal forced off).
2. **Thin route tests** that prove the HTTP mapping still works when there is
   no Temporal wrapping.

## Non-goals

- Do **not** change `dispatch.py`, routes, activities, or workflows.
- Do **not** optionalize or remove the Studio Temporal worker.
- Do **not** parametrize or rewrite the existing Temporal-path tests.
- Do **not** duplicate service-level cases already covered in
  `test_service.py` or Temporal route tests (server-owned fields, operating
  states, clone-must-not-register, in-place save overwrite). Those are
  `AgentStudioService` behavior, shared by both dispatch modes.

## Decisions (locked)

| Decision | Choice |
|---|---|
| Layers | Dispatch unit tests + thin HTTP route tests |
| Temporal tests | Leave as-is (still force Temporal on) |
| Fixture sharing | Do not extract shared fixtures from the Temporal route file; duplicate the small helpers |
| Service seam | Monkeypatch `agent_team_studio.agent_studio.runtime.get_studio_service` (the lazy import `_direct_service()` uses) |
| Temporal guard | Autouse-patch `dispatch._temporal_enabled` to `False`; also assert `execute_workflow_sync` is never called |
| Error contract | Direct path must raise native `ValueError` / `LookupError` / other exceptions — never `ApplicationError` |
| Return shapes | Direct path returns service objects (`ConversationStateResponse`, `AgentDefinition`, `(AgentManifest, bool)`), not activity dicts |

## Architecture

```
HTTP route  →  dispatch.start/send/clone/save
                    │
                    ├─ Temporal on  → workflow → activity → AgentStudioService
                    │                 (already tested)
                    │
                    └─ Temporal off → AgentStudioService  (this spec)
```

Both branches already share one process-wide `AgentStudioService` singleton.
The tests pin the off-branch wiring, not the service’s business logic.

### File 1: dispatch unit tests

**Create:** `backend/agents/agent_team_studio/agent_studio/tests/test_direct_dispatch.py`

Mirror of `test_temporal_activity.py`, targeting `dispatch.start_conversation`,
`dispatch.send_message`, `dispatch.clone_from_registry`, and
`dispatch.save_agent` instead of the four activities.

**Fixtures**

- Autouse: `monkeypatch.setattr(dispatch, "_temporal_enabled", lambda: False)`.
- `service`: `Mock(spec=AgentStudioService)` installed at
  `agent_team_studio.agent_studio.runtime.get_studio_service`.
- Autouse guard: patch `dispatch.execute_workflow_sync` to raise
  `AssertionError` if invoked.

**Required cases (one test each unless noted)**

| Helper | Happy path | Contract errors | Other |
|---|---|---|---|
| `start_conversation` | returns the service `ConversationStateResponse` unchanged; called with `("new", None, "hi")` | `ValueError` and `LookupError` re-raised as the same native type (not `ApplicationError`) | `RuntimeError` propagates |
| `send_message` | returns service response; called with `("c9", "make a planner")` | `LookupError` re-raised natively | — |
| `clone_from_registry` | returns service `AgentDefinition`; called with `("blogging.planner",)` | `LookupError` re-raised natively | — |
| `save_agent` | returns `(manifest, True)` tuple (not `{"manifest", "created"}`); called with the same `AgentDefinition` instance | `ValueError` re-raised natively | — |

Subclass parity with the activity tests (those map subclasses onto base
`ApplicationError.type` markers; the direct path must **not** remap):

- A `ValueError` subclass from `start_conversation` is re-raised as that
  subclass (still a `ValueError`, never `ApplicationError`).
- A `KeyError` from `send_message` is re-raised as `KeyError` (still a
  `LookupError`, never `ApplicationError`).

Happy-path assertions compare object identity/equality of the service return
value. They must **not** call `.model_dump()` and round-trip through
`model_validate` the way the activity tests do — that round-trip is the
Temporal serialization contract, which this path does not have.

### File 2: thin route tests

**Create:** `backend/unified_api/tests/test_agent_studio_direct_routes.py`

Same TestClient + scripted `AgentStudioService` pattern as
`test_agent_studio_routes.py`, with these differences:

- `_temporal_enabled()` forced to `False`.
- `execute_workflow_sync` patched to raise `AssertionError` if called (no
  inline activity stand-in).
- `get_studio_service` still patched to the scripted/fake-registry service.

**Required HTTP cases**

Success (scripted assistant + `FakeRegistry`):

- `POST /api/agent-studio/conversations` `{"mode": "new"}` → 200, has
  `conversation_id`.
- `POST /api/agent-studio/conversations/{id}/messages` after start → 200.
- `POST /api/agent-studio/agents/from-registry/blogging.planner` → 200,
  `mode == "refine"`.
- `POST /api/agent-studio/agents` ready definition → 200, `created is True`.

Error mapping (proves native exceptions still hit the route’s
`ValueError` → 400 / `LookupError` → 404 without Temporal translation):

- start refine without source → 400
- start refine unknown source → 404
- send to unknown conversation → 404
- mocked `send_message` `ValueError` → 400
- clone unknown agent → 404
- save missing `role` → 400
- mocked `save_agent` `RuntimeError` with
  `raise_server_exceptions=False` → 500

Do not copy the Temporal file’s remaining cases (Pydantic 422s, server-owned
fields, states seeding/normalization, clone-must-not-register, in-place
overwrite). Those are independent of dispatch mode.

### Comment updates

In `test_temporal_dispatch.py` and `test_agent_studio_routes.py`, replace the
“exercised separately” claim with an explicit pointer to
`test_direct_dispatch.py` / `test_agent_studio_direct_routes.py`. No other
edits to those files.

## Error handling

The direct path has no `_translate_workflow_failure` step. Tests fail the
contract if:

- a service `ValueError`/`LookupError` is wrapped as `ApplicationError` or
  `WorkflowFailureError`
- `execute_workflow_sync` runs
- `save_agent` returns a dict instead of `(AgentManifest, bool)`

Route tests use the same HTTP mapping as production
(`unified_api/routes/agent_studio.py`): `ValueError` → 400, `LookupError` →
404, anything else → FastAPI 500.

## Testing

Run from `backend/` with the existing venv:

```
pytest agents/agent_team_studio/agent_studio/tests/test_direct_dispatch.py \
       unified_api/tests/test_agent_studio_direct_routes.py \
       agents/agent_team_studio/agent_studio/tests/test_temporal_dispatch.py \
       agents/agent_team_studio/agent_studio/tests/test_temporal_activity.py \
       unified_api/tests/test_agent_studio_routes.py -q
```

All new tests plus the existing 51 Temporal-path tests must pass. No
production code changes, so no coverage-gap on `dispatch.py`’s off-branch
should remain for the four public helpers.

## Rollout

Tests only. No feature flag, no migration, no worker/lifespan change.
