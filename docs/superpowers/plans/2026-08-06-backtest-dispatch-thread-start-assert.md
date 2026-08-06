# Strengthen Backtest Dispatch Fallback Thread Assertions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Strengthen `test_backtest_dispatch_falls_back_to_thread_on_dispatch_failure` so it asserts the fallback thread is started and wired to `_run_backtest_background` with the correct job arguments.

**Architecture:** Test-only change. Production fallback in `api/main.py` already constructs `threading.Thread(target=_run_backtest_background, args=(job_id, strategy, config, submitted_by, notes), daemon=True)` and calls `start()`. Extend the existing mock-based regression test with assertions that would fail if `start()` were omitted or the target/args were wrong.

**Tech Stack:** Python 3.10+, pytest, `unittest.mock`

## Global Constraints

- Modify only `backend/agents/investment_team/tests/test_temporal_bootstrap.py`
- Do not change production fallback-dispatch logic
- Do not assert `daemon=True` (out of acceptance criteria)
- Never put GitHub issue numbers in code, comments, commit messages, or docs (PR body only)
- Worktree: `.worktrees/fix-5053-backtest-dispatch-thread-start` on branch `fix/5053-backtest-dispatch-thread-start`

## File map

| File | Role |
|---|---|
| `backend/agents/investment_team/tests/test_temporal_bootstrap.py` | Strengthen assertions in `test_backtest_dispatch_falls_back_to_thread_on_dispatch_failure` (~862–896) |
| `backend/agents/investment_team/api/main.py` | Reference only — production `Thread(...); thread.start()` at ~1282–1287; do not edit |

---

### Task 1: Strengthen fallback-thread assertions

**Files:**
- Modify: `backend/agents/investment_team/tests/test_temporal_bootstrap.py` (`test_backtest_dispatch_falls_back_to_thread_on_dispatch_failure`)
- Reference (do not modify): `backend/agents/investment_team/api/main.py:1282-1287`

**Interfaces:**
- Consumes: existing test setup (`thread_ctor` mock of `api_main.threading.Thread`, `strat` StrategySpec, POST `/backtests` response)
- Produces: stronger postconditions on the same test — no new public APIs

- [ ] **Step 1: Replace the weak end-of-test assertions with the strengthened block**

In `backend/agents/investment_team/tests/test_temporal_bootstrap.py`, replace the final two assertions of `test_backtest_dispatch_falls_back_to_thread_on_dispatch_failure`:

```python
    assert resp.status_code == 200  # not a 500
    thread_ctor.assert_called_once()  # fell back to the thread path
```

with:

```python
    assert resp.status_code == 200  # not a 500
    thread_ctor.assert_called_once()  # fell back to the thread path
    thread_ctor.return_value.start.assert_called_once()
    _, kwargs = thread_ctor.call_args
    assert kwargs["target"] is api_main._run_backtest_background
    args = kwargs["args"]
    assert args[0] == resp.json()["job_id"]
    assert args[1] is strat
    assert args[3] == "agent-1"
```

Keep the rest of the test unchanged (Temporal enabled, `start_backtest_workflow` raises, `Thread` mocked).

- [ ] **Step 2: Run the strengthened test and confirm it passes**

From `backend/` in the worktree (reuse main venv if worktree has none):

```bash
PYTHONPATH=".:agents" /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/pytest \
  agents/investment_team/tests/test_temporal_bootstrap.py::test_backtest_dispatch_falls_back_to_thread_on_dispatch_failure -q
```

Expected: `1 passed`

- [ ] **Step 3: Commit**

```bash
git add backend/agents/investment_team/tests/test_temporal_bootstrap.py
git commit -m "$(cat <<'EOF'
Strengthen backtest Temporal-fallback thread assertions.

Assert the fallback thread is started and targets _run_backtest_background
with the correct job_id, strategy, and submitted_by.
EOF
)"
```
