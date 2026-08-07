# Strategy Lab API orchestration: inventory and extract boundaries

This note inventories Strategy Lab run/cycle helpers and documents ownership
between `investment_team/api/main.py` and
`strategy_lab/orchestrator_api.py`. Cluster 1's persist/reconcile/progress
pieces and Cluster 5's purge helpers now have real implementations in
`orchestrator_api.py`; `api.main` re-exports them for compatibility.

Companion notes: `MIXIN_BOUNDARIES.md` (pipeline mixin family),
`run_state.py` (already-extracted in-memory run registry + shared `lock`).

**This extraction does not change runtime semantics.** `api.main` remains the
FastAPI route and model owner, while it re-exports the extracted helper objects
so existing imports retain identity.

---

## Already extracted (do not re-litigate)

| Symbol / concern | Owner today |
|---|---|
| `lock`, `active_runs`, `_run_transition_locks`, `acquire_run_transition_lock` | `strategy_lab/run_state.py` |
| `get_lab_run_job_client`, `normalize_persisted_job`, `load_run_from_job_service`, `get_run_state` | `strategy_lab/run_state.py` |
| `api.main` aliases `_lock`, `_active_runs`, `_get_lab_run_job_client`, … | Re-exports from `run_state` |

`lock` is process-wide and also guards non–Strategy-Lab module state in
`api.main` (profiles, advisor sessions, live paper polling, etc.). Splitting
per-concern locks is out of scope for the API-orchestration extract.

---

## Module ownership (target)

| Module | Owns | Does not own |
|---|---|---|
| `strategy_lab/run_state.py` (today) | Run registry, transition locks, lab-run job client helpers | Cycle/finalize/dispatch bodies; persist dicts |
| `run_state.py` **or sibling store module** (planned) | Above **plus** Strategy Lab `_PersistentDict` stores used by lab helpers (`strategy_lab_records`, and the lab-facing use of `strategies` / `backtests` / `paper_trading_sessions`) | HTTP routes; Temporal workflow/activity definitions |
| `strategy_lab/orchestrator_api.py` (target) | Run/cycle orchestration helpers currently in `api.main` (clusters below) | FastAPI route handlers; request/response models; advisory / non-lab paper-trading dispatch |
| `api/main.py` (target end state) | Routes, Pydantic models, wiring | Strategy Lab orchestration bodies |
| `strategy_lab/temporal/activities.py` | Thin Temporal wrappers | Implementations (import from `orchestrator_api`) |

**Shared-state access (decided):** extend `run_state` (or a sibling store
module under `strategy_lab/`) so both `api.main` and `orchestrator_api` import
stores + `lock` from there. Do **not** have extracted helpers import stores
back from `api.main` (circular-import trap once `main` imports
`orchestrator_api`) — **except** the temporary lazy
`api.main._strategy_lab_records` read inside `_snapshot_prior_records`, which
must be removed when store extraction lands. Do **not** require a DI context
object for body-move steps.

`_PersistentDict` may move with the stores or remain a shared util that the
store module imports — decide when the store extraction lands.

---

## Helper inventory

### Cluster 1 — Run-state I/O (partially extracted)

| Helper | Role | Primary callers |
|---|---|---|
| `_persist_run_state` | Create/update lab-run job | `orchestrator_api` implementation; `api.main` re-export |
| `_reconcile_run_progress` | Sync in-memory progress from job service | `orchestrator_api` implementation; `api.main` re-export |
| `_run_state_to_response` | Map state dict → status response model | status / list routes |
| `_build_run_state` | Mint run-state dict for run/resume/restart | run / resume / restart routes |
| `_job_progress_percent` | Clamp progress % | `orchestrator_api` implementation; `api.main` re-export |
| `_STRATEGY_LAB_PROGRESS_FIELDS` | Field allowlist for reconcile | `orchestrator_api` implementation; `api.main` re-export |
| `STRATEGY_LAB_TERMINAL_STATUSES` | Terminal status frozenset | `orchestrator_api` implementation; `api.main` re-export |

### Cluster 2 — Dispatch / failure / concurrency guards

