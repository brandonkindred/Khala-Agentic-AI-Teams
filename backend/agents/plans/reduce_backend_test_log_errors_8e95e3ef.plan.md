---
name: Reduce backend test/log errors
overview: Reduce the errors seen in the logs by (1) fixing the root cause of the ImportError when running pytest in agent-generated backends, and (2) improving error visibility so logs show actionable failure details instead of truncated messages.
todos:
  - id: set-pythonpath-in-run-pytest
    content: In shared/command_runner.py run_pytest(), set env_override so PYTHONPATH includes the project root (merge with existing PYTHONPATH if any) and pass it to run_command().
    status: completed
  - id: increase-orchestrator-test-failure-log
    content: In orchestrator.py line ~387, change the tests-failed log from summary[:200] to summary[:1200] (or 800) so the traceback is visible.
    status: completed
  - id: increase-backend-agent-build-failed-log
    content: In backend_agent/agent.py line ~392, change build_errors[:200] to build_errors[:800] in the Build FAILED log.
    status: completed
  - id: increase-backend-agent-last-error-logs
    content: In backend_agent/agent.py lines ~397-413, increase build_errors[:500] to build_errors[:800] for the repeated-failure reason and repeated_build_failure_reason[:300] to [:800] for the final error log.
    status: completed
  - id: run-tests-verify
    content: Run the software_engineering_team test suite (e.g. pytest from software_engineering_team/) to ensure command_runner and orchestrator/agent changes do not break existing tests.
    status: completed
isProject: false
---

# Reduce Backend Test and Log Errors

## Execution tasks (for agents)

Execute in this order:

1. **set-pythonpath-in-run-pytest** — In `software_engineering_team/shared/command_runner.py`, in `run_pytest()`, set `env_override` so `PYTHONPATH` includes the resolved project root. Prepend the project root to any existing `os.environ.get("PYTHONPATH", "")` so existing path is preserved, then pass `env_override={"PYTHONPATH": ...}` to `run_command()`.
2. **increase-orchestrator-test-failure-log** — In `software_engineering_team/orchestrator.py`, find the log line for "Tests failed for task" and change the slice from `summary[:200]` to `summary[:1200]` (or 800).
3. **increase-backend-agent-build-failed-log** — In `software_engineering_team/backend_agent/agent.py`, find the "Build FAILED" log and change `build_errors[:200]` to `build_errors[:800]`.
4. **increase-backend-agent-last-error-logs** — In the same file, in the repeated-build-failure block: use `build_errors[:800]` for the "Last error" text and log `repeated_build_failure_reason[:800]` instead of `[:300]`.
5. **run-tests-verify** — From `software_engineering_team/`, run `pytest` (or the project’s test command) and fix any regressions.

---

## What the logs show

- **Build verification** runs `python -m pytest` in the backend dir (e.g. `agent-written-apps/todo/backend`). Pytest **fails with exit_code=2** due to an **ImportError** when collecting `tests/test_task_model.py` (error at line 2 of that file).
- The failure is almost certainly because the test does `from app.models...` (or similar) while the project root is not on `PYTHONPATH`, so `app` is not importable when pytest runs with `cwd=backend_dir`.
- Logs **truncate** the failure: the orchestrator and backend agent log only the first **200** or **300** characters of the error, so you see `ImportError while importing test module '/home/deepthought/Dev/agent-writ` and never the full traceback (e.g. which import failed and why).

So there are two kinds of “errors” to reduce:

1. **Actual test failures** (ImportError) — fix by making the test run environment match how the agent structures projects.
2. **Unactionable log output** — fix by logging more of the failure so you (and the agent) can see the real error.

---

## 1. Fix pytest ImportError (root cause)

Agent-generated backends use the structure required by [backend_agent/prompts.py](software_engineering_team/backend_agent/prompts.py): `app/` package and `tests/` at the same level. Tests typically do `from app.models.task import Task` (or similar). Pytest is run with `cwd=project_path` but **no PYTHONPATH**, so the `app` package is not on `sys.path` and imports fail.

**Change:** In [shared/command_runner.py](software_engineering_team/shared/command_runner.py), in `run_pytest`, pass the project root on `PYTHONPATH` when calling `run_command`, so that `import app...` works without requiring the target repo to have a conftest or editable install.

- In `run_pytest`, set `env_override` so that `PYTHONPATH` includes `root` (and preserves any existing `PYTHONPATH` if present).
- Call: `run_command(cmd, cwd=project_path, timeout=TEST_TIMEOUT, env_override={"PYTHONPATH": root})` (or merge with `os.environ.get("PYTHONPATH", "")` if you want to preserve existing path).

This is a small, standard fix and should prevent the class of “ImportError while importing test module” you’re seeing.

---

## 2. Improve error visibility in logs

Today the **full** pytest error is passed to the agent (up to 2500 chars via `pytest_error_summary()` and 2000 chars in `code_review_issues`), but **human-readable logs** are heavily truncated, so the terminal doesn’t show the real failure.

**Orchestrator** ([orchestrator.py](software_engineering_team/orchestrator.py)):

- Line 387: `logger.warning("Tests failed for task %s: %s", task_id, summary[:200])` — increase the slice so the traceback is visible (e.g. **800–1200** chars), or log the full summary and use a short one-line summary in the same message.

**Backend agent** ([backend_agent/agent.py](software_engineering_team/backend_agent/agent.py)):

- Line 392: `build_errors[:200]` in the “Build FAILED” log — increase to **800** (or similar) so the critical part of the error is visible.
- Lines 407 and 413: “Last error” uses `build_errors[:500]` and then logs `repeated_build_failure_reason[:300]` — increase to **800** for both so that when the loop stops after 3 identical failures, the log still shows the meaningful part of the error.

Optional: if you want to avoid huge log lines, you could log a short one-liner and “Full error (N chars) written to …” and write the full `build_errors` / `summary` to a file under the job or repo path. The minimal improvement is just increasing the character limits above.

---

## 3. Optional: stronger agent guidance (only if ImportErrors persist)

If after (1) you still see import-related test failures (e.g. different layout in some repos), you can add one sentence to the backend prompt in [backend_agent/prompts.py](software_engineering_team/backend_agent/prompts.py): e.g. that tests run with the project root as the current working directory and that `from app.xxx` requires the project root on the Python path (and that the runner sets this, so tests should use `from app....` and not relative imports that assume another cwd). This is secondary to (1).

---

## Summary


| Area                       | Change                                                                                                                                                   |
| -------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **command_runner.py**      | In `run_pytest`, set `PYTHONPATH` to the project path (and optionally existing PYTHONPATH) so `import app` works.                                        |
| **orchestrator.py**        | Log more of the test failure summary (e.g. 800–1200 chars instead of 200).                                                                               |
| **backend_agent/agent.py** | Log more of `build_errors` (e.g. 800 chars) and of “Last error” / repeated failure reason (e.g. 800 chars) so failures are debuggable from the terminal. |


After these changes, the same workflow should (1) pass pytest when the only issue was missing PYTHONPATH for `app`, and (2) show enough error text in the logs when something else fails so you can act on it without opening extra logs or artifacts.