# Always-Return Backtest Worker Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `_run_backtest_background` return a terminal status string on every path, and have `run_backtest_activity` branch on that return instead of re-reading the job store.

**Architecture:** Persist COMPLETED/FAILED via existing `_bt_update_job` as today; on cancel checkpoints return `_BT_JOB_STATUS_CANCELLED` without overwriting terminal cancel. Every exit returns one of `_BT_JOB_STATUS_{COMPLETED,FAILED,CANCELLED}`. The Temporal activity keeps its entry COMPLETED short-circuit via `_backtest_job_status`, then uses the worker return for outcome.

**Tech Stack:** Python 3.10+, pytest, FastAPI investment_team API, Temporal activity wrapper

## Global Constraints

- Work only in worktree: `.worktrees/fix-5064-backtest-return-type` on branch `fix/5064-backtest-return-type`
- Design-by-Contract: update Preconditions / Postconditions on changed functions
- Never put GitHub issue numbers in code, comments, commit messages, or docs (PR body only)
- Ruff line-length 120; run lint on touched files
- Coverage: new/changed code ≥ 90%
- Do not change thread-dispatch behavior (Thread target may discard return)
- Do not redesign job-store APIs or other background workers
- Spec: `docs/superpowers/specs/2026-08-06-backtest-background-return-status-design.md`

## File map

| File | Role |
|---|---|
| `backend/agents/investment_team/api/main.py` | `_run_backtest_background` return type + returns + docstring |
| `backend/agents/investment_team/temporal/workflows.py` | `run_backtest_activity` consumes worker return |
| `backend/agents/investment_team/tests/test_api_main_extra.py` | Assert worker return values |
| `backend/agents/investment_team/tests/test_temporal_bootstrap.py` | Activity stubs return status strings |

---

### Task 1: Worker return-value tests (TDD)

**Files:**
- Modify: `backend/agents/investment_team/tests/test_api_main_extra.py` (`test_run_backtest_background_completes`, `test_run_backtest_background_handles_backtest_execution_error`, `test_run_backtest_background_handles_generic_exception`, `test_run_backtest_background_early_cancellation`, `test_run_backtest_background_retry_reuses_backtest_id` ~969–1168)
- Test: same file

**Interfaces:**
- Consumes: `api_main._run_backtest_background(...) -> str` (not yet implemented; tests must fail)
- Produces: assertions that lock COMPLETED / FAILED / CANCELLED return contract for Task 2

- [ ] **Step 1: Update failing assertions on existing worker tests**

In `test_run_backtest_background_completes`, capture and assert the return:

```python
    status = api_main._run_backtest_background("job-1", strategy, config, "tester", [])
    # Final state update is to COMPLETED.
    assert state.get("status") == "completed"
    assert state.get("backtest_id", "").startswith("bt-")
    assert status == api_main._BT_JOB_STATUS_COMPLETED
```

In `test_run_backtest_background_handles_backtest_execution_error`:

```python
    status = api_main._run_backtest_background("job-2", strategy, config, "tester", None)
    assert state.get("status") == "failed"
    assert state.get("error") == "bad strategy"
    assert status == api_main._BT_JOB_STATUS_FAILED
```

In `test_run_backtest_background_handles_generic_exception`:

```python
    status = api_main._run_backtest_background("job-3", strategy, config, "tester", None)
    assert state.get("status") == "failed"
    assert "network down" in (state.get("error") or "")
    assert status == api_main._BT_JOB_STATUS_FAILED
```

In `test_run_backtest_background_early_cancellation`:

```python
    status = api_main._run_backtest_background("job-4", strategy, config, "tester", None)
    # No update calls — early return.
    assert state == {}
    assert status == api_main._BT_JOB_STATUS_CANCELLED
```

In `test_run_backtest_background_retry_reuses_backtest_id`, assert both invocations return COMPLETED:

```python
    status1 = api_main._run_backtest_background("job-retry", strategy, config, "tester", [])
    first_backtest_id = state["backtest_id"]

    status2 = api_main._run_backtest_background("job-retry", strategy, config, "tester", [])
    second_backtest_id = state["backtest_id"]

    assert first_backtest_id == second_backtest_id
    # The second run overwrote the same record rather than adding a duplicate.
    assert len(api_main._backtests.values()) == 1
    assert status1 == api_main._BT_JOB_STATUS_COMPLETED
    assert status2 == api_main._BT_JOB_STATUS_COMPLETED
```

- [ ] **Step 2: Run tests to verify they fail**

Run from `backend/`:

```bash
cd backend && .venv/bin/python -m pytest \
  agents/investment_team/tests/test_api_main_extra.py::test_run_backtest_background_completes \
  agents/investment_team/tests/test_api_main_extra.py::test_run_backtest_background_handles_backtest_execution_error \
  agents/investment_team/tests/test_api_main_extra.py::test_run_backtest_background_handles_generic_exception \
  agents/investment_team/tests/test_api_main_extra.py::test_run_backtest_background_early_cancellation \
  agents/investment_team/tests/test_api_main_extra.py::test_run_backtest_background_retry_reuses_backtest_id \
  -v
```

Expected: FAIL — return is `None`, so assertions against `_BT_JOB_STATUS_*` fail (or cancel assertion fails).

- [ ] **Step 3: Commit the failing tests**

```bash
git add backend/agents/investment_team/tests/test_api_main_extra.py
git commit -m "$(cat <<'EOF'
Fail tests until backtest worker returns terminal status strings.

EOF
)"
```

---

### Task 2: Implement `_run_backtest_background` always-return contract

**Files:**
- Modify: `backend/agents/investment_team/api/main.py:1155-1232`
- Test: `backend/agents/investment_team/tests/test_api_main_extra.py` (Task 1 assertions)

**Interfaces:**
- Consumes: `_bt_is_job_cancelled`, `_bt_update_job`, `_run_real_data_backtest`, `_BT_JOB_STATUS_*`
- Produces: `def _run_backtest_background(...) -> str` returning COMPLETED / FAILED / CANCELLED

- [ ] **Step 1: Change signature and docstring postconditions**

Replace the signature and Postconditions section:

```python
def _run_backtest_background(
    job_id: str,
    strategy: StrategySpec,
    config: BacktestConfig,
    submitted_by: str,
    notes: List[str],
) -> str:
    """Background worker: run a real-data backtest and persist the completed record.

    Long-running (market data + sandbox execution), so this runs off the request
    thread (or via Temporal dispatch) to avoid proxy timeouts.

    Preconditions:
        - ``job_id`` must already exist in the backtest job store (created by
          ``run_backtest`` / ``_bt_create_job``), typically with status PENDING
        - ``strategy`` must be a valid ``StrategySpec`` suitable for
          ``_run_real_data_backtest``
        - ``config`` must be a valid ``BacktestConfig``
        - ``submitted_by`` and ``notes`` are recorded on the resulting
          ``BacktestRecord`` as-is

    Postconditions:
        - On the success path: job status becomes RUNNING then COMPLETED with a
          serialized ``RunBacktestResponse``; a ``BacktestRecord`` is stored under
          ``_backtests[backtest_id]``, where ``backtest_id`` is derived
          deterministically from ``job_id``. A second invocation for the same
          ``job_id`` (e.g. a Temporal activity retry that lands after a worker
          crash left the job at RUNNING) therefore overwrites the same record
          instead of orphaning a duplicate. Returns ``_BT_JOB_STATUS_COMPLETED``.
        - On ``BacktestExecutionError`` or other exceptions: job status becomes
          FAILED with an error string, unless a cancel check already returned.
          Returns ``_BT_JOB_STATUS_FAILED`` after persisting FAILED.
        - If ``_bt_is_job_cancelled(job_id)`` is true at a check point, return
          ``_BT_JOB_STATUS_CANCELLED`` without writing COMPLETED or FAILED so the
          cancelled status visible at that check is preserved. Updates use
          unconditional ``_bt_update_job``, so a cancel that lands between a
          check and the next update can still be overwritten with RUNNING,
          COMPLETED, or FAILED.
    """
```

- [ ] **Step 2: Return status constants on every path**

Replace the body returns (keep persistence logic identical):

```python
    try:
        if _bt_is_job_cancelled(job_id):
            return _BT_JOB_STATUS_CANCELLED
        _bt_update_job(job_id, status=_BT_JOB_STATUS_RUNNING)
        result, trades = _run_real_data_backtest(strategy, config)
        if _bt_is_job_cancelled(job_id):
            return _BT_JOB_STATUS_CANCELLED
        # Deterministic (not random) so a retry of the same job_id — e.g. a
        # Temporal activity retry after a worker crash left the job RUNNING —
        # overwrites the same record instead of minting a duplicate.
        backtest_id = f"bt-{hashlib.sha256(job_id.encode()).hexdigest()[:8]}"
        now = _now()
        record = BacktestRecord(
            backtest_id=backtest_id,
            strategy_id=strategy.strategy_id,
            strategy=strategy,
            config=config,
            submitted_by=submitted_by,
            submitted_at=now,
            completed_at=now,
            result=result,
            notes=notes,
            trades=trades,
        )
        with _lock:
            _backtests[backtest_id] = record
        _bt_update_job(
            job_id,
            status=_BT_JOB_STATUS_COMPLETED,
            result=RunBacktestResponse(backtest=record).model_dump(mode="json"),
            backtest_id=backtest_id,
        )
        return _BT_JOB_STATUS_COMPLETED
    except BacktestExecutionError as exc:
        if _bt_is_job_cancelled(job_id):
            return _BT_JOB_STATUS_CANCELLED
        _bt_update_job(job_id, status=_BT_JOB_STATUS_FAILED, error=str(exc.detail))
        return _BT_JOB_STATUS_FAILED
    except Exception as exc:
        logger.exception("Backtest job %s failed", job_id)
        if _bt_is_job_cancelled(job_id):
            return _BT_JOB_STATUS_CANCELLED
        _bt_update_job(job_id, status=_BT_JOB_STATUS_FAILED, error=str(exc))
        return _BT_JOB_STATUS_FAILED
```