| Helper | Role | Primary callers |
|---|---|---|
| `_dispatch_strategy_lab_run` | Start batch workflow (Temporal-only) | run / resume / restart |
| `_fail_strategy_lab_run` | Best-effort mark failed + delayed cleanup | `_dispatch_strategy_lab_run` |
| `_no_active_run_locked` | 409 if any run is `running` (caller holds `lock`) | resume (same critical section as write) |
| `_ensure_no_active_run` | Locked wrapper around `_no_active_run_locked` | run / resume / restart |
| `_require_run_transition_lock` | Non-blocking per-`run_id` transition lock or 409 | run / resume / restart |

### Cluster 3 — Cycle pipeline

| Helper | Role | Primary callers |
|---|---|---|
| `_run_one_strategy_lab_cycle` | Orchestrator cycle + finalize | thread-era / tests; Temporal uses `run_cycle` + finalize activity |
| `_finalize_strategy_lab_cycle_record` | Signal brief attach, gated paper step, persist | `_run_one_strategy_lab_cycle`; `finalize_cycle_record_activity` |
| `_persist_strategy_lab_record` | Write record + strategy + backtest under `lock` | `_finalize_strategy_lab_cycle_record` |
| `_normalize_strategy_lab_asset_class` | Asset-class coerce | `_build_strategy_from_ideation` |
| `_coerce_strategy_lab_timeframe` | Timeframe coerce | `_build_strategy_from_ideation` |
| `_normalize_strategy_lab_rule_list` | Rule-list coerce | `_build_strategy_from_ideation` |
| `_build_strategy_from_ideation` | `StrategySpec` from ideation dict | tests / ideation paths |
| `_strategy_lab_signal_expert_enabled` | Env gate for signal expert | `_compute_signal_brief_snapshot` |
| `_compute_signal_brief_snapshot` | Per-batch signal brief (fail-open) | `compute_signal_brief_activity` |

`_finalize_strategy_lab_cycle_record` currently calls `_run_paper_trading_step`
(defined in `api.main` with paper-trading routes). When cluster 3 moves,
either leave a narrow import of that step from `api.main` temporarily or
extract the step into a paper-trading helper module in the same extract
wave — do not silently duplicate it.

### Cluster 4 — Cancel / terminal status

| Helper | Role | Primary callers |
|---|---|---|
| `_strategy_lab_external_terminal_status` | Persisted external stop status or `None` | activities; cancel helpers |
| `_is_strategy_lab_run_externally_stopped` | Any external terminal | `is_run_cancelled_activity` |
| `_is_strategy_lab_run_cancelled` | Exact `"cancelled"` only | routes needing precise cancel |
| `_strategy_lab_run_failure` | Optional failure string for a run | status surfaces |
| `_STRATEGY_LAB_EXTERNAL_TERMINAL_STATUSES` | `cancelled`/`failed`/`interrupted` | external-terminal helpers |

### Cluster 5 — Storage purge / prior-record snapshot (extracted)

| Helper | Role | Primary callers |
|---|---|---|
| `_snapshot_prior_records` | Locked parse+sort of lab records | `orchestrator_api` implementation; `api.main` re-export |
| `_delete_jobs_concurrently` | Bounded parallel `delete_job` | `orchestrator_api` implementation; `api.main` re-export |
| `_delete_paper_sessions_for_lab_record` | Delete paper jobs for one lab id | `orchestrator_api` implementation; `api.main` re-export |
| `_purge_strategy_lab_job_storage` | Full lab + paper storage purge | `orchestrator_api` implementation; `api.main` re-export |
| `_PURGE_MAX_WORKERS`, `_PURGE_TIMEOUT_S` | Purge fan-out knobs | `orchestrator_api` constants; `api.main` re-export |

### Explicitly not claimed by `orchestrator_api`

- FastAPI route functions and Strategy Lab request/response models
  (`RunStrategyLabRequest`, status/results models, …).
- `_PersistentDict` and non-lab stores (`_profiles`, `_proposals`,
  `_validations`, `_advisor_sessions`).
- Shared Temporal enable helpers used outside the lab batch path
  (`_dispatch_via_temporal`, `_require_temporal`) until a separate shared
  dispatch util is justified — lab dispatch can keep calling them from
  `api.main` or import them as peers later.
- Live paper-trading background / advisory agent singletons.

---

## Call graph (Temporal hot path)

