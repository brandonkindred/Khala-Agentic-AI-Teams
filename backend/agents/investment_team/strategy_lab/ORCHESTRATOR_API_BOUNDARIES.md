# Strategy Lab API orchestration: inventory and extract boundaries

This note inventories the Strategy Lab run/cycle helpers, documents the target
ownership for `strategy_lab/orchestrator_api.py`, and records how shared state
(`lock`, record maps) is accessed after the partial body move. Read this before
relocating remaining helpers — it is the map for "where does this belong" so
later extraction steps do not invent ownership mid-refactor.

Companion notes: `MIXIN_BOUNDARIES.md` (pipeline mixin family),
`run_state.py` (already-extracted in-memory run registry + shared `lock`).

**Partial body move complete (persist / reconcile / purge).** Run-state I/O and
purge helper bodies now live in `orchestrator_api.py`; `api.main` keeps thin
aliases for routes and tests. Five Temporal-hot helpers remain deferred via
lazy `__getattr__` (see below). Runtime semantics are unchanged.

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
`orchestrator_api`). Do **not** require a DI context object for the first
body-move steps.

`_PersistentDict` may move with the stores or remain a shared util that the
store module imports — decide when the store extraction lands; not required
for the façade step.

---

## Helper inventory

### Cluster 1 — Run-state I/O

**Owner:** bodies in `orchestrator_api`; `api.main` aliases for routes/tests.

| Helper | Role | Primary callers |
|---|---|---|
| `_persist_run_state` | Create/update lab-run job | routes; Temporal `persist_run_state_activity`; `_fail_strategy_lab_run` |
| `_reconcile_run_progress` | Sync in-memory progress from job service | `list` / `status` / `stream` / `jobs` routes |
| `_run_state_to_response` | Map state dict → status response model | status / list routes |
| `_build_run_state` | Mint run-state dict for run/resume/restart | run / resume / restart routes |
| `_job_progress_percent` | Clamp progress % | `list_strategy_lab_jobs` |
| `_STRATEGY_LAB_PROGRESS_FIELDS` | Field allowlist for reconcile | `_reconcile_run_progress` |
| `STRATEGY_LAB_TERMINAL_STATUSES` | Terminal status frozenset | reconcile, fail, routes |

### Cluster 2 — Dispatch / failure / concurrency guards (still in `api.main`)

| Helper | Role | Primary callers |
|---|---|---|
| `_dispatch_strategy_lab_run` | Start batch workflow (Temporal-only) | run / resume / restart |
| `_fail_strategy_lab_run` | Best-effort mark failed + delayed cleanup | `_dispatch_strategy_lab_run` |
| `_no_active_run_locked` | 409 if any run is `running` (caller holds `lock`) | resume (same critical section as write) |
| `_ensure_no_active_run` | Locked wrapper around `_no_active_run_locked` | run / resume / restart |
| `_require_run_transition_lock` | Non-blocking per-`run_id` transition lock or 409 | run / resume / restart |

### Cluster 3 — Cycle pipeline (still in `api.main`; finalize deferred via façade)

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

### Cluster 4 — Cancel / terminal status (still in `api.main`; external-terminal helpers deferred via façade)

| Helper | Role | Primary callers |
|---|---|---|
| `_strategy_lab_external_terminal_status` | Persisted external stop status or `None` | activities; cancel helpers |
| `_is_strategy_lab_run_externally_stopped` | Any external terminal | `is_run_cancelled_activity` |
| `_is_strategy_lab_run_cancelled` | Exact `"cancelled"` only | routes needing precise cancel |
| `_strategy_lab_run_failure` | Optional failure string for a run | status surfaces |
| `_STRATEGY_LAB_EXTERNAL_TERMINAL_STATUSES` | `cancelled`/`failed`/`interrupted` | external-terminal helpers |

### Cluster 5 — Storage purge / prior-record snapshot

**Owner:** purge helpers in `orchestrator_api` (aliases on `api.main`);
`_snapshot_prior_records` still in `api.main` (deferred via façade).

