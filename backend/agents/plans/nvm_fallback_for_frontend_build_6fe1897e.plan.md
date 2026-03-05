---
name: NVM fallback for frontend build
overview: When frontend build verification fails due to Node.js version (e.g. Angular CLI requires v20.19+ but system has v18), use NVM to install and use a supported Node version and retry the build instead of failing the task.
todos: []
isProject: false
---

# NVM fallback for frontend build verification

## Problem

Frontend tasks fail at Phase 2 (Build verification) with:

```text
Unsupported environment: Node.js version v18.17.1 detected.
The Angular CLI requires a minimum Node.js version of v20.19 or v22.12.
```

The orchestrator already detects this via `[is_ng_build_environment_failure()](software_engineering_team/shared/command_runner.py)` and marks the task as failed with an "ENV:" prefix. The desired behavior is: when this happens, use NVM to install/use a supported Node version and retry the build so the branch can be merged.

## Approach

- **Single responsibility**: Keep NVM logic in the command runner; the orchestrator continues to call “run frontend build verification” and only sees success/failure and error text.
- **Fallback only**: Run the normal `ng build` first. If it fails and the failure is an environment (Node) failure, retry the build in a shell that has NVM loaded and a supported Node version active. If NVM is not available or the retry fails, preserve current behavior (task fails with the same style of message).
- **Version**: Use Node 20 (e.g. `nvm install 20` / `nvm use 20`) so we satisfy Angular’s “v20.19 or v22.12” requirement without changing behavior for users who already have Node 22.

## Implementation

### 1. [shared/command_runner.py](software_engineering_team/shared/command_runner.py)

- **NVM detection**
  - Add a helper (e.g. `_get_nvm_script_prefix()`) that returns a shell fragment to source NVM, or `None` if NVM is not found.
  - Resolve NVM from `os.environ.get("NVM_DIR")` or `Path.home() / ".nvm"`, and check that `nvm.sh` exists under that path. If not, return `None` (no NVM fallback).
- **Run command under NVM**
  - Add `run_command_with_nvm(cmd, cwd, node_version="20", timeout=BUILD_TIMEOUT)` that:
    - Builds a one-liner: `source "<NVM_DIR>/nvm.sh" && nvm install <node_version> --no-progress 2>/dev/null; nvm use <node_version> && <cmd>`.
    - Runs it with `subprocess.run(["bash", "-c", script], cwd=cwd, capture_output=True, text=True, timeout=timeout, env=os.environ.copy())`.
    - Returns a `CommandResult` in the same shape as `run_command()` (success, exit_code, stdout, stderr, timed_out).
- **Ng build with NVM fallback**
  - Add `run_ng_build_with_nvm_fallback(project_path: str | Path) -> CommandResult`:
    1. Call existing `run_ng_build(project_path)`.
    2. If `result.success`, return `result`.
    3. If not, and `not is_ng_build_environment_failure(result)`, return `result` (real build error; no NVM retry).
    4. If NVM prefix is `None`, return the original `result` (same ENV failure as today).
    5. Otherwise call `run_command_with_nvm(["npx", "ng", "build", "--configuration=development"], cwd=project_path)` and return its result.
- **Logging**
  - Log when falling back to NVM and when NVM is not available so operators can see why a task failed or succeeded.

### 2. [orchestrator.py](software_engineering_team/orchestrator.py)

- In `_run_build_verification()`, for the frontend branch:
  - Replace the call to `run_ng_build(frontend_dir)` with `run_ng_build_with_nvm_fallback(frontend_dir)`.
  - Keep the rest unchanged: still use `is_ng_build_environment_failure(result)` on the returned result to set the `ENV:` prefix and failure reason when the (possibly NVM-retried) build still fails.

No changes to the frontend agent itself, task assignment, or merge logic—only how the single “run ng build” step is executed and when we retry under NVM.

## Flow (mermaid)

```mermaid
sequenceDiagram
  participant Orch as Orchestrator
  participant CR as command_runner
  participant NVM as NVM shell

  Orch->>CR: run_ng_build_with_nvm_fallback(frontend_dir)
  CR->>CR: run_ng_build()
  alt build success
    CR-->>Orch: CommandResult(success=True)
  else build failed
    CR->>CR: is_ng_build_environment_failure?
    alt not env failure
      CR-->>Orch: CommandResult(success=False, build errors)
    else env failure (Node version)
      alt NVM not found
        CR-->>Orch: CommandResult(success=False, ENV message)
      else NVM available
        CR->>NVM: bash -c "source nvm; nvm use 20; npx ng build ..."
        NVM-->>CR: build result
        CR-->>Orch: CommandResult from NVM build
      end
    end
  end
```



## Edge cases

- **NVM not installed**: No script prefix; return original ENV failure (same user-visible behavior as now).
- **NVM install 20 fails** (e.g. network): Retry build will fail; we return that result and orchestrator will still treat it as build failure; if the stderr still contains Node version text, `is_ng_build_environment_failure` may still be true and we’d still report an ENV-style message, which is acceptable.
- **Project path with special characters**: Use `cwd` in `subprocess.run` only; avoid embedding path in the shell string for the “ng build” part by running the build as a single `bash -c` that only sources NVM and runs `nvm use`; the actual command is run with `cwd=project_path` so the shell does not need to `cd` into a user path.

## Optional follow-up

- **ng serve smoke test**: If the team runs `run_ng_serve_smoke_test` in environments where Node may be old, the same NVM fallback could be added there later (same pattern: run once, on Node version–style failure retry with NVM).
- **Frontend project init**: `ensure_frontend_project_initialized` runs `npm install`; it could be extended to use NVM for install if needed, but current failures are from `ng build`, so out of scope for this change.

