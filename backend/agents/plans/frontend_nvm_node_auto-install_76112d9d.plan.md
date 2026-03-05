---
name: Frontend NVM Node auto-install
overview: "Fix two issues from the logs: (1) backend worker NameError due to stale _all_queue_ids reference; (2) frontend init failure because NVM is present but Node 22.12 is not installed. The plan fixes the bug and adds NVM/Node auto-install with visible errors and fallbacks."
todos:
  - id: fix-backend-queue-fn
    content: In orchestrator.py _backend_worker, replace _all_queue_ids() with _remaining_queue_ids() at line ~546
    status: completed
  - id: verify-no-other-all-queue-ids
    content: Grep codebase for _all_queue_ids and confirm no other references remain
    status: completed
  - id: add-ensure-nvm-installed
    content: In command_runner.py, implement ensure_nvm_installed() - check for nvm.sh; if missing run official NVM install script (curl/wget) with timeout 120s and non-interactive env; re-check nvm.sh and return success/failure with stderr on failure
    status: completed
  - id: call-ensure-nvm-in-frontend-init
    content: In ensure_frontend_project_initialized, call ensure_nvm_installed() before use_nvm = _get_nvm_script_prefix(); keep fallback to system node/npm if NVM still missing
    status: completed
  - id: remove-dev-null-run-command-with-nvm
    content: In run_command_with_nvm, remove '2>/dev/null' from the nvm install step so install stderr is captured in CommandResult
    status: completed
  - id: remove-dev-null-ng-serve
    content: In run_ng_serve_smoke_test inline script, remove '2>/dev/null' from nvm install step
    status: completed
  - id: optional-node-install-fallback
    content: "Optional: In run_command_with_nvm, if nvm install <version> fails, try fallback version (e.g. 22 or v22.12.0) once before failing; log which version was used"
    status: completed
  - id: test-backend-worker
    content: Run orchestrator with a job that has backend tasks and confirm no NameError
    status: completed
  - id: test-frontend-init
    content: Run frontend init (or full pipeline) and confirm either Node install succeeds or a clear error is shown in logs
    status: completed
isProject: false
---

# Frontend NVM/Node/npm auto-install and orchestrator bug fix

## Problems identified from the logs

**1. Backend worker NameError (all backend tasks fail)**

- **Log:** `NameError: name '_all_queue_ids' is not defined` in `_backend_worker` at line 546.
- **Cause:** The orchestrator was updated to rename `_all_queue_ids` to `_remaining_queue_ids`, but the backend worker still calls `_all_queue_ids()` when building `remaining_ids` for the Tech Lead context.
- **Evidence:** Backend tasks backend-data-models, backend-crud-api, backend-validation all fail with "Unhandled exception: name '_all_queue_ids' is not defined".

**2. Frontend init failure (all frontend tasks fail)**

- **Log:** `Frontend init failed: N/A: version "22.12 -> N/A" is not yet installed. You need to run "nvm install 22.12" to install it before using it.`
- **Cause:** NVM is installed (so the code uses NVM), but Node 22.12 is not installed. The script runs `nvm install 22.12 --no-progress 2>/dev/null; nvm use 22.12 && <cmd>`. If the install fails or never runs successfully, `nvm use` fails with that message. Install stderr is hidden by `2>/dev/null`.
- **Evidence:** All four frontend tasks (frontend-app-shell, frontend-list-component, frontend-form-component, frontend-detail-component) fail with the same NVM version message.

---

## Fixes

### Fix 1: Correct the backend worker function name (orchestrator)

**File:** [software_engineering_team/orchestrator.py](software_engineering_team/orchestrator.py)

- **Location:** In `_backend_worker`, the line that builds `remaining_ids` for the backend workflow (around line 546).
- **Change:** Replace `_all_queue_ids()` with `_remaining_queue_ids()`.
- **Code:** `remaining_ids = set(_all_queue_ids()) - {task_id}` → `remaining_ids = set(_remaining_queue_ids()) - {task_id}`.

This is the only reference to `_all_queue_ids` left in the file; the function was renamed to `_remaining_queue_ids` and used correctly in the prefix and frontend worker. Apply Fix 1 first so backend tasks can run; then apply Fix 2 so frontend init can succeed or fail with clear errors.

### Fix 2: NVM/Node/npm (see sections below)

Address why Node 22.12 is not installed and make the system able to install NVM and Node when missing. Details in the rest of the plan.

---