| Helper | Role | Primary callers | Owner |
|---|---|---|---|
| `_snapshot_prior_records` | Locked parse+sort of lab records | cycles; `snapshot_prior_records_activity`; signal brief | `api.main` (deferred) |
| `_delete_jobs_concurrently` | Bounded parallel `delete_job` | purge helpers | `orchestrator_api` |
| `_delete_paper_sessions_for_lab_record` | Delete paper jobs for one lab id | `delete_strategy_lab_record` | `orchestrator_api` |
| `_purge_strategy_lab_job_storage` | Full lab + paper storage purge | `clear_strategy_lab_storage` | `orchestrator_api` |
| `_PURGE_MAX_WORKERS`, `_PURGE_TIMEOUT_S` | Purge fan-out knobs | purge helpers | `orchestrator_api` |

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
  → orchestrator_api._persist_run_state  (real body)
       → run_state.get_lab_run_job_client

activities.snapshot_prior_records_activity
  → orchestrator_api._snapshot_prior_records  →  api.main._snapshot_prior_records
       → run_state.lock + _strategy_lab_records (still constructed in api.main)

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

### HTTP route edges (documented; moved helpers resolve via `api.main` aliases)

`run` / `resume` / `restart`
  → `_ensure_no_active_run` / `_no_active_run_locked`, `_require_run_transition_lock`,
    `_build_run_state`, `_persist_run_state`, `_dispatch_strategy_lab_run`
  → on failure: `_fail_strategy_lab_run`

`list` / `status` / `stream` / `jobs`
  → `_reconcile_run_progress`, `_run_state_to_response`, `_job_progress_percent`

`delete record` / `clear storage`
  → `_delete_paper_sessions_for_lab_record`, `_purge_strategy_lab_job_storage`

---

## Partial body move (persist / reconcile / purge)

**Bodies now in `orchestrator_api`:**

- `STRATEGY_LAB_TERMINAL_STATUSES`
- `_STRATEGY_LAB_PROGRESS_FIELDS`
- `_persist_run_state`
- `_reconcile_run_progress`
- `_run_state_to_response`
- `_build_run_state`
- `_job_progress_percent`
- `_PURGE_MAX_WORKERS`, `_PURGE_TIMEOUT_S`
- `_delete_jobs_concurrently`
- `_delete_paper_sessions_for_lab_record`
- `_purge_strategy_lab_job_storage`

**`api.main` aliases:** routes and tests import the same symbols from
`api.main`; those names re-export from `orchestrator_api` so monkeypatches and
existing call sites keep working.

**Deferred via lazy `__getattr__` → `api.main` (five Temporal-hot helpers):**

1. `_snapshot_prior_records`
2. `_compute_signal_brief_snapshot`
3. `_is_strategy_lab_run_externally_stopped`
4. `_strategy_lab_external_terminal_status`
5. `_finalize_strategy_lab_cycle_record`

Lazy resolution avoids eagerly loading `api.main` when activities import the
façade at worker startup; monkeypatches on `api.main` still apply for
deferred names.

`strategy_lab/temporal/activities.py` imports Temporal-hot helpers from
`orchestrator_api` instead of `api.main`.

---

## Deferred extraction order (remaining work)

Order later body-move PRs to keep Temporal and HTTP sharing one
implementation without circular imports:

1. **Store extraction** — move lab `_PersistentDict` instances (and optionally
   `_PersistentDict` itself) behind `run_state` or `strategy_lab/stores.py`;
   leave aliases on `api.main` if other teams still need them.
2. **Remaining Temporal-hot bodies** — `_snapshot_prior_records`, signal brief,
   cancel/external-terminal helpers, `_finalize_strategy_lab_cycle_record`;
   drop `__getattr__` indirection for those five.
3. **Dispatch / fail / transition guards** (cluster 2); routes become thin
   callers.
4. **Optional:** extract `_run_paper_trading_step` dependency cleanly if it
   still couples finalize to the paper-trading route module.

Each step should stay behavior-preserving (verbatim move + import rewires),
with the investment_team test suite as the gate.

---

## Out of scope for the persist/reconcile/purge extract

- Moving the five deferred Temporal-hot bodies (still via `__getattr__`)
- Changing Strategy Lab runtime semantics
- Splitting the shared `lock`
- Extracting dispatch / fail / transition guards (cluster 2)