- [ ] **Step 3: Run worker tests to verify they pass**

```bash
cd backend && .venv/bin/python -m pytest \
  agents/investment_team/tests/test_api_main_extra.py::test_run_backtest_background_completes \
  agents/investment_team/tests/test_api_main_extra.py::test_run_backtest_background_handles_backtest_execution_error \
  agents/investment_team/tests/test_api_main_extra.py::test_run_backtest_background_handles_generic_exception \
  agents/investment_team/tests/test_api_main_extra.py::test_run_backtest_background_early_cancellation \
  agents/investment_team/tests/test_api_main_extra.py::test_run_backtest_background_retry_reuses_backtest_id \
  -v
```

Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add backend/agents/investment_team/api/main.py
git commit -m "$(cat <<'EOF'
Return terminal status strings from _run_backtest_background.

EOF
)"
```

---

### Task 3: Activity consumes worker return (TDD)

**Files:**
- Modify: `backend/agents/investment_team/tests/test_temporal_bootstrap.py` (`test_run_backtest_activity_reconstructs_models_and_runs_worker`, `test_run_backtest_activity_raises_when_job_failed`, `test_run_backtest_activity_reports_cancelled_status` ~260–337)
- Modify: `backend/agents/investment_team/temporal/workflows.py:57-107`
- Test: `backend/agents/investment_team/tests/test_temporal_bootstrap.py`

**Interfaces:**
- Consumes: `_run_backtest_background(...) -> str` from Task 2
- Produces: `run_backtest_activity` branches on worker return; entry short-circuit still uses `_backtest_job_status`

- [ ] **Step 1: Update activity tests so stubs return status strings**

In `test_run_backtest_activity_reconstructs_models_and_runs_worker`, make the stub return COMPLETED (side-effect list append must still happen). Replace the lambda with:

```python
    calls = []

    def _bg(*a):
        calls.append(a)
        return api_main._BT_JOB_STATUS_COMPLETED

    monkeypatch.setattr(api_main, "_run_backtest_background", _bg)
```

Keep `_backtest_job_status` returning `None` for the entry short-circuit only (no second read needed after implementation).

In `test_run_backtest_activity_raises_when_job_failed`:

```python
    monkeypatch.setattr(inv_models, "StrategySpec", lambda **kw: object())
    monkeypatch.setattr(inv_models, "BacktestConfig", lambda **kw: object())
    monkeypatch.setattr(api_main, "_backtest_job_status", lambda jid: None)
    monkeypatch.setattr(
        api_main,
        "_run_backtest_background",
        lambda *a: api_main._BT_JOB_STATUS_FAILED,
    )

    from temporalio.exceptions import ApplicationError

    with pytest.raises(ApplicationError, match="failed"):
        run_backtest_activity("job-x", {}, {}, "agent", [])
```

In `test_run_backtest_activity_reports_cancelled_status`:

```python
    monkeypatch.setattr(inv_models, "StrategySpec", lambda **kw: object())
    monkeypatch.setattr(inv_models, "BacktestConfig", lambda **kw: object())
    monkeypatch.setattr(api_main, "_backtest_job_status", lambda jid: None)
    monkeypatch.setattr(
        api_main,
        "_run_backtest_background",
        lambda *a: api_main._BT_JOB_STATUS_CANCELLED,
    )

    result = run_backtest_activity("job-cancelled", {}, {}, "agent", [])

    assert result == {"job_id": "job-cancelled", "status": "cancelled"}
