# Strategy Lab Dispatch/Guards Extract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move Strategy Lab dispatch, fail, and concurrency-guard helper bodies from `api/main.py` into `strategy_lab/orchestrator_api.py`, leave thin aliases on `api.main`, and keep runtime behavior identical.

**Architecture:** Verbatim body relocate into `orchestrator_api`. Import `run_state` symbols as `_lock` / `_active_runs` / `_acquire_run_transition_lock`. Lazy-import `_require_temporal` from `api.main` inside `_dispatch_strategy_lab_run` only. Hybrid aliases on `api.main`. Retarget closed-over monkeypatches onto `orchestrator_api`. Leave `_DEFERRED_EXPORTS` (finalize/snapshot/signal/cancel) unchanged.

**Tech Stack:** Python 3.10+, pytest, FastAPI (`api.main`), `strategy_lab.run_state`, threading.Timer

**Spec:** `docs/superpowers/specs/2026-08-07-strategy-lab-dispatch-guards-extract-design.md`

**Worktree:** `.worktrees/issue-5515-extract-dispatch-finalize` (or equivalent isolated checkout off current `main`)

## Global Constraints

- No intentional runtime behavior change (verbatim move + import rewires)
- Never put GitHub issue numbers in code, comments, commit messages, or docs (PR body only: `Closes #N`)
- Design-by-Contract docstrings preserved on moved helpers
- Ruff line-length 120; Python 3.10+
- Coverage: new/changed Python ≥ 90%
- Do **not** move finalize / snapshot / signal / external-terminal / cancel / ideation / `_run_one_strategy_lab_cycle` / stores / `_require_temporal` body
- `orchestrator_api` must not import `api.main` at module top (lazy inside `_dispatch_strategy_lab_run` for `_require_temporal` only; `RunStrategyLabRequest` via `TYPE_CHECKING`)
- No `sys.modules` override shims
- Prefer `run_state` imports so cut/paste bodies keep `_lock` / `_active_runs` names

## File map

| File | Role |
|---|---|
| `backend/agents/investment_team/strategy_lab/orchestrator_api.py` | Real bodies for five cluster-2 helpers; `__getattr__` unchanged for deferred five |
| `backend/agents/investment_team/api/main.py` | Delete cluster-2 bodies; add aliases from `orchestrator_api` |
| `backend/agents/investment_team/tests/test_orchestrator_api.py` | Add five names to `_MOVED` |
| `backend/agents/investment_team/tests/test_strategy_lab_routes.py` | Retarget `_no_active_run_locked*` patches onto `orchestrator_api._active_runs` |
| `backend/agents/investment_team/tests/test_temporal_bootstrap.py` | Retarget `_fail_strategy_lab_run*` closed-over patches onto `orchestrator_api` |
| `backend/agents/investment_team/strategy_lab/ORCHESTRATOR_API_BOUNDARIES.md` | Mark cluster 2 owned; refresh deferred order |

## Exact symbols

**Move (real definitions on `orchestrator_api`):**  
`_fail_strategy_lab_run`, `_dispatch_strategy_lab_run`, `_no_active_run_locked`, `_ensure_no_active_run`, `_require_run_transition_lock`

**Still deferred (`__getattr__` → `api.main`):**  
`_snapshot_prior_records`, `_compute_signal_brief_snapshot`, `_is_strategy_lab_run_externally_stopped`, `_strategy_lab_external_terminal_status`, `_finalize_strategy_lab_cycle_record`

**Line ranges in `api/main.py` (at plan-writing HEAD `7cdf57c5a`; re-verify with `rg` before cutting):**

| Symbol | Approx lines |
|---|---|
| `_fail_strategy_lab_run` | 2342–2411 |
| `_dispatch_strategy_lab_run` | 2414–2502 |
| `_no_active_run_locked` | 2505–2527 |
| `_ensure_no_active_run` | 2530–2545 |
| `_require_run_transition_lock` | 2548–2577 |

Stop before `_dispatch_backtest_run` (line ~2580) — that stays in `api.main`.

---

### Task 1: RED — expect cluster-2 symbols defined on `orchestrator_api`

**Files:**
- Modify: `backend/agents/investment_team/tests/test_orchestrator_api.py`

**Interfaces:**
- Consumes: current `orchestrator_api` (cluster 2 still only as `api.main` bodies; not in `__dict__`)
- Produces: failing `_MOVED` assertions for the five new names

- [ ] **Step 1: Extend `_MOVED`**

