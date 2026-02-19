---
name: Agent crash handling and repair team
overview: Add prominent logging and a distinct job status when agents crash catastrophically, and introduce a Repair Agent team that fixes bugs in the agent codebase when an agent process raises an exception (NameError, SyntaxError, ImportError, etc.).
todos:
  - id: crash-banner-1
    content: Add helper to parse traceback and extract file_path, line_number, function_name (use traceback module or regex on last frame)
    status: completed
  - id: crash-banner-2
    content: Implement _log_agent_crash_banner(task_id, agent_type, exception, log_prefix) with ERROR level, repeated ! lines, task_id, agent_type, exception type/message, location
    status: completed
  - id: crash-banner-3
    content: Call _log_agent_crash_banner from backend worker except Exception block (before logger.exception)
    status: completed
  - id: crash-banner-4
    content: Call _log_agent_crash_banner from frontend worker except Exception block (before logger.exception)
    status: completed
  - id: job-status-1
    content: Add JOB_STATUS_AGENT_CRASH = 'agent_crash' to shared/job_store.py with docstring
    status: completed
  - id: job-status-2
    content: "In backend except block: build agent_crash_details dict (task_id, agent_type, exception_type, exception_message, traceback, file_path, line_number)"
    status: completed
  - id: job-status-3
    content: "In backend except block: call update_job(job_id, status=JOB_STATUS_AGENT_CRASH, error=str(e), agent_crash_details=...)"
    status: completed
  - id: job-status-4
    content: "In frontend except block: same agent_crash_details and update_job with JOB_STATUS_AGENT_CRASH"
    status: completed
  - id: repair-models-1
    content: Create repair_agent/models.py with RepairInput (traceback, exception_type, exception_message, task_id, agent_type, agent_source_path)
    status: completed
  - id: repair-models-2
    content: Add RepairOutput with suggested_fixes (List[Dict]), summary, applied fields
    status: completed
  - id: repair-prompts-1
    content: Create repair_agent/prompts.py with REPAIR_PROMPT instructing LLM to parse traceback, identify root cause, produce minimal edits
    status: completed
  - id: repair-prompts-2
    content: "Add constraint: only edit files under agent_source_path; no app code changes"
    status: completed
  - id: repair-agent-1
    content: Create repair_agent/agent.py with RepairExpertAgent.run(RepairInput) -> RepairOutput
    status: completed
  - id: repair-agent-2
    content: Implement LLM call with REPAIR_PROMPT and parse JSON output for suggested_fixes (file_path, line_start, line_end, replacement_content)
    status: completed
  - id: repair-agent-3
    content: Create repair_agent/__init__.py with exports
    status: completed
  - id: repair-integrate-1
    content: Define REPAIRABLE_EXCEPTIONS = (NameError, SyntaxError, ImportError, AttributeError, IndentationError, ModuleNotFoundError)
    status: completed
  - id: repair-integrate-2
    content: "In backend except block: if type(e) in REPAIRABLE_EXCEPTIONS and task_id not in repaired_tasks, invoke repair agent"
    status: completed
  - id: repair-integrate-3
    content: Resolve agent_source_path (Path(__file__).resolve().parent for software_engineering_team/)
    status: completed
  - id: repair-integrate-4
    content: "Apply suggested_fixes: validate path under agent_source_path, write replacement content to file"
    status: completed
  - id: repair-integrate-5
    content: "Re-queue task: remove from failed, append to backend_queue or frontend_queue with state_lock"
    status: completed
  - id: repair-integrate-6
    content: "Add repaired_tasks: set to worker state; add task_id after repair attempt (max 1 per task)"
    status: completed
  - id: repair-integrate-7
    content: Wrap repair invocation in try/except; on repair agent crash log and skip re-queue
    status: completed
  - id: repair-integrate-8
    content: Mirror repair logic in frontend except block (frontend_queue, repaired_tasks)
    status: completed
  - id: repair-status-clear
    content: When task completes after repair, clear JOB_STATUS_AGENT_CRASH (job continues as RUNNING then COMPLETED)
    status: completed
  - id: test-crash-banner
    content: "Add test: mock exception with traceback, call _log_agent_crash_banner, assert ERROR logged with task_id and exception"
    status: completed
  - id: test-repair-agent
    content: "Add test_repair_agent.py: mock LLM returns suggested_fixes for NameError traceback, assert RepairOutput has file_path and replacement"
    status: completed
  - id: test-repair-apply
    content: "Add test: repair agent suggests import fix, apply_fixes validates path and writes; assert file content updated"
    status: completed
  - id: test-repair-requeue
    content: "Add orchestrator test: on backend crash with NameError, repair applied, task re-queued, worker picks it up"
    status: completed
