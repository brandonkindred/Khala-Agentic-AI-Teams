---
name: Extract shared SSE streaming module + fix investment's missing reaper
overview: Single-source the per-job SSE endpoint helper into a shared module and close investment's latent unbounded-subscription memory leak by giving its event-bus binding the same reaper/TTL/cap that blogging already runs. The bus *algorithm* is already shared (shared_job_event_bus); the remaining duplication is the reaper-equipped binding boilerplate and the ~65-line cloned SSE endpoint generator.
todos:
  - id: reaper-1
    content: Extract a reusable reaper-equipped bus binding into shared_job_event_bus (factory or ReaperHandle) so a team gets subscribe/publish/cleanup/shutdown plus the lazy background reaper with one call
    status: pending
  - id: reaper-2
    content: Rebind blogging's job_event_bus onto the shared reaper helper (no behavioural change; keeps BLOGGING_EVENT_BUS_* knobs)
    status: pending
  - id: reaper-3
    content: Rebind investment's job_event_bus onto the shared reaper helper with INVESTMENT_EVENT_BUS_* knobs — closes the unbounded-subscription leak
    status: pending
  - id: reaper-4
    content: Wire investment's app on_shutdown to stop its reaper (create_team_app currently passes no on_shutdown)
    status: pending
  - id: sse-1
    content: Add shared_sse module exposing _sse_line framing + sse_job_stream core with sync and async wrappers (snapshot, terminal-status set, to_event, keepalive, 4h deadline, touch-for-liveness)
    status: pending
  - id: sse-2
    content: Migrate blogging's stream_job_status (sync) onto sse_job_stream
    status: pending
  - id: sse-3
    content: Migrate investment's stream_strategy_lab_run (async) onto sse_job_stream; this adds the missing sub.touch() liveness call so the now-present reaper won't evict an active consumer
    status: pending
  - id: sse-4
    content: Point deepthought's SSE endpoint at the shared _sse_line framing helper (cosmetic single-sourcing; deepthought does not use the job bus)
    status: pending
  - id: test-1
    content: Tests — reaper now runs for investment; terminal-event delivery + keepalive unchanged for blogging and investment; shared-module unit tests for both sync and async paths; 90% line coverage
    status: pending
isProject: false
---

# Spec

## Problem

The per-job event bus and the SSE endpoint generator were duplicated across the
blogging and investment teams. The bus **algorithm** has since been single-sourced
into `backend/agents/shared_job_event_bus/bus.py` (both teams now hold their own
`BusState` and bind thin wrappers), so that part of the original duplication is
already resolved. Two gaps remain:

1. **Latent memory leak in investment.** Blogging's binding
   (`backend/agents/blogging/shared/job_event_bus.py`) starts a background
   **reaper** that evicts idle subscriptions past a TTL and enforces a hard cap on
   tracked jobs (`_start_reaper_if_needed`, `_reap_once`, `shutdown`, ~60 lines).
   Investment's binding (`backend/agents/investment_team/api/job_event_bus.py`,
   ~50 lines) deliberately starts **no reaper**. A subscription that skips
   `cleanup_job` (process crash, abandoned SSE client whose `finally` never runs)
   therefore accumulates **unbounded**. ⚠️ This is the latent leak the work item
   calls out.

2. **Cloned SSE endpoint generator.** The SSE streaming generator is copy-pasted
   per endpoint (~65 lines each):
   - `backend/agents/blogging/api/main.py` `stream_job_status` (≈950–1013) — a
     **sync** generator using `sub.notify.wait(timeout=1.0)`.
   - `backend/agents/investment_team/api/main.py` `stream_strategy_lab_run`
     (≈2509–2573) — an **async** generator using `await asyncio.sleep(1.0)`.

   Both share the `f"data: {json.dumps(data, default=str)}\n\n"` framing, the
   terminal-snapshot short-circuit, the drain loop with `sent_terminal`, the
   `": keepalive\n\n"` comment line, and the 4-hour deadline. `deepthought`'s SSE
   endpoint (`backend/agents/deepthought/api/main.py`) shares only the `data:`/
   `: keepalive` framing — it does not use the job bus.

