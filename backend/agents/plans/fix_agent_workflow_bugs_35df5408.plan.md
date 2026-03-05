---
name: Fix agent workflow bugs
overview: "Fix three bugs: documentation agent branch name mismatch causing merge failures, backend agent hardcoded `python` command causing infinite retry loops, and frontend agent missing npm project initialization."
todos:
  - id: fix-doc-merge
    content: Fix documentation agent branch name mismatch in documentation_agent/agent.py -- update branch_name from create_feature_branch return value
    status: completed
  - id: fix-python-cmd
    content: Add _find_python() helper to shared/command_runner.py and replace hardcoded 'python' in run_python_syntax_check() and run_pytest()
    status: completed
  - id: fix-frontend-init
    content: Add _ensure_frontend_project_initialized() to command_runner.py and call it from orchestrator.py before frontend Phase 1
    status: completed
  - id: update-frontend-prompt
    content: Update frontend_agent/prompts.py to inform the agent that base project scaffolding exists
    status: completed
isProject: false
---

# Fix Agent Workflow Bugs

## Issue 1: Documentation Agent Merge Failure

**Root cause:** Branch name mismatch in [documentation_agent/agent.py](software_engineering_team/documentation_agent/agent.py).

On line 261, `branch_name` is set to `f"docs/{task_id}"` (e.g. `docs/backend-validation`). But `create_feature_branch` in [shared/git_utils.py](software_engineering_team/shared/git_utils.py) prepends `feature/`, creating `feature/docs/backend-validation` (line 51). The merge on line 366 then tries to merge `docs/backend-validation` -- a branch that doesn't exist.

**Fix:** After calling `create_feature_branch`, update `branch_name` to match the actual branch created. The function already returns the branch name as the second tuple element (line 70 of git_utils.py). Use it:

```python
# Line 261: set initial branch_name
branch_name = f"docs/{task_id}"

# Line 287-293: after create_feature_branch, capture the real branch name
ok, msg = create_feature_branch(path, DEVELOPMENT_BRANCH, f"docs/{task_id}")
if not ok:
    ...
    return ...
branch_name = msg  # msg contains the actual branch name (e.g. "feature/docs/backend-validation")
```

This ensures `merge_branch(path, branch_name, ...)` on line 366, `_cleanup_branch` calls, and `delete_branch` on line 378 all use the correct name.

---

## Issue 2: Backend Agent `python` Command Not Found

**Root cause:** Hardcoded `"python"` in [shared/command_runner.py](software_engineering_team/shared/command_runner.py).

- `run_python_syntax_check()` (line 241): `["python", "-m", "py_compile", str(f)]`
- `run_pytest()` (line 216): `["python", "-m", "pytest", "-v", "--tb=short"]`

On systems where only `python3` is available, `run_command` catches `FileNotFoundError` and returns "Command not found: python", but there's no fallback. This causes the backend agent to retry 20 times in a loop, each time getting the same error.

**Fix:** Add a `_find_python()` helper that probes for a working Python interpreter, trying `python` first, then `python3`. Cache the result so discovery only runs once. Use it in both functions:

```python
_cached_python: Optional[str] = None

def _find_python() -> str:
    """Return the name of an available Python interpreter, preferring 'python' then 'python3'."""
    global _cached_python
    if _cached_python is not None:
        return _cached_python
    for candidate in ("python", "python3"):
        try:
            subprocess.run([candidate, "--version"], capture_output=True, timeout=5)
            _cached_python = candidate
            return candidate
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    _cached_python = "python3"  # last-resort default
    return _cached_python
```

Then replace `"python"` on lines 216 and 241 with `_find_python()`.

---

## Issue 3: Frontend Agent Missing npm Project Initialization

**Root cause:** The frontend agent assumes an Angular project already exists. It writes files under `src/` but never creates `package.json`, `angular.json`, or installs dependencies. The orchestrator's build verification (line 348 of [orchestrator.py](software_engineering_team/orchestrator.py)) silently skips `ng build` when no `package.json` is found, masking the problem.

**Fix:** Add a project initialization step in the orchestrator's frontend workflow, before the first coding iteration. This will be placed in the orchestrator at the point where the frontend exclusive section begins (around line 620), before Phase 1.

The initialization logic should:

1. Check if `{repo_path}/frontend/package.json` exists
2. If not, run `npm init -y` in the `frontend/` directory
3. Install base Angular dependencies: `npm install @angular/core @angular/common @angular/compiler @angular/platform-browser @angular/platform-browser-dynamic @angular/router @angular/forms @angular/animations rxjs zone.js tslib`
4. Install dev dependencies: `npm install --save-dev @angular/cli @angular/compiler-cli @angular/build typescript`
5. Create a minimal `angular.json` and `tsconfig.json` if they don't exist
6. Log the initialization so it's visible in the workflow

Implementation location: Add a new helper function `_ensure_frontend_project_initialized()` in [shared/command_runner.py](software_engineering_team/shared/command_runner.py) (it already handles build commands), and call it from the orchestrator before Phase 1 of the frontend workflow. Also update the frontend prompt in [frontend_agent/prompts.py](software_engineering_team/frontend_agent/prompts.py) to mention that the base project scaffolding is already set up.