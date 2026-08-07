# Design: Extract Strategy Lab dispatch / failure / concurrency guards

Date: 2026-08-07

## Goal

Move Strategy Lab **dispatch / failure / concurrency-guard** helpers out of
`investment_team/api/main.py` into `strategy_lab/orchestrator_api.py`, leaving
run / resume / restart routes as thin callers via hybrid aliases. Behavior is
unchanged.

This is the third extraction slice after the inventory/façade step and the
persist/reconcile/purge body move. **Finalize and other Temporal-hot deferred
helpers are intentionally not moved in this slice** (narrow scope choice);
they remain lazy re-exports from `orchestrator_api` → `api.main` until a
follow-up extract.

## Context

`ORCHESTRATOR_API_BOUNDARIES.md` clusters remaining Strategy Lab orchestration
still in `api.main`. Cluster 1 (run-state I/O) and purge helpers already have
real bodies in `orchestrator_api` with `api.main` aliases. Cluster 2
(dispatch / fail / guards) still has full function bodies in `api.main` and is
the sole target of this design.

Sibling pattern from the persist/reconcile/purge extract:

- Verbatim body move into `orchestrator_api`
- Shared run registry via `run_state` (`_lock`, `_active_runs`, …)
- Hybrid aliases on `api.main` so routes/tests keep local names
- Lazy import of `api.main`-owned types/utils inside functions (no import-time
  cycle)
- No `sys.modules` override shims
- Monkeypatches that rebind **helper names** on `api.main` keep working;
  patches of **closed-over globals** must target `orchestrator_api` / `run_state`

## Decisions

| Topic | Choice |
|---|---|
| Scope | Cluster 2 only (five helpers below) |
| Finalize / deferred Temporal helpers | Explicitly deferred to a follow-up |
| Route wiring | Hybrid aliases on `api.main` (Approach A) |
| `RunStrategyLabRequest` / `_require_temporal` | Lazy import inside `_dispatch_strategy_lab_run` |
| Shared run state | Continue importing from `run_state` |
| Persist on fail | Call `_persist_run_state` already defined in `orchestrator_api` |
| Override shims | Forbidden |
| Store / `_PersistentDict` extraction | Out of scope |

## Scope

### In scope — move real bodies into `orchestrator_api`

| Symbol | Role |
|---|---|
| `_dispatch_strategy_lab_run` | Start batch workflow (Temporal-only) |
| `_fail_strategy_lab_run` | Best-effort mark failed + delayed cleanup |
| `_no_active_run_locked` | 409 if any run is `running` (caller holds lock) |
| `_ensure_no_active_run` | Locked wrapper around `_no_active_run_locked` |
| `_require_run_transition_lock` | Non-blocking per-`run_id` transition lock or 409 |

### Wiring

- `api.main` keeps thin aliases, e.g.
  `_dispatch_strategy_lab_run = _strategy_lab_orchestrator_api._dispatch_strategy_lab_run`
  (same hybrid style as persist helpers).
- Run / resume / restart routes continue to call the local names; they do not
  grow new orchestration logic.
- Update `ORCHESTRATOR_API_BOUNDARIES.md` so cluster 2 is marked owned by
  `orchestrator_api`.
- Extend `test_orchestrator_api.py` identity assertions for the five moved
  symbols; keep `_DEFERRED_EXPORTS` unchanged for finalize/snapshot/signal/
  external-terminal helpers.
- Retarget unit tests that patch closed-over globals used by the moved bodies
  (see Testing).

### Out of scope

- `_finalize_strategy_lab_cycle_record`, `_persist_strategy_lab_record`,
  `_snapshot_prior_records`, `_compute_signal_brief_snapshot`, external-terminal
  / cancel helpers, ideation builders, `_run_one_strategy_lab_cycle`
- Extracting `_require_temporal` / `_dispatch_via_temporal` into a shared util
- Moving `_PersistentDict` lab stores
- Extracting `_run_paper_trading_step`
- Non–Strategy-Lab restructuring of `api.main`
- Temporal workflow redesign
- Grep/body-ownership regression guards (reserved for the verification slice)

