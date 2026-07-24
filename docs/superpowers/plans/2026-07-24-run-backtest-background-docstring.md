# Add `_run_backtest_background` DbC Docstring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Design-by-Contract docstring to `_run_backtest_background` covering job-store preconditions, status transitions, cancel early-returns, and `_backtests` persistence.

**Architecture:** Documentation-only edit in the investment team API. No runtime behavior change. Style mirrors sibling `_run_paper_trading_background` in the same file. Verify by reading the function body against Preconditions/Postconditions.

**Tech Stack:** Python 3.10 docstring / Design-by-Contract style already used in `backend/agents/investment_team/api/main.py`.

**Spec:** `docs/superpowers/specs/2026-07-24-run-backtest-background-docstring-design.md`

## Global Constraints

- Docstring text only — do not change the function body or any other helper.
- Keep double-backtick wrapping on names (``job_id``, ``strategy``, etc.).
- Do not mention tracker issue numbers in the commit message or in source comments.
- Work only in the worktree at `.worktrees/docs-2224-run-backtest-background-docstring` on branch `docs/2224-run-backtest-background-docstring`.

---

### Task 1: Add `_run_backtest_background` docstring

**Files:**
- Modify: `backend/agents/investment_team/api/main.py` (`_run_backtest_background`; currently around lines 926–970)

**Interfaces:**
- Consumes: Existing parameters (`job_id`, `strategy`, `config`, `submitted_by`, `notes`) and helpers (`_bt_is_job_cancelled`, `_bt_update_job`, `_run_real_data_backtest`, `_backtests`)
- Produces: Complete Preconditions/Postconditions docstring; unchanged runtime behavior

- [ ] **Step 1: Confirm current function has no docstring**

Open `_run_backtest_background` and confirm it looks like:

```python
def _run_backtest_background(
    job_id: str,
    strategy: StrategySpec,
    config: BacktestConfig,
    submitted_by: str,
    notes: List[str],
) -> None:
    try:
        if _bt_is_job_cancelled(job_id):
            return
        _bt_update_job(job_id, status=_BT_JOB_STATUS_RUNNING)
        result, trades = _run_real_data_backtest(strategy, config)
        if _bt_is_job_cancelled(job_id):
            return
        backtest_id = f"bt-{uuid.uuid4().hex[:8]}"
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
    except HTTPException as exc:
        if _bt_is_job_cancelled(job_id):
            return
        _bt_update_job(job_id, status=_BT_JOB_STATUS_FAILED, error=str(exc.detail))
    except Exception as exc:
        logger.exception("Backtest job %s failed", job_id)
        if _bt_is_job_cancelled(job_id):
            return
        _bt_update_job(job_id, status=_BT_JOB_STATUS_FAILED, error=str(exc))
```

Note: no docstring between the signature and `try:`.

Optionally skim `_run_paper_trading_background` in the same file for the sibling Preconditions/Postconditions style to match.

- [ ] **Step 2: Insert the docstring**

Insert exactly this docstring immediately after the `) -> None:` line (before `try:`):

```python
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
          serialized ``RunBacktestResponse``; a new ``BacktestRecord`` is stored
          under ``_backtests[backtest_id]``
        - On ``HTTPException`` or other exceptions: job status becomes FAILED with
          an error string (unless cancelled — see below)
        - If ``_bt_is_job_cancelled(job_id)`` is true at any check point, return
          without writing COMPLETED or FAILED so the cancelled status is preserved
    """
```

Do not touch the function body.

- [ ] **Step 3: Manual contract check**

Re-read the body and tick each path against the docstring:

| Path / contract item | Matches body? |
|---|---|
| Preconditions name `job_id`, `strategy`, `config`, `submitted_by`, `notes` | yes |
| Success: RUNNING then COMPLETED + `_backtests[backtest_id]` | yes |
| `HTTPException` / other → FAILED with error (unless cancelled) | yes |
| Cancelled early-return: no COMPLETED/FAILED write | yes |
| Function body identical to before Step 2 | yes |

Expected: all yes.

- [ ] **Step 4: Commit**

```bash
git add backend/agents/investment_team/api/main.py
git commit -m "$(cat <<'EOF'
Document DbC contract for _run_backtest_background job status transitions.

EOF
)"
```

Expected: clean working tree on `docs/2224-run-backtest-background-docstring` with this commit on top of the design-doc commit.

---

## Self-review (plan vs spec)

1. **Spec coverage:** Goal (DbC docstring), non-goals (no logic/tests/sibling edits), exact docstring wording, cancel postcondition, success criteria — all covered by Task 1.
2. **Placeholders:** None.
3. **Consistency:** Parameter names, status constants, and cancel behavior match the live function body and the approved spec.
