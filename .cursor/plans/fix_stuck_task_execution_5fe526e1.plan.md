---
name: Fix stuck task execution
overview: Fix the orchestrator so tasks never get stuck by removing hard dependency blocking (the Tech Lead's execution_order already handles ordering) and making branch creation resilient to stale branches from previous runs.
todos:
  - id: remove-dep-blocking
    content: Remove the dependency-checking block (lines 388-395) from the orchestrator main loop. The execution_order from the Tech Lead already encodes correct sequencing.
    status: completed
  - id: resilient-branches
    content: Update create_feature_branch in git_utils.py to recover from 'branch already exists' by deleting the stale branch and recreating from base.
    status: completed
  - id: track-failures
    content: Add a `failed` set in the orchestrator, populate it when tasks fail, and update end-of-run reporting to show completed/failed/remaining counts instead of 'likely stuck'.
    status: completed
  - id: no-silent-skips
    content: Ensure every task failure path (branch, merge, write, exception) adds the task to `failed` and logs the reason, instead of silently continuing.
    status: completed
isProject: false
---

# Fix Stuck Task Execution in Orchestrator

## Root Cause Analysis

There are **three compounding bugs** that cause the stuck-task cascade seen in the terminal output:

### Bug 1: Hard dependency blocking in the orchestrator (the main issue)

In [orchestrator.py](software_engineering_team/orchestrator.py) lines 388-395, the orchestrator checks each task's `dependencies` list against the `completed` set. If any dependency hasn't completed, the task is re-queued to the back of the queue:

```388:395:software_engineering_team/orchestrator.py
            missing_deps = [d for d in (task.dependencies or []) if d not in completed]
            if missing_deps:
                logger.warning(
                    "Task %s blocked: missing dependencies %s - re-queuing (completed: %s/%s)",
                    task_id, missing_deps, len(completed), total_tasks,
                )
                execution_queue.append(task_id)
                continue
```

This is **redundant and harmful**: the Tech Lead already produces an `execution_order` that respects the dependency graph (topologically sorted). If any task fails or is skipped, every downstream task gets stuck in an infinite re-queue loop until `max_passes` is exhausted.

### Bug 2: Branch-already-exists is a fatal error with no recovery

In [orchestrator.py](software_engineering_team/orchestrator.py) lines 412-416, when `create_feature_branch` fails (e.g., a stale branch from a prior run), the task is silently skipped:

```412:416:software_engineering_team/orchestrator.py
                ok, msg = create_feature_branch(path, DEVELOPMENT_BRANCH, task_id)
                if not ok:
                    logger.error("[%s] Feature branch creation FAILED: %s - skipping task", task_id, msg)
                    continue
```

This caused the `backend-data-models` failure in the log, which then cascaded to block `backend-crud-api`, `backend-validation`, `frontend-list-component`, `frontend-form-component`, and `frontend-detail-component` -- 6 tasks blocked by 1 stale branch.

### Bug 3: Failed tasks are not tracked

When a task fails for any reason (branch creation, merge failure, exception), it is simply skipped via `continue`. It is never added to `completed` or any `failed` set. There is no way to distinguish "hasn't run yet" from "ran and failed." This makes end-of-run reporting misleading.

---

## Planned Changes

### 1. Remove hard dependency blocking from the orchestrator

**File:** [orchestrator.py](software_engineering_team/orchestrator.py) lines 388-395

**Change:** Delete the `missing_deps` check entirely. The `execution_order` from the Tech Lead already encodes dependency-respecting sequencing. Dependencies remain as metadata for the Tech Lead's planning and progress reviews but are not enforced at runtime.

This also means the `max_passes` safety limit can be simplified -- tasks are never re-queued for dependency reasons, only new tasks are dynamically added (from QA fix tasks or Tech Lead reviews).

### 2. Make `create_feature_branch` handle existing branches

**File:** [shared/git_utils.py](software_engineering_team/shared/git_utils.py) in `create_feature_branch()` (line 38)

**Change:** When `git checkout -b` fails because the branch already exists, recover by:

1. Checking out the base branch
2. Deleting the stale feature branch (`git branch -D`)
3. Recreating it from the base branch

This handles branches left over from previous runs, partial failures, or retries.

### 3. Track failed tasks and improve end-of-run reporting

**File:** [orchestrator.py](software_engineering_team/orchestrator.py)

**Changes:**

- Add a `failed` set alongside `completed` to track tasks that failed
- When a task fails (branch creation after recovery attempt, merge failure, exception, etc.), add it to `failed`
- At end of run, report `completed`, `failed`, and `remaining` counts clearly
- Replace the misleading "likely stuck due to unresolved dependencies" warning with accurate failure reporting

### 4. Ensure failed tasks don't silently disappear

**File:** [orchestrator.py](software_engineering_team/orchestrator.py) -- each agent block (devops/backend/frontend)

**Change:** In every code path where a task can fail (branch creation line 414-416, merge failure, clarification timeout, write failure), explicitly add the task_id to the `failed` set and log the failure reason. Currently, some failures just `continue` without any tracking.