isProject: false
---

# Agent crash handling and repair team

## 1. Prominent logging for agent crashes

**Current behavior:** When backend/frontend agents raise an unhandled exception, the orchestrator logs `logger.exception("%s[%s] Backend task exception", ...)` and records `failed[task_id] = f"Unhandled exception: {e}"`. The traceback is printed via `logger.exception` but there is no visual emphasis.

**Changes in** `[software_engineering_team/orchestrator.py](software_engineering_team/orchestrator.py)`:

- Add `_log_agent_crash_banner(task_id, agent_type, exception, log_prefix)` that:
  - Logs at `logger.error` with a prominent banner (similar to `_log_task_completion_banner` but for failures)
  - Uses repeated `!` or `#` lines for visibility
  - Includes: task_id, agent_type (backend/frontend), exception type and message, file:line from traceback
  - Logs the full traceback (already done by `logger.exception`; optionally repeat a truncated form in the banner)
- Call this from both the backend and frontend `except Exception` blocks (lines 912–916 and 1030–1034) before the existing `logger.exception` call.

**Example banner format:**

```
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
  *** AGENT CRASH (Backend) ***
  Task: backend-tests-tenant-isolation-enforcement
  Exception: NameError: name 'compute_spec_content_chars' is not defined
  Location: backend_agent/agent.py:407 in _plan_task
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
```

---

## 2. Distinct job status for agent crashes

**Changes in** `[software_engineering_team/shared/job_store.py](software_engineering_team/shared/job_store.py)`:

- Add `JOB_STATUS_AGENT_CRASH = "agent_crash"` (or `JOB_STATUS_NEEDS_REPAIR`).
- Document that this status indicates an agent process crashed (not an LLM or build failure).

**Changes in** `[software_engineering_team/orchestrator.py](software_engineering_team/orchestrator.py)`:

- In the backend and frontend `except Exception` blocks, after logging and setting `failed[task_id]`:
  - Call `update_job(job_id, status=JOB_STATUS_AGENT_CRASH, error=str(e), agent_crash_details={...})`.
  - `agent_crash_details` should include: `task_id`, `agent_type`, `exception_type`, `exception_message`, `traceback` (full or truncated), `file_path`, `line_number` (parsed from traceback).
- Ensure the API/job store can persist and return `agent_crash_details` for UI or repair logic.

**Note:** The orchestrator continues after an agent crash (other tasks may still run). The job status distinguishes “agent crashed” from “task failed due to build/QA/review”. If multiple agents crash, the last crash’s details can overwrite; alternatively, store a list of crash events. For v1, a single `agent_crash_details` is sufficient.

---

## 3. Repair agent team

**Purpose:** When an agent process crashes with a code error (NameError, SyntaxError, ImportError, AttributeError, etc.), a Repair Agent analyzes the traceback and applies fixes to the agent codebase (e.g. `software_engineering_team/backend_agent/agent.py`).

**Trigger:** Only when the orchestrator catches an `Exception` in the backend or frontend worker (excluding `LLMError`, `httpx.HTTPError`). Do not trigger for build failures or task workflow failures.

**New module:** `software_engineering_team/repair_agent/`

- `agent.py` – `RepairExpertAgent` with:
  - `run(RepairInput) -> RepairOutput`
  - Input: `traceback: str`, `exception_type: str`, `exception_message: str`, `task_id: str`, `agent_type: str`, `agent_source_path: Path` (path to `software_engineering_team/` or repo root)
  - Output: `suggested_fixes: List[Dict]` (file path, line range, replacement content), `summary: str`, `applied: bool`
