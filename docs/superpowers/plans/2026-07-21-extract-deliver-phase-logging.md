# Extract Deliver-Phase + Logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract the backend/frontend deliver-phase + final logging blocks into `BaseV2DevelopmentAgent._run_deliver_and_finalize`, parameterized by `_TEAM_LABEL` and `_DELIVER_IN_PROGRESS_STATUS`.

**Architecture:** Mirror `_run_planning_and_branch_setup`: staticmethod on the base, inject `run_deliver`, mutate `result`, return optional failure reason. Subclasses set two class attributes and replace the inline block with one call.

**Tech Stack:** Python 3.10, pytest, existing `BaseV2DevelopmentAgent` helpers in `shared/v2_orchestrator.py`.

## Global Constraints

- Observable status/log text must match today for both teams (including frontend ellipsis).
- Never reference GitHub issue numbers in code, comments, or commit messages.
- DbC docstrings with Preconditions / Postconditions on the new helper.
- 90% coverage floor on touched files; `make test` / `make lint` from `backend/`.

## File Structure

| File | Responsibility |
|---|---|
| `shared/v2_orchestrator.py` | New `_run_deliver_and_finalize` helper |
| `backend_code_v2_team/orchestrator.py` | Class attrs + call site |
| `frontend_code_v2_team/orchestrator.py` | Class attrs + call site |
| `tests/test_v2_orchestrator_helpers.py` | Unit tests for the helper |

---

### Task 1: Failing unit tests for `_run_deliver_and_finalize`

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_v2_orchestrator_helpers.py`

- [ ] **Step 1: Write failing tests**

Add `TestRunDeliverAndFinalize` covering:

1. Success path with `merge_to_development=True` / `merged=True` / `failed_count=0` → `result.success`, final status `"{label} task complete"`, progress 100
2. Partial path (`failed_count > 0`) → `needs_followup`, `"…completed with issues"`, progress 95
3. Deliver exception → failure reason returned, early exit (no final success update)
4. In-progress status uses the passed `deliver_in_progress_status` verbatim
5. Branch-ready path when `merge_to_development=False` uses `branch_ready`

Use a tiny mutable fake result object and injected `run_deliver` / `update_job` / logger, same style as `TestRunPlanningAndBranchSetup`.

Because the helper reads class attrs, either:

- pass `team_label` / `deliver_in_progress_status` as explicit kwargs (preferred if we design the helper that way for testability while subclasses pass `cls._TEAM_LABEL`), **or**
- call via a tiny test subclass that sets the attrs

Prefer explicit kwargs for the strings (subclasses pass `type(self)._TEAM_LABEL`) so unit tests stay attribute-free — matching how `emit_branch_ready_progress` is parameterized on preflight. Document that subclasses own the class attrs and pass them in.

- [ ] **Step 2: Run tests — expect fail**

```bash
cd backend && python -m pytest agents/software_engineering_team/tests/test_v2_orchestrator_helpers.py::TestRunDeliverAndFinalize -q
```

Expected: import/AttributeError — helper missing.

- [ ] **Step 3: Commit tests** (only if user asked to commit)

---

### Task 2: Implement helper on `BaseV2DevelopmentAgent`

**Files:**
- Modify: `backend/agents/software_engineering_team/shared/v2_orchestrator.py`

- [ ] **Step 1: Implement `_run_deliver_and_finalize`**

Signature sketch:

```python
@staticmethod
def _run_deliver_and_finalize(
    *,
    task_id: str,
    repo_path: Path,
    current_files: set,
    exec_summary: str,
    task_title: str,
    task_description: str,
    tool_agents: Dict[Any, Any],
    feature_branch_name: Optional[str],
    merge_to_development: bool,
    failed_count: int,
    completed_count: int,
    start_time: float,
    result: Any,
    run_deliver: Callable[..., Any],
    update_job: Callable[..., None],
    logger: logging.Logger,
    team_label: str,
    deliver_in_progress_status: str,
) -> Optional[str]:
```

Body mirrors today's backend block (order: set phase → progress 90 → try deliver → mutate result → elapsed → final_status → update_job → log). Frontend's reorder of `elapsed` vs `_update_job` is not load-bearing; pick backend order.

Import `Phase` from `software_engineering_team.shared.v2_models` inside the method or at module top — prefer module top if no cycle; otherwise local import.

Update the module docstring to mention deliver+finalize is now shared.

- [ ] **Step 2: Run helper tests — expect pass**

```bash
cd backend && python -m pytest agents/software_engineering_team/tests/test_v2_orchestrator_helpers.py::TestRunDeliverAndFinalize -q
```

---

### Task 3: Wire both DevelopmentAgents

**Files:**
- Modify: `backend/agents/software_engineering_team/backend_code_v2_team/orchestrator.py`
- Modify: `backend/agents/software_engineering_team/frontend_code_v2_team/orchestrator.py`

- [ ] **Step 1: Backend**

Add:

```python
_TEAM_LABEL = "Backend"
_DELIVER_IN_PROGRESS_STATUS = "Committing changes and preparing delivery"
```

Replace the deliver+finalize block with a call that passes `run_deliver=run_deliver`, `team_label=self._TEAM_LABEL` (or `type(self)._TEAM_LABEL`), etc. On non-`None` failure reason, `return result`.

- [ ] **Step 2: Frontend**

Same with Frontend strings (including `"..."`).

- [ ] **Step 3: Regression tests**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_v2_orchestrator_helpers.py \
  agents/software_engineering_team/tests/test_backend_code_v2_team.py \
  agents/software_engineering_team/tests/test_frontend_code_v2_team.py -q
```

- [ ] **Step 4: Full gate**

```bash
cd backend && make lint && make test
```

---

### Task 4: Done

- [ ] Report worktree path, summary of changes, test evidence.
- [ ] Do not commit unless the user asks.