In `test_orchestrator_api.py`, append to `_MOVED`:

```python
_MOVED = (
    "STRATEGY_LAB_TERMINAL_STATUSES",
    "_persist_run_state",
    "_reconcile_run_progress",
    "_run_state_to_response",
    "_build_run_state",
    "_job_progress_percent",
    "_delete_jobs_concurrently",
    "_delete_paper_sessions_for_lab_record",
    "_purge_strategy_lab_job_storage",
    "_fail_strategy_lab_run",
    "_dispatch_strategy_lab_run",
    "_no_active_run_locked",
    "_ensure_no_active_run",
    "_require_run_transition_lock",
)
```

Leave `_DEFERRED` unchanged.

- [ ] **Step 2: Run RED**

```bash
cd backend
PYTHONPATH=agents pytest agents/investment_team/tests/test_orchestrator_api.py -q
```

Expected: FAIL — new names missing from `orchestrator_api.__dict__`.

- [ ] **Step 3: Commit**

```bash
git add backend/agents/investment_team/tests/test_orchestrator_api.py
git commit -m "$(cat <<'EOF'
Expect Strategy Lab orchestrator_api to own dispatch/guard symbols.

EOF
)"
```

---

### Task 2: Move cluster-2 bodies into `orchestrator_api` + aliases

**Files:**
- Modify: `backend/agents/investment_team/strategy_lab/orchestrator_api.py`
- Modify: `backend/agents/investment_team/api/main.py` (delete bodies ~2342–2577; add aliases near existing orchestrator aliases ~158–171)

**Interfaces:**
- Consumes: `run_state.lock` / `active_runs` / `acquire_run_transition_lock`; in-module `_persist_run_state` / `STRATEGY_LAB_TERMINAL_STATUSES`
- Produces: five real callables on `orchestrator_api`; `api.main` aliases identical by identity

- [ ] **Step 1: Add imports on `orchestrator_api`**

At top of `orchestrator_api.py`, ensure:

```python
import threading
```

(if not already present) and:

```python
from investment_team.strategy_lab.run_state import (
    acquire_run_transition_lock as _acquire_run_transition_lock,
)
```

Keep existing `_lock` / `_active_runs` imports from `run_state`.

In `TYPE_CHECKING` block, add:

```python
from investment_team.api.main import RunStrategyLabRequest
```

(alongside any existing TYPE_CHECKING imports; do **not** add a runtime top-level `api.main` import).

- [ ] **Step 2: Paste the five function bodies**

Cut `_fail_strategy_lab_run` through `_require_run_transition_lock` from `api/main.py` and paste into `orchestrator_api.py` **above** `__all__` / `_DEFERRED_EXPORTS` (after purge helpers is fine).

Adjustments while pasting (only these):

1. `_dispatch_strategy_lab_run` signature — quote the request type and lazy-require Temporal:

```python
def _dispatch_strategy_lab_run(
    run_id: str, request: "RunStrategyLabRequest", *, allow_already_started: bool = True
) -> None:
    """…preserve existing docstring verbatim…"""
    try:
        from temporalio.exceptions import WorkflowAlreadyStartedError
    except ImportError:  # pragma: no cover - temporalio always installed
        WorkflowAlreadyStartedError = ()  # type: ignore[assignment]

    try:
        from investment_team.api import main as _api_main

        _api_main._require_temporal()
        from investment_team.strategy_lab.temporal.start_workflow import (
            start_strategy_lab_batch_workflow,
        )

        start_strategy_lab_batch_workflow(run_id, request)
    except Exception as exc:
        # …remainder of existing except body unchanged…
```

2. Keep `_fail_strategy_lab_run` calling in-module `_persist_run_state` and `threading.Timer` (module-level `threading` import).
3. Keep `_require_run_transition_lock` calling `_acquire_run_transition_lock` from `run_state`.
4. Do **not** change `_DEFERRED_EXPORTS`.

- [ ] **Step 3: Extend `__all__`**

Add the five names to `__all__` (before the deferred names is fine):

```python
    "_fail_strategy_lab_run",
    "_dispatch_strategy_lab_run",
    "_no_active_run_locked",
    "_ensure_no_active_run",
    "_require_run_transition_lock",
```

- [ ] **Step 4: Replace bodies in `api.main` with aliases**

Delete the five `def` blocks from `api/main.py`. Near the existing alias block (~158–171), add:

```python
_dispatch_strategy_lab_run = _strategy_lab_orchestrator_api._dispatch_strategy_lab_run
_ensure_no_active_run = _strategy_lab_orchestrator_api._ensure_no_active_run
_fail_strategy_lab_run = _strategy_lab_orchestrator_api._fail_strategy_lab_run
_no_active_run_locked = _strategy_lab_orchestrator_api._no_active_run_locked
_require_run_transition_lock = _strategy_lab_orchestrator_api._require_run_transition_lock
```

Keep `api.main`'s `_acquire_run_transition_lock` import from `run_state` if other `api.main` code still uses it; routes that only used it via `_require_run_transition_lock` do not need changes.

- [ ] **Step 5: Run identity tests (partial green)**

```bash
cd backend
PYTHONPATH=agents pytest agents/investment_team/tests/test_orchestrator_api.py -q
```

Expected: PASS for ownership/identity. Do **not** yet expect all fail/guard unit tests green (Task 3 retargets patches).

- [ ] **Step 6: Commit**

```bash
git add \
  backend/agents/investment_team/strategy_lab/orchestrator_api.py \
  backend/agents/investment_team/api/main.py
git commit -m "$(cat <<'EOF'
Move Strategy Lab dispatch and concurrency guards into orchestrator_api.

EOF
)"
```

---

### Task 3: Retarget closed-over monkeypatches

**Files:**
- Modify: `backend/agents/investment_team/tests/test_strategy_lab_routes.py` (`_no_active_run_locked*` tests ~234–290)
- Modify: `backend/agents/investment_team/tests/test_temporal_bootstrap.py` (`_fail_strategy_lab_run*` tests ~411–560+)

**Interfaces:**
- Consumes: moved bodies that close over `orchestrator_api._active_runs`, `_persist_run_state`, `threading`
- Produces: live regression tests (not early-return false passes)

- [ ] **Step 1: Fix `_no_active_run_locked*` tests**

For each test that currently does `monkeypatch.setattr(api_main, "_active_runs", …)` then calls `api_main._no_active_run_locked()`, switch to:

```python
from investment_team.strategy_lab import orchestrator_api

shared = {…}  # same fixture dict as before
monkeypatch.setattr(orchestrator_api, "_active_runs", shared)
orchestrator_api._no_active_run_locked()  # or api_main alias — both OK for the call
# assert against `shared` when checking mutation/absence
```

Apply to:

- `test_no_active_run_locked_noop_when_empty`
- `test_no_active_run_locked_raises_409_when_running_entry_present`
- `test_no_active_run_locked_tolerates_entry_missing_status_key`
- `test_no_active_run_locked_detects_running_entry_alongside_malformed_one`

Route fixtures that already patch `orchestrator_api._active_runs` + `api.main` + `run_state` to the **same** dict may leave dispatch/ensure stubs on `api.main` as-is (name rebinding).

- [ ] **Step 2: Fix `_fail_strategy_lab_run*` tests**

In `test_temporal_bootstrap.py`, for every `_fail_strategy_lab_run` unit test, retarget:

```python
from investment_team.strategy_lab import orchestrator_api

monkeypatch.setattr(orchestrator_api, "_active_runs", {…})
monkeypatch.setattr(orchestrator_api, "_persist_run_state", …)
monkeypatch.setattr(orchestrator_api.threading, "Timer", …)
# Call either:
orchestrator_api._fail_strategy_lab_run(run_id, "boom")
# Assert on orchestrator_api._active_runs / shared dict / persist observations
```

Also fix assertions that read `api_main._active_runs` or `api_main._lock.locked()` after fail — use the shared dict / `orchestrator_api._lock` (same object as `run_state.lock`).

Where the test’s point is that persist or timer ran, keep/add an assertion that the stub was called (e.g. timer `started` / persist observed) so an early-return cannot false-pass.

Leave tests that only stub `_dispatch_strategy_lab_run` / `_ensure_no_active_run` **names** on `api.main` for route flows unchanged.

- [ ] **Step 3: Run targeted suites**

```bash
cd backend
PYTHONPATH=agents pytest \
  agents/investment_team/tests/test_orchestrator_api.py \
  agents/investment_team/tests/test_strategy_lab_routes.py \
  agents/investment_team/tests/test_temporal_bootstrap.py \
  -q --tb=short
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add \
  backend/agents/investment_team/tests/test_strategy_lab_routes.py \
  backend/agents/investment_team/tests/test_temporal_bootstrap.py
git commit -m "$(cat <<'EOF'
Retarget dispatch/guard monkeypatches onto orchestrator_api.

EOF
)"
```

---