```

Leave `test_run_backtest_activity_is_idempotent_when_already_completed` unchanged (still uses `_backtest_job_status` at entry).

- [ ] **Step 2: Run activity tests to verify they fail under old activity code**

```bash
cd backend && .venv/bin/python -m pytest \
  agents/investment_team/tests/test_temporal_bootstrap.py::test_run_backtest_activity_reconstructs_models_and_runs_worker \
  agents/investment_team/tests/test_temporal_bootstrap.py::test_run_backtest_activity_raises_when_job_failed \
  agents/investment_team/tests/test_temporal_bootstrap.py::test_run_backtest_activity_reports_cancelled_status \
  -v
```

Expected: FAIL — old activity still calls `_backtest_job_status` after the worker; with a single-return stub of `None` at entry only, post-call status is `None`, so failed/cancelled cases no longer raise/report correctly (or StopIteration if any leftover `iter` stubs remain — after Step 1 they should be single-value stubs).

Note: after Step 1 alone, `raises_when_job_failed` / `reports_cancelled` will fail because the activity ignores the worker return and sees `None` from `_backtest_job_status`. The reconstruct test may incorrectly PASS (treats non-failed/non-cancelled as completed) until Step 3 — that is acceptable; the failed/cancelled tests are the red signal.

- [ ] **Step 3: Update `run_backtest_activity` to consume the worker return**

Replace the worker call + outcome branch (keep imports needed for entry short-circuit):

```python
    from investment_team.api.main import (
        _BT_JOB_STATUS_CANCELLED,
        _BT_JOB_STATUS_COMPLETED,
        _BT_JOB_STATUS_FAILED,
        _backtest_job_status,
        _run_backtest_background,
    )
    from investment_team.models import BacktestConfig, StrategySpec

    if _backtest_job_status(job_id) == _BT_JOB_STATUS_COMPLETED:
        return {"job_id": job_id, "status": "completed"}

    final_status = _run_backtest_background(
        job_id,
        StrategySpec(**strategy),
        BacktestConfig(**config),
        submitted_by,
        notes,
    )

    if final_status == _BT_JOB_STATUS_FAILED:
        raise ApplicationError(f"Backtest {job_id} failed", type="BacktestFailed")
    if final_status == _BT_JOB_STATUS_CANCELLED:
        return {"job_id": job_id, "status": "cancelled"}
    return {"job_id": job_id, "status": "completed"}
```

Update the activity docstring Postconditions to say outcome comes from the worker return (entry short-circuit still uses the job store).

- [ ] **Step 4: Run activity tests to verify they pass**

```bash
cd backend && .venv/bin/python -m pytest \
  agents/investment_team/tests/test_temporal_bootstrap.py::test_run_backtest_activity_reconstructs_models_and_runs_worker \
  agents/investment_team/tests/test_temporal_bootstrap.py::test_run_backtest_activity_is_idempotent_when_already_completed \
  agents/investment_team/tests/test_temporal_bootstrap.py::test_run_backtest_activity_raises_when_job_failed \
  agents/investment_team/tests/test_temporal_bootstrap.py::test_run_backtest_activity_reports_cancelled_status \
  -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/investment_team/temporal/workflows.py \
  backend/agents/investment_team/tests/test_temporal_bootstrap.py
git commit -m "$(cat <<'EOF'
Drive backtest activity outcome from worker status return.

EOF
)"
```

---

### Task 4: Lint + package verification

**Files:**
- Verify only (no new production code unless lint forces a fix)

- [ ] **Step 1: Lint touched files**

```bash
cd backend && .venv/bin/ruff check \
  agents/investment_team/api/main.py \
  agents/investment_team/temporal/workflows.py \
  agents/investment_team/tests/test_api_main_extra.py \
  agents/investment_team/tests/test_temporal_bootstrap.py
```

Expected: no issues

- [ ] **Step 2: Run the focused investment_team suites for touched areas**

```bash
cd backend && .venv/bin/python -m pytest \
  agents/investment_team/tests/test_api_main_extra.py \
  agents/investment_team/tests/test_temporal_bootstrap.py \
  -q
```

Expected: all pass

- [ ] **Step 3: Commit any lint-only fixes if needed; otherwise skip**

If lint required edits:

```bash
git add <touched files>
git commit -m "$(cat <<'EOF'
Fix lint on backtest status-return changes.

EOF
)"
```

If clean, do not create an empty commit.

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| `-> str` + COMPLETED/FAILED/CANCELLED on every exit | Task 2 |
| Cancel returns CANCELLED without COMPLETED/FAILED write | Task 2 |
| Activity uses worker return; keeps entry COMPLETED short-circuit | Task 3 |
| Worker tests assert returns | Task 1 |
| Activity stubs return status strings | Task 3 |
| Thread dispatch unchanged | (no task — out of scope / preserved) |
| Lint + touched tests pass | Task 4 |