## Architecture

```
api.main routes (run / resume / restart)
  → api.main aliases
       → orchestrator_api._ensure_no_active_run / _no_active_run_locked
       → orchestrator_api._require_run_transition_lock
       → orchestrator_api._build_run_state / _persist_run_state  (already moved)
       → orchestrator_api._dispatch_strategy_lab_run
            lazy: api.main._require_temporal, RunStrategyLabRequest
            → start_strategy_lab_batch_workflow(...)
            on failure → orchestrator_api._fail_strategy_lab_run
                 → run_state lock + active_runs
                 → orchestrator_api._persist_run_state
                 → threading.Timer delayed cleanup

orchestrator_api._DEFERRED_EXPORTS  (unchanged)
  → finalize / snapshot / signal / external-terminal still → api.main
```

### Circular-import rules

1. No top-level `from investment_team.api import main` inside `orchestrator_api`.
2. Lazy import `api.main` only inside `_dispatch_strategy_lab_run` for
   `_require_temporal` and typing/use of `RunStrategyLabRequest` as needed.
3. `_fail_strategy_lab_run` must call the in-module `_persist_run_state`, not
   route through `api.main`.
4. Transition locks continue via `run_state.acquire_run_transition_lock` (or
   existing `api.main` alias of that symbol — prefer importing from `run_state`
   in the moved body).

### Monkeypatch semantics (document in boundaries note)

| What tests patch | After move |
|---|---|
| Helper **name** on `api.main` (e.g. stub `_dispatch_strategy_lab_run`) | Still works (alias rebinding) |
| Closed-over `_active_runs` / `_persist_run_state` / `threading.Timer` used by fail/guards | Patch `orchestrator_api` (and/or `run_state`), not only `api.main` |

## Error handling

Unchanged from today:

- `_dispatch_strategy_lab_run`: `WorkflowAlreadyStartedError` handling per
  `allow_already_started`; other failures call `_fail_strategy_lab_run` then
  re-raise / wrap as `HTTPException(503)`.
- `_fail_strategy_lab_run`: best-effort; persist and timer errors must not
  escape beyond existing try/except contracts.
- Guards: `HTTPException(409)` only; no mutation of `_active_runs`.

## Testing

1. **Identity / ownership** — `test_orchestrator_api.py`: each moved symbol is
   defined on `orchestrator_api` and identical to the `api.main` alias; deferred
   set still resolves via `__getattr__`.
2. **Retarget closed-over patches**
   - `_no_active_run_locked*` tests: patch `orchestrator_api._active_runs`
   - `_fail_strategy_lab_run*` tests: patch `orchestrator_api._active_runs`,
     `orchestrator_api._persist_run_state`, and `orchestrator_api.threading.Timer`
   - Assert call-through where early-return would false-pass (e.g. persist
     called / timer started) when the test’s point is that path.
3. **Route stubs** that rebind `_dispatch_strategy_lab_run` /
   `_ensure_no_active_run` on `api.main` may stay as-is.
4. Gate: investment_team Strategy Lab route + fail/guard related tests green;
   `ruff check` clean on touched paths.

## Success criteria

- [ ] Five cluster-2 helpers have real bodies only in `orchestrator_api`
- [ ] `api.main` exposes them only as aliases (no duplicate bodies)
- [ ] Run / resume / restart behavior unchanged; routes remain thin callers
- [ ] Boundaries doc updated; deferred finalize exports unchanged
- [ ] Existing Strategy Lab API tests pass after monkeypatch retargets

## Risks

| Risk | Mitigation |
|---|---|
| Dead tests after move (patch wrong module) | Retarget + assert side effects (timer / persist) |
| Lazy `_require_temporal` misses `api.main` monkeypatch | Document; tests that stub Temporal should patch the name dispatch actually looks up, or stub `_dispatch_strategy_lab_run` itself |
| Accidental finalize creep | Spec/out-of-scope list; leave `_DEFERRED_EXPORTS` alone |