## Why the frontend is failing (NVM/Node)

From the terminal output and code:

1. **NVM is present** – `[_get_nvm_script_prefix()](software_engineering_team/shared/command_runner.py)` finds `~/.nvm/nvm.sh` (or `NVM_DIR`), so the frontend path uses NVM.
2. **Node 22.12 is not installed** – The message `version "22.12 -> N/A" is not yet installed` comes from `nvm use 22.12` when that version does not exist.
3. **Install errors are hidden** – In `[run_command_with_nvm](software_engineering_team/shared/command_runner.py)` the script is:
  - `nvm install 22.12 --no-progress 2>/dev/null; nvm use 22.12 && <cmd>`
  - So if `nvm install 22.12` fails (network, mirror, or version resolution), stderr is discarded and the next command (`nvm use`) fails with the message you see.

So the “coding agent” (orchestrator + command_runner) does **not** install NVM or Node today; it only **uses** NVM if it is already there, and tries (silently) to install a Node version that may never succeed.

## Goal

Make the system able to:

1. **Install NVM** when it is not installed.
2. **Install the required Node version** (and thus npm) via NVM when missing.
3. Surface failures instead of hiding them, and optionally fall back to another Node version.

All of this should happen in the same process/orchestrator run (no human steps).

---

## 1. Auto-install NVM when missing

**File:** [software_engineering_team/shared/command_runner.py](software_engineering_team/shared/command_runner.py)

- Add a function `**ensure_nvm_installed()**` that:
  - If `_get_nvm_script_prefix()` is not `None`, return success (NVM already there).
  - Otherwise run the official NVM install script in a subprocess, e.g.:
    - `curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.4/install.sh | bash`
    - Or use `wget` if `curl` is not available (the install script supports both).
  - Use a timeout (e.g. 120s) and capture stdout/stderr.
  - Set env so the script is non-interactive (e.g. `PROFILE=/dev/null` or unset `PROFILE` so it doesn’t require a login shell).
  - After a successful run, check again for `~/.nvm/nvm.sh` (or `NVM_DIR`); if it exists, return success.
  - Return a small result type or raise so callers can tell success from failure (and log stderr on failure).
- **Where to call it:** From `**ensure_frontend_project_initialized**` (and optionally from `run_command_with_nvm` when NVM is missing): before any NVM use, call `ensure_nvm_installed()`. If it fails, fall back to system `node`/`npm` (current behavior when NVM is not found) and log that frontend may need a specific Node version.

---

## 2. Stop hiding `nvm install` errors and make Node install reliable

**File:** [software_engineering_team/shared/command_runner.py](software_engineering_team/shared/command_runner.py)

- In `**run_command_with_nvm**` (and the inline script in `**run_ng_serve_smoke_test**`):
  - **Remove `2>/dev/null**` from the `nvm install` step so install failures appear in `CommandResult.stderr` and logs.
  - Optionally split into two steps: first run `nvm install <version>` and check exit code; if it fails, try a fallback (e.g. `nvm install 22` or `nvm install --lts`) once, then run `nvm use <version>` and the actual command. This way the agent/user sees “install failed” or “fallback to 22 succeeded”.
  - Consider using an explicit version string that NVM accepts (e.g. `**v22.12.0**` or `**22**`) if `22.12` keeps resolving to N/A on some systems; keep `ANGULAR_NODE_VERSION` as the default but allow a fallback list.
- Ensure the combined script still does: source NVM → install Node (no stderr hiding) → use Node → run command. Capture full stderr in `CommandResult` so the orchestrator can log “Frontend init failed: ”.

---

## 3. Wire NVM ensure into frontend init

**File:** [software_engineering_team/shared/command_runner.py](software_engineering_team/shared/command_runner.py)

- In `**ensure_frontend_project_initialized**`:
  - Before the “use_nvm = _get_nvm_script_prefix() is not None” block, call `**ensure_nvm_installed()**`.
  - If NVM was just installed (or was already there), **then** set `use_nvm = _get_nvm_script_prefix() is not None` and proceed with `run_command_with_nvm` for npm init and installs.
  - If NVM is still not available after ensure (e.g. install failed or no curl/wget), keep current fallback: use system `node`/`npm` and log that the required Node version may not be met.

No change to the frontend **coding agent** (LLM) itself; the **environment** (command_runner + orchestrator) gains the ability to install NVM and Node.

---

## 4. npm

