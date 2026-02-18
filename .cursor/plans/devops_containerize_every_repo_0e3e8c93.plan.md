---
name: DevOps containerize every repo
overview: The DevOps agent currently only runs for a repo when at least one task for that repo completed successfully. That skips the frontend repo when there are no frontend tasks or all frontend tasks failed. The fix is to trigger DevOps for every git repo created by the pipeline (backend and frontend) based on repo existence, and optionally run DevOps when each worker drains so it happens "after each respective agent is done."
todos: []
isProject: false
---

# DevOps: Containerize Every Repo After Respective Agent Completes

## Root cause

In [orchestrator.py](software_engineering_team/orchestrator.py), the DevOps block (lines 478–497) only runs for a repo when **at least one task for that repo completed successfully**:

```749:497:software_engineering_team/orchestrator.py
        # DevOps: containerize and deploy backend and frontend (triggered by Tech Lead after all tasks)
        devops_agent = agents.get("devops")
        if devops_agent and completed_code_task_ids:
            has_backend_tasks = any(
                all_tasks.get(tid) and all_tasks[tid].assignee == "backend" for tid in completed_code_task_ids
            )
            has_frontend_tasks = any(
                all_tasks.get(tid) and all_tasks[tid].assignee == "frontend" for tid in completed_code_task_ids
            )
            if has_backend_tasks and backend_dir.is_dir() and (backend_dir / ".git").exists():
                ...
            if has_frontend_tasks and frontend_dir.is_dir() and (frontend_dir / ".git").exists():
                ...
```

So the frontend repo is skipped when:

- The plan had **no frontend tasks**, or
- **All frontend tasks failed** (so no frontend task id is in `completed_code_task_ids`).

The backend and frontend repos are only created when their workers run at least one task (they call `ensure_*_project_initialized` and `git_setup`). So a repo with `.git` is exactly “a repo created by the coding agents.”

## Approach

1. **Trigger DevOps for every created repo**
  Run DevOps for a repo whenever that repo **exists and has been initialized** (directory exists and has `.git`), instead of requiring `has_backend_tasks` / `has_frontend_tasks`. That way every repo created by the pipeline gets containerized.
2. **Optional: run DevOps when each worker drains**
  To match “after each respective agent is done with all tasks for their repository,” run DevOps from inside each worker when its queue is empty (right before the worker returns). That makes backend containerization happen when the backend worker finishes, and frontend when the frontend worker finishes, without depending on the other track.

Recommended: do (1) first (minimal change, fixes the bug). Optionally add (2) so containerization is clearly tied to “this agent’s work is done.”

---

## Implementation

### 1. Relax the DevOps trigger condition (orchestrator)

**File:** [software_engineering_team/orchestrator.py](software_engineering_team/orchestrator.py)

- In the DevOps block after `t_backend.join(); t_frontend.join()` (around 478–497):
  - Remove the requirement `if devops_agent and completed_code_task_ids`.
  - Run DevOps when the repo exists and is a git repo:
    - **Backend:** if `devops_agent` and `backend_dir.is_dir()` and `(backend_dir / ".git").exists()` → call `tech_lead.trigger_devops_for_backend(...)`.
    - **Frontend:** if `devops_agent` and `frontend_dir.is_dir()` and `(frontend_dir / ".git").exists()` → call `tech_lead.trigger_devops_for_frontend(...)`.
- Remove the `has_backend_tasks` and `has_frontend_tasks` variables and their use in the `if` conditions.

Effect: both backend and frontend repos get containerized whenever they were created by the pipeline, even if no tasks or all tasks failed for one of them.

### 2. (Optional) Run DevOps when each worker drains

**File:** [software_engineering_team/orchestrator.py](software_engineering_team/orchestrator.py)

- In `_backend_worker`, after the `while True` loop exits (queue empty), before the function returns:
  - If `backend_dir.is_dir()` and `(backend_dir / ".git").exists()` and `devops_agent`:
    - Call `tech_lead.trigger_devops_for_backend(devops_agent, backend_dir, architecture, spec_content, existing_pipeline=...)` (reuse the same `_read_repo_code(backend_dir, [".yml", ".yaml"])` for `existing_pipeline`).
- In `_frontend_worker`, after the `while True` loop exits:
  - If `frontend_dir.is_dir()` and `(frontend_dir / ".git").exists()` and `devops_agent`:
    - Call `tech_lead.trigger_devops_for_frontend(devops_agent, frontend_dir, architecture, spec_content, existing_pipeline=...)`.

If this is added, the existing end-of-pipeline DevOps block can be kept as a fallback (same conditions: repo exists and has `.git`) so repos that had no tasks assigned but were created by init still get containerized; the tech lead methods are idempotent (they add/update files), so double-running is acceptable. Alternatively, remove the end-of-pipeline DevOps block and rely only on the per-worker triggers; then a repo is only containerized if its worker ran at least one task (and thus created the repo), which matches “each git repo that is created by the coding agents.”

### 3. Retry path (run_failed_tasks)

**File:** [software_engineering_team/orchestrator.py](software_engineering_team/orchestrator.py)

- After the retry loop (around 1075), add a DevOps step similar to the main run: for each of `backend_dir` and `frontend_dir`, if the repo exists (dir + `.git`), call `tech_lead.trigger_devops_for_backend` / `trigger_devops_for_frontend` so that retried jobs also get both repos containerized when applicable.

---

## No changes needed

- **Tech Lead** ([tech_lead_agent/agent.py](software_engineering_team/tech_lead_agent/agent.py)): `trigger_devops_for_backend` and `trigger_devops_for_frontend` already write into the given `repo_path` with `write_agent_output(path, result, subdir="")`, so backend and frontend repos are written correctly.
- **DevOps agent** ([devops_agent/agent.py](software_engineering_team/devops_agent/agent.py), [devops_agent/prompts.py](software_engineering_team/devops_agent/prompts.py)): Already accepts `target_repo="backend"` or `"frontend"` and produces appropriate Dockerfile/CI per repo.
- **repo_writer** ([shared/repo_writer.py](software_engineering_team/shared/repo_writer.py)): `_output_to_files_dict` already maps DevOps output (pipeline_yaml, dockerfile, etc.) to paths under the given repo; no change needed.

---

## Summary


| Item                         | Action                                                                                                                                                                       |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Main run: when to run DevOps | Trigger for backend if `backend_dir` exists and has `.git`; for frontend if `frontend_dir` exists and has `.git`. Do not require `completed_code_task_ids` or `has_*_tasks`. |
| Optional                     | Run DevOps at end of each worker when its queue drains.                                                                                                                      |
| Retry run                    | After retry loop, run DevOps for each repo that exists (dir + `.git`).                                                                                                       |


This ensures the DevOps agent containerizes every git repo created by the coding agents (backend and frontend), and that it runs after the respective agent is done with all tasks for that repository (either at end of pipeline or when that worker drains).