## Drift that must be reconciled during the migration

These differences between the two clones are intentional in spots and accidental
in others; the shared helper must preserve the intentional ones and fix the
accidental one:

| Aspect | Blogging (sync) | Investment (async) | Resolution |
|---|---|---|---|
| Concurrency model | sync generator (threadpool), `notify.wait` | async generator, `asyncio.sleep` | **Keep both** — shared core, two thin wrappers |
| `sub.touch()` liveness | present each loop pass | **missing** | Shared core always touches → fixes investment |
| Terminal event types | `complete`, `error`, `cancelled` | `complete`, `error` | Caller-supplied `terminal_types` set |
| Initial snapshot | always emitted | only when `current` state exists | Caller-supplied `snapshot` callback may return `None` to skip |

The missing `sub.touch()` in investment is load-bearing for this work item:
once investment **has** a reaper, an actively-connected consumer that goes quiet
longer than the TTL would be wrongly evicted and lose its terminal event. The
shared core calling `touch()` every pass closes that hole as a side effect.

## Goals

1. Close the investment unbounded-subscription leak by giving its bus binding the
   reaper/TTL/cap, without re-duplicating blogging's reaper boilerplate.
2. Single-source the SSE endpoint generator so the bus + streaming contract has
   one implementation, while preserving each team's sync/async concurrency model.
3. No behavioural change to blogging or investment terminal-event delivery or
   keepalive timing, verified by their existing tests.

## Non-goals

- Solving the documented **multi-worker** limitation (events don't cross uvicorn
  workers / replicas). Out of scope; the shared module keeps the same process-local
  caveat and docstring warning.
- Migrating deepthought's bus model — it has none. deepthought only adopts the
  `_sse_line` framing helper.
- Changing TTL/cap default values for blogging.

# Implementation Plan

## Part A — Reaper-equipped bus binding (closes the leak)

The reaper machinery blogging carries (`_start_reaper_if_needed` / `_reap_once` /
`shutdown`, lazy-start under lock via `BackgroundHeartbeat`, indirection through
module globals so tests can monkeypatch TTL/cap) is exactly what investment needs.
Rather than copy it into investment, **extract it once** into
`shared_job_event_bus`.

1. Add `shared_job_event_bus/reaper.py` exposing a small factory, e.g.
   `make_reaper(state, *, ttl_seconds, max_jobs, interval_seconds, name, label,
   logger) -> ReaperHandle`, where `ReaperHandle` provides `ensure_started()`
   (idempotent lazy start under `state.lock`), `reap_once()` (re-reads tunables so
   monkeypatching still works), and `shutdown()` (swap-under-lock then stop outside
   the lock to avoid the documented deadlock). This is blogging's current logic
   lifted verbatim, parameterised by the env-var prefix and thread name.
2. Re-export from `shared_job_event_bus/__init__.py`.
3. **Blogging** (`blogging/shared/job_event_bus.py`): replace the inline reaper
   block with a `ReaperHandle`; `subscribe()` calls `handle.ensure_started()`,
   `shutdown()` delegates to `handle.shutdown()`. Keep the `BLOGGING_EVENT_BUS_*`
   env-var names and the public `__all__` (`shutdown` included) unchanged.
4. **Investment** (`investment_team/api/job_event_bus.py`): add the same
   `ReaperHandle` with `INVESTMENT_EVENT_BUS_TTL_SECONDS` /
   `INVESTMENT_EVENT_BUS_MAX_JOBS` / `INVESTMENT_EVENT_BUS_REAPER_INTERVAL` knobs
   (documented in `docs/ENV_VARS.md`), have `subscribe()` call
   `ensure_started()`, and add `shutdown` to `__all__`.
5. **Wire investment teardown.** `investment_team/api/main.py` calls
   `create_team_app(...)` with **no** `on_shutdown`. Add an
   `on_shutdown=_run_investment_service_shutdown` that calls the bus binding's
   `shutdown()` (mirroring blogging's `on_shutdown` pattern at
   `blogging/api/main.py:196`), so the reaper thread is stopped cleanly on
   process exit.