```
activities.persist_run_state_activity
  → orchestrator_api._persist_run_state
       → run_state.get_lab_run_job_client

activities.snapshot_prior_records_activity
  → orchestrator_api._snapshot_prior_records
       → run_state.lock + lazy api.main._strategy_lab_records

activities.compute_signal_brief_activity
  → orchestrator_api._compute_signal_brief_snapshot  →  api.main...
       → _snapshot_prior_records, SignalIntelligenceExpert, market provider

activities.is_run_cancelled_activity
  → orchestrator_api._is_strategy_lab_run_externally_stopped  →  api.main...
       → _strategy_lab_external_terminal_status → job client

activities.external_terminal_status_activity
  → orchestrator_api._strategy_lab_external_terminal_status  →  api.main...

activities.finalize_cycle_record_activity
  → orchestrator_api._finalize_strategy_lab_cycle_record  →  api.main...
       → _run_paper_trading_step, _persist_strategy_lab_record
       → lock + strategy_lab_records / strategies / backtests / paper_trading_sessions
```

### HTTP route edges (documented; not re-exported in the façade step)

`run` / `resume` / `restart`
  → `_ensure_no_active_run` / `_no_active_run_locked`, `_require_run_transition_lock`,
    `_build_run_state`, `_persist_run_state`, `_dispatch_strategy_lab_run`
  → on failure: `_fail_strategy_lab_run`

`list` / `status` / `stream` / `jobs`
  → `_reconcile_run_progress`, `_run_state_to_response`, `_job_progress_percent`

`delete record` / `clear storage`
  → `_delete_paper_sessions_for_lab_record`, `_purge_strategy_lab_job_storage`

---

## Current export boundary

`orchestrator_api.py` implements these Cluster 1/5 helpers:

1. `_persist_run_state`
2. `_reconcile_run_progress`
3. `_job_progress_percent`
4. `_STRATEGY_LAB_PROGRESS_FIELDS`
5. `STRATEGY_LAB_TERMINAL_STATUSES`
6. `_snapshot_prior_records`
7. `_delete_jobs_concurrently`
8. `_delete_paper_sessions_for_lab_record`
9. `_purge_strategy_lab_job_storage`
10. `_PURGE_MAX_WORKERS` / `_PURGE_TIMEOUT_S`

`api.main` imports and re-exports those exact objects. Only the four remaining
Temporal-hot helpers use lazy resolution (`__getattr__` → `api.main`):
`_compute_signal_brief_snapshot`, `_is_strategy_lab_run_externally_stopped`,
`_strategy_lab_external_terminal_status`, and
`_finalize_strategy_lab_cycle_record`.

`strategy_lab/temporal/activities.py` imports its helpers from
`orchestrator_api` instead of `api.main`.

---

## Deferred extraction order (remaining)

Cluster 1 persist/reconcile/progress and Cluster 5 purge/snapshot bodies are
already in `orchestrator_api` (this extract). Remaining order:

1. **Store extraction** — move lab `_PersistentDict` instances (and optionally
   `_PersistentDict` itself) behind `run_state` or `strategy_lab/stores.py`;
   leave aliases on `api.main` if other teams still need them. Until then,
   `_snapshot_prior_records` keeps a **lazy** import of
   `api.main._strategy_lab_records` (import outside the lock) as a temporary
   exception to the "do not import stores from `api.main`" rule.
2. **Move remaining Temporal-hot / cycle helpers** (Cluster 3 finalize + signal
   brief; Cluster 4 external-terminal helpers) into `orchestrator_api` for real;
   drop lazy `__getattr__` re-export indirection for those names.
3. **Move dispatch/guards** (Cluster 2) and thin Strategy Lab routes to
   delegation-only call sites.
4. **Optional:** extract `_run_paper_trading_step` dependency cleanly if it
   still couples finalize to the paper-trading route module.

Each step should stay behavior-preserving (verbatim move + import rewires),
with the investment_team test suite as the gate.

**Test-patch note:** helpers and constants owned by `orchestrator_api` /
`run_state` must be monkeypatched on those modules (not on `api.main`
re-export aliases). Implementations close over their defining module's
globals, so alias-only patches silently miss.

---

## Out of scope (current extract and remaining steps)

- Changing Strategy Lab runtime semantics
- Splitting the shared `lock`
- FastAPI route/model ownership moves (routes stay in `api.main` until the
  dispatch/thinning step)
- Non-Strategy-Lab `api.main` restructuring