- `models.py` – `RepairInput`, `RepairOutput`
- `prompts.py` – Prompt instructing the LLM to parse the traceback, identify the root cause (e.g. missing import, typo), and produce minimal edits to fix the agent code. Emphasize: only edit files under `agent_source_path`; no changes to app code.

**Flow in orchestrator:**

1. Catch exception in backend/frontend worker.
2. Log crash banner and update job status (steps 1 and 2 above).
3. Parse traceback to extract `file_path` and `line_number` (use `traceback` module or regex).
4. If `exception_type` is one of `NameError`, `SyntaxError`, `ImportError`, `AttributeError`, `IndentationError`, `ModuleNotFoundError` (or a configurable list):
  - Resolve `agent_source_path` (e.g. `Path(__file__).resolve().parent` for `software_engineering_team/`).
  - Call `repair_agent.run(RepairInput(...))`.
  - If `RepairOutput.suggested_fixes` is non-empty, apply edits (write to files).
  - Re-queue the task: remove from `failed`, append to `backend_queue` or `frontend_queue`, and continue the worker loop (the worker is still running; it will pick up the re-queued task on the next iteration).
5. If repair is applied and task is re-queued, clear or update `JOB_STATUS_AGENT_CRASH` when the task eventually completes (or fails again for a different reason).
6. Add a guard: max 1 repair attempt per task per job to avoid infinite repair loops. Track `repaired_tasks: set` in the worker state.

**Safety:**

- Restrict edits to paths under `agent_source_path` (e.g. `software_engineering_team/`). Reject any suggested path outside that tree.
- Validate that suggested fixes are minimal (e.g. single-file, small line ranges). Reject large replacements if desired.
- If the repair agent itself crashes, log and do not retry repair for that task.

---

## 4. Implementation order

1. Add `_log_agent_crash_banner` and call it from both exception handlers.
2. Add `JOB_STATUS_AGENT_CRASH` and `update_job` with `agent_crash_details`.
3. Create `repair_agent` module (models, prompts, agent).
4. Integrate repair invocation and fix application in the orchestrator exception handlers.
5. Add re-queue logic and `repaired_tasks` guard.
6. Add tests for the repair agent (mock LLM, assert it suggests an import fix for a NameError traceback) and for the crash banner.

---

## 5. Files to create or modify


| File                                                   | Action                                                       |
| ------------------------------------------------------ | ------------------------------------------------------------ |
| `software_engineering_team/shared/job_store.py`        | Add `JOB_STATUS_AGENT_CRASH`                                 |
| `software_engineering_team/orchestrator.py`            | Add crash banner, status update, repair invocation, re-queue |
| `software_engineering_team/repair_agent/agent.py`      | New – RepairExpertAgent                                      |
| `software_engineering_team/repair_agent/models.py`     | New – RepairInput, RepairOutput                              |
| `software_engineering_team/repair_agent/prompts.py`    | New – repair prompt                                          |
| `software_engineering_team/repair_agent/__init__.py`   | New – exports                                                |
| `software_engineering_team/tests/test_repair_agent.py` | New – unit tests                                             |


---

## 6. Open questions

- **Repair agent scope (resolved):** Start with `NameError`, `SyntaxError`, `ImportError`, `AttributeError`, `IndentationError`, `ModuleNotFoundError`. Expand to `TypeError`, `KeyError`, or other runtime errors in a future iteration if needed.
- **Re-queue semantics:** After repair, the task goes back to the queue. The worker may have already moved on. The backend worker loop is `while True` with `task_id = _pop_runnable_task(...)`. So we need to re-append the task_id to `backend_queue` (or `frontend_queue`) while holding `state_lock`. The worker will then pick it up on a subsequent iteration. This works as long as the worker hasn’t exited (e.g. due to empty queue). After an exception, the worker continues to the next iteration of the `while True` loop, so re-queuing is valid.
- **API surface:** Does the job API need to expose `agent_crash_details` or `JOB_STATUS_AGENT_CRASH` for a UI? The plan assumes `update_job` accepts arbitrary kwargs; the job store already merges them into the job dict.