## Part B — Shared SSE streaming helper

6. Add `backend/agents/shared_sse/` (new sibling package; depends on
   `shared_job_event_bus` for `Subscription`). Per the work item this is sharper
   than the generic team-API scaffold: `team_contract` covers CORS/health/ready/
   meta but **not** SSE. Expose:
   - `sse_line(data: dict) -> str` — the `f"data: {json.dumps(data, default=str)}\n\n"`
     framing (also consumed by deepthought).
   - A shared inner stepper that, given a live `Subscription`, performs one
     drain-pass: pop queued events, frame them, detect terminal types, emit the
     `{"type": "done"}` line, and otherwise emit the keepalive comment. This is the
     single copy of the drain/terminal/keepalive logic.
   - `sse_job_stream_sync(subscribe, unsubscribe, job_id, snapshot, terminal_types,
     deadline_seconds=4*3600)` — sync generator using `sub.notify.wait(timeout=1.0)`.
   - `sse_job_stream_async(...)` — async generator using `await asyncio.sleep(1.0)`.

   Both wrappers: emit the optional initial `snapshot()` (skip when it returns
   `None`), call `sub.touch()` every pass, honour the 4-hour deadline, and
   `unsubscribe` in `finally`. The terminal-snapshot short-circuit (when the job is
   already terminal at request time) stays in each endpoint since it needs the
   team's state lookup; it reuses `sse_line`.
7. **Migrate blogging** `stream_job_status` to `sse_job_stream_sync` with
   `terminal_types={"complete","error","cancelled"}` and a `snapshot` closure over
   `_snapshot_event()`.
8. **Migrate investment** `stream_strategy_lab_run` to `sse_job_stream_async` with
   `terminal_types={"complete","error"}` and a `snapshot` closure that returns
   `None` when no `current` state exists. This brings in the `sub.touch()` call the
   async clone was missing.
9. **deepthought**: replace its inline `f"data: ...\n\n"` lines with
   `shared_sse.sse_line` where applicable (its `event:`-prefixed named-event lines
   and `model_dump_json()` payloads stay as-is; only the plain framing is shared).

## Module-placement note

`sse_job_stream` is tightly coupled to the bus (`Subscription`, `touch`, `events`,
`notify`). Folding it into `shared_job_event_bus/sse.py` instead of a new
`shared_sse/` package is a viable alternative and keeps one streaming package. The
work item names `shared_sse`, and the framing-only `sse_line` (used by deepthought,
which has no bus) reads as a standalone concern — so default to the new `shared_sse`
package importing from `shared_job_event_bus`. Confirm placement before coding.

# Verification

- **Reaper now runs for investment.** Unit test: subscribe to a run, never
  `cleanup_job`, advance time past the TTL (monkeypatch the tunables, drive
  `reap_once()`), assert the subscription is evicted and its subscriber woken —
  the test that previously could only pass for blogging.
- **No behavioural regression.** Existing blogging + investment SSE/event-bus
  tests pass unchanged: initial snapshot, incremental updates, terminal event
  (`complete`/`error`[/`cancelled`]) followed by `{"type":"done"}`, and keepalive
  emission on silence. (Requires team deps + live Postgres per the team test
  setup; `backend/conftest.py` provides the in-process job service under pytest.)
- **Shared-module unit tests** for `shared_sse`: both the sync and async wrappers
  drive a fake `Subscription` preloaded with events through to a terminal `done`,
  and a silence case asserting the keepalive line — kept independent of any team's
  Postgres. Hit the 90% line-coverage floor for the new module.
- `make lint` (ruff, line-length 120) clean.

# Impact

- **Memory:** closes investment's unbounded-subscription leak; the now-shared
  `touch()` prevents the reaper from evicting active consumers mid-stream.
- **Complexity:** removes the duplicated reaper block (≈60 lines) and the cloned
  SSE generator (≈65 lines × 2), single-sourcing both the bus reaper and the
  streaming contract behind one module each.