### Task 4: Update `ORCHESTRATOR_API_BOUNDARIES.md`

**Files:**
- Modify: `backend/agents/investment_team/strategy_lab/ORCHESTRATOR_API_BOUNDARIES.md`

**Interfaces:**
- Consumes: post-move ownership
- Produces: accurate inventory for follow-up finalize extract

- [ ] **Step 1: Edit cluster 2 section**

Change heading from “(still in `api.main`)” to owned by `orchestrator_api` with `api.main` aliases (mirror cluster 1 wording).

- [ ] **Step 2: Extend “Partial body move” list**

Add the five cluster-2 symbols under bodies now in `orchestrator_api`. Keep the five deferred Temporal-hot names under `__getattr__`.

- [ ] **Step 3: Refresh deferred extraction order**

Replace the remaining-work numbered list so it reflects reality after this PR:

1. Store extraction (lab `_PersistentDict`s)
2. Remaining Temporal-hot bodies (finalize / snapshot / signal / external-terminal) — drop `__getattr__` for those five
3. Optional: extract `_run_paper_trading_step` cleanly

Remove “Dispatch / fail / transition guards” from the remaining list (done). Note in a short sentence that cluster 2 landed **before** finalize by deliberate narrow scope (not the original suggested order).

- [ ] **Step 4: Document monkeypatch caveat**

Add 2–4 lines: aliasing helper **names** on `api.main` keeps route stubs working; closed-over globals (`_active_runs`, `_persist_run_state`, `threading.Timer`) must be patched on `orchestrator_api` / `run_state`.

- [ ] **Step 5: Commit**

```bash
git add backend/agents/investment_team/strategy_lab/ORCHESTRATOR_API_BOUNDARIES.md
git commit -m "$(cat <<'EOF'
Document Strategy Lab dispatch/guards move into orchestrator_api.

EOF
)"
```

---

### Task 5: Lint, smoke, PR

**Files:** none new (verification)

- [ ] **Step 1: Ruff**

```bash
cd backend
ruff check agents/investment_team/strategy_lab/orchestrator_api.py \
  agents/investment_team/api/main.py \
  agents/investment_team/tests/test_orchestrator_api.py \
  agents/investment_team/tests/test_strategy_lab_routes.py \
  agents/investment_team/tests/test_temporal_bootstrap.py
ruff format --check …same paths…
```

Fix any I001 / format issues; commit if needed:

```bash
git commit -m "$(cat <<'EOF'
Format Strategy Lab dispatch/guards extraction.

EOF
)"
```

- [ ] **Step 2: Broader smoke**

```bash
cd backend
PYTHONPATH=agents pytest agents/investment_team/tests/test_orchestrator_api.py \
  agents/investment_team/tests/test_strategy_lab_routes.py \
  agents/investment_team/tests/test_temporal_bootstrap.py \
  agents/investment_team/tests/test_api_main_extra.py \
  -q --tb=line
```

Expected: PASS (or only pre-existing unrelated failures — do not ignore new failures in moved helpers).

- [ ] **Step 3: Push and open PR**

```bash
git push -u origin HEAD
gh pr create --title "Extract Strategy Lab dispatch/guards into orchestrator_api" --body "$(cat <<'EOF'
## Summary
- Move `_dispatch_strategy_lab_run`, `_fail_strategy_lab_run`, and concurrency guards into `strategy_lab/orchestrator_api.py`
- Keep thin `api.main` aliases; leave finalize/deferred Temporal helpers unchanged
- Retarget closed-over monkeypatches onto `orchestrator_api`

## Test plan
- [ ] `pytest` orchestrator_api + strategy_lab_routes + temporal_bootstrap
- [ ] ruff check on touched paths
- [ ] Confirm run/resume/restart route stubs still work via `api.main` name aliases

Closes #5515
EOF
)"
```

---

## Self-review (plan vs spec)

| Spec requirement | Task |
|---|---|
| Move five cluster-2 helpers | Task 2 |
| Hybrid `api.main` aliases | Task 2 |
| Lazy `_require_temporal` / no top-level `api.main` import | Task 2 |
| Finalize / `_DEFERRED_EXPORTS` unchanged | Tasks 1–2 |
| Retarget closed-over patches | Task 3 |
| Boundaries doc + monkeypatch caveat | Task 4 |
| Tests pass + ruff + PR | Task 5 |
| No store / paper-step / `_require_temporal` extract | Global constraints |

No placeholders; symbol names match the spec; line ranges are approximate and must be re-verified before cutting.
