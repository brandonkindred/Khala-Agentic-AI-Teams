# Design: SE startup fail-fast on missing/unreachable Temporal

Date: 2026-08-07

## Goal

Make `software_engineering_team` refuse to serve traffic when Temporal is
disabled or unreachable at boot. `_se_startup()` must raise (aborting ASGI
lifespan) instead of logging and continuing, so the process exits non-zero /
fails its readiness check rather than accepting work with no worker running.

## Context

Part of the Temporal-mandatory SE epic (parent #3969 under #3965). Shared
`start_team_worker` silently no-ops when `TEMPORAL_ADDRESS` is unset; that
shared behavior is out of scope. SE therefore needs its own explicit fail-fast
assertion ahead of starting either Temporal worker (SE and coding_team).

Today `_se_startup` wraps every step — including both worker starts — in
try/except log-and-continue, and is marked `# pragma: no cover`.

## Decisions

| Topic | Choice |
|---|---|
| Approach | Async `_assert_temporal_ready()` called at the top of `_se_startup` |
| Hook shape | Make `_se_startup` async so it can `await` the assert (`_maybe_call` already awaits awaitables) |
| Disabled Temporal | `not is_temporal_enabled()` → raise `RuntimeError` requiring `TEMPORAL_ADDRESS` |
| Connectivity | `await connect_temporal_client()` — real client connect, not address-only |
| Connect failure | Propagate the exception from `connect_temporal_client` |
| Probe vs worker client | Probe connection is throwaway; workers still connect via existing `start_team_worker` path |
| Other startup steps | Telemetry, CodeEngineProvider, and worker-start try/except blocks stay log-and-continue |
| Shared infra | Do not change `start_team_worker`'s no-op-when-disabled behavior |
| Testability | Assert helper is unit-tested; no live Temporal in default pytest |

## Behavior

`_assert_temporal_ready()` (new, async):

1. If `not is_temporal_enabled()` → raise `RuntimeError` with a message that
   SE requires `TEMPORAL_ADDRESS`.
2. Else `await connect_temporal_client()`. On success, return. On failure,
   let the exception propagate.

`_se_startup()` (async):

1. `await _assert_temporal_ready()` — no try/except around this call.
2. Existing telemetry registration (log-and-continue).
3. Existing SE Temporal worker start (log-and-continue).
4. Existing CodeEngineProvider install (log-and-continue).
5. Existing coding_team Temporal worker start (log-and-continue).

Raising from the startup hook aborts the FastAPI lifespan (`create_team_app`
awaits `on_startup` inside the try before `yield`), so the process fails to
become ready.

## Testing

New file `backend/agents/software_engineering_team/tests/test_se_startup_fail_fast.py`:

| Case | Setup | Expectation |
|---|---|---|
| Disabled | mock `is_temporal_enabled` → `False` | `_assert_temporal_ready` raises `RuntimeError` |
| Unreachable | enabled + `connect_temporal_client` raises | exception propagates |
| Ready | enabled + connect returns a client | completes without error |

Optional thin test: `_se_startup` awaits the assert before any worker start
(mocks for the remaining side effects).

Default pytest remains hermetic — mock `is_temporal_enabled` /
`connect_temporal_client`; no live Temporal server.

## Out of scope

- Changing `backend/shared/temporal/worker.py` `start_team_worker` no-op behavior
- Removing code_review_agent test-only Temporal-disable guards (sibling issues)
- Broader thread-mode test sweep across SE (siblings under #3969)
- Making worker-start failures themselves raise (assertion covers unreachable;
  post-probe start failures stay log-and-continue for this issue)

## Files

- `backend/agents/software_engineering_team/api/lifecycle.py` — assert helper + async `_se_startup`
- `backend/agents/software_engineering_team/tests/test_se_startup_fail_fast.py` — new
- This design doc