npm is included with Node when installed via NVM, so no separate “install npm” step is needed. Once NVM and the desired Node version are installed, `npm` is available in that environment.

---

## 6. Detailed todo tasks

Execute in this order to ensure all required changes are made correctly.

**Fix 1 – Orchestrator (do first)**

1. **fix-backend-queue-fn** – In [orchestrator.py](software_engineering_team/orchestrator.py), inside `_backend_worker`, find the line `remaining_ids = set(_all_queue_ids()) - {task_id}` (around line 546) and change it to `remaining_ids = set(_remaining_queue_ids()) - {task_id}`. Save the file.
2. **verify-no-other-all-queue-ids** – Run `grep -r "_all_queue_ids"` in the software_engineering_team directory and confirm no other references exist. If any remain, replace them with `_remaining_queue_ids`.

**Fix 2 – NVM/Node in command_runner.py**

1. **add-ensure-nvm-installed** – In [shared/command_runner.py](software_engineering_team/shared/command_runner.py), add a new function `ensure_nvm_installed()` that: (a) returns immediately with success if `_get_nvm_script_prefix()` is not None; (b) otherwise runs the official NVM install script (`curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.4/install.sh | bash` or wget equivalent) in a subprocess with timeout 120s, non-interactive env (e.g. `PROFILE=/dev/null` or unset), and capture stdout/stderr; (c) after the run, checks again for `~/.nvm/nvm.sh` (or `NVM_DIR`); (d) returns a result indicating success or failure, including stderr on failure so callers can log it.
2. **call-ensure-nvm-in-frontend-init** – In `ensure_frontend_project_initialized`, before the block that sets `use_nvm = _get_nvm_script_prefix() is not None`, call `ensure_nvm_installed()`. Then set `use_nvm = _get_nvm_script_prefix() is not None`. If NVM is still not available after ensure, keep the existing fallback (use system node/npm) and log that the required Node version may not be met.
3. **remove-dev-null-run-command-with-nvm** – In `run_command_with_nvm`, in the script string that runs `nvm install ...`, remove the `2>/dev/null` so that install stderr is not discarded. Ensure the script still captures combined stderr in the subprocess result so it appears in `CommandResult.stderr`.
4. **remove-dev-null-ng-serve** – In `run_ng_serve_smoke_test`, in the inline bash script that runs `nvm install ...`, remove the `2>/dev/null` from the install step so install failures are visible.
5. **optional-node-install-fallback** – (Optional) In `run_command_with_nvm`, if desired: run `nvm install <version>` first and check exit code; if non-zero, try one fallback version (e.g. `22` or `v22.12.0`) before running `nvm use` and the command. Log which Node version was used so debugging is easier.

**Verification**

1. **test-backend-worker** – Run the API (e.g. `python3 agent_implementations/run_api_server.py`) and trigger a run-team job that includes backend tasks. Confirm in logs that backend tasks complete without `NameError: name '_all_queue_ids' is not defined`.
2. **test-frontend-init** – Run a job that includes frontend tasks. Confirm either (a) Node 22.12 is installed and frontend init succeeds, or (b) a clear error message appears in logs (no silent failure), and if NVM was missing, `ensure_nvm_installed()` was attempted and its failure is logged.

---

## 5. Summary of code touchpoints


| Area                                                                           | Change                                                                                                                                                                                                                                                                         |
| ------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| [orchestrator.py](software_engineering_team/orchestrator.py)                   | **Fix 1 (bug):** In `_backend_worker`, line ~546, change `_all_queue_ids()` to `_remaining_queue_ids()` so backend tasks no longer raise NameError.                                                                                                                            |
| [shared/command_runner.py](software_engineering_team/shared/command_runner.py) | **Fix 2 (NVM/Node):** Add `ensure_nvm_installed()`; in `run_command_with_nvm` (and ng serve script) remove `2>/dev/null`, optionally add Node install fallback and/or use `v22.12.0`; in `ensure_frontend_project_initialized` call `ensure_nvm_installed()` before using NVM. |


---

## Risks and notes

- **Permissions**: NVM install writes to `$HOME/.nvm` and possibly `~/.bashrc` / `~/.profile`. The process running the orchestrator must be able to write there.
- **Network**: Install requires outbound HTTPS (curl/wget to GitHub). In air-gapped or restricted environments, NVM/Node install will fail; fallback to system Node remains.
- **Version**: If `22.12` is not available on all mirrors, using `v22.12.0` or `22` (latest v22) as fallback can improve reliability.

