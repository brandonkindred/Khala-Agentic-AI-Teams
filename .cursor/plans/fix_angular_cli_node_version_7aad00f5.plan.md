---
name: Fix Angular CLI Node version
overview: The frontend build sees Node v18.17.1 instead of v22.12 because the NVM script in command_runner runs `nvm use` inside a subshell, so PATH never updates in the shell that runs `npx ng build`. Fix by running nvm install/use in the same shell (braces instead of parentheses), then harden with version checks and .nvmrc so the issue cannot recur.
todos:
  - id: fix-subshell-run-command-with-nvm
    content: In run_command_with_nvm, replace ( ) with { }; so nvm use runs in same shell
    status: completed
  - id: fail-when-nvm-missing
    content: In run_ng_build_with_nvm_fallback, return explicit failure when NVM not found
    status: completed
  - id: add-nvmrc-frontend-init
    content: In ensure_frontend_project_initialized, write .nvmrc with ANGULAR_NODE_VERSION
    status: completed
  - id: optional-verify-node-version
    content: (Optional) In run_command_with_nvm, verify node --version after nvm use
    status: completed
  - id: optional-ng-serve-fallback
    content: (Optional) In run_ng_serve_smoke_test, add 22 fallback with same-shell braces
    status: completed
  - id: optional-frontend-prompts
    content: (Optional) In frontend_agent prompts, mention Node 22.12+ / NVM / .nvmrc
    status: completed
isProject: false
---

# Fix Angular CLI Node version (v18.17.1 vs v22.12)

## Root cause

The terminal shows:

- "Node.js version v18.17.1 detected" when `ng build` runs
- "The Angular CLI requires a minimum Node.js version of v20.19 or v22.12"
- "v22.22.0 is already installed" (likely from nvm when trying the fallback)

In [software_engineering_team/shared/command_runner.py](software_engineering_team/shared/command_runner.py), `run_command_with_nvm` (lines 266-271) builds a script like:

```bash
source "/path/to/nvm.sh" && (nvm install 22.12 --no-progress && nvm use 22.12) || (nvm install 22 --no-progress && nvm use 22) && npx ng build --configuration=development
```

In Bash, **parentheses `( ... )` create a subshell**. So:

1. `nvm use 22.12` or `nvm use 22` runs inside a subshell and updates PATH only in that subshell.
2. When that subshell exits, the parent shell continues with its **original** PATH (e.g. system Node v18.17.1).
3. `npx ng build` runs in the parent shell and therefore uses Node v18.17.1. Angular CLI then reports the version error.

The NVM switch never affects the process that runs `ng build`. This is a well-known Bash gotcha: variable and environment changes in a subshell do not persist.

## Fix strategy

1. **Run nvm install/use in the same shell** so PATH is set before `npx` runs (use braces `{ }` instead of parentheses).
2. **When NVM is missing**, fail with a clear requirement instead of falling back to system Node (which is often too old).
3. **Pin version in repos** with `.nvmrc` so `nvm use` in the project directory uses the correct version.
4. **(Optional)** Verify Node version after `nvm use` and before running the command; fail fast with a clear message if below minimum.

---

## Task 1: Fix subshell bug in `run_command_with_nvm`

**File:** [software_engineering_team/shared/command_runner.py](software_engineering_team/shared/command_runner.py)

**Location:** Lines 266-271, inside `run_command_with_nvm`.

**Current code (broken):**

```python
script = (
    f"{nvm_prefix} && "
    f"(nvm install {node_version} --no-progress && nvm use {node_version}) || "
    f"(nvm install {NVM_NODE_FALLBACK_VERSION} --no-progress && nvm use {NVM_NODE_FALLBACK_VERSION}) && "
    f"{shlex.join(cmd)}"
)
```

**Required change:** Use Bash **braces** `{ ... }` instead of parentheses so the group runs in the **current** shell. In Bash, a semicolon (or newline) is required before the closing `}`. In Python f-strings, literal `{` and `}` must be escaped as `{{` and `}}` so that `{node_version}` and `{NVM_NODE_FALLBACK_VERSION}` are still interpolated.

**Replace with:**

```python
script = (
    f"{nvm_prefix} && "
    f"{{ nvm install {node_version} --no-progress && nvm use {node_version}; }} || "
    f"{{ nvm install {NVM_NODE_FALLBACK_VERSION} --no-progress && nvm use {NVM_NODE_FALLBACK_VERSION}; }} && "
    f"{shlex.join(cmd)}"
)
```

**Resulting Bash script (conceptually):** `source "..." && { nvm install 22.12 ... && nvm use 22.12; } || { nvm install 22 ... && nvm use 22; } && npx ng build ...` so `nvm use` runs in the same shell as `npx ng build`.

**Verification:** After the change, run a frontend build in a repo that has NVM and system Node &lt; 22; the build should use Node 22.12 or 22.x and succeed (or fail only on code, not on "Node.js version v18.17.1 detected").

---

## Task 2: Fail when NVM is missing instead of using system Node

**File:** [software_engineering_team/shared/command_runner.py](software_engineering_team/shared/command_runner.py)

**Location:** `run_ng_build_with_nvm_fallback` (lines 322-336).

**Current behavior:** When `_get_nvm_script_prefix()` is `None`, the code calls `run_ng_build(project_path)`, which uses whatever is on `PATH` (often Node v18). That leads to the "v18.17.1 detected" error from Angular.

**Required change:** When NVM is not found, do **not** call `run_ng_build`. Return a `CommandResult` with `success=False` and a clear, actionable stderr.

**Implementation steps:**

1. In the `else` branch (when `_get_nvm_script_prefix() is None`), build an error message string, e.g. `"NVM not found. Angular CLI requires Node v20.19+ or v22.12+. Install NVM (https://github.com/nvm-sh/nvm) and run: nvm install 22.12"`.
2. (Recommended) Optionally run `subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=5)` (or use `shutil.which("node")` and then run that node's `--version`). If available, append to the message, e.g. `" System Node is v18.17.1."`.
3. Return `CommandResult(success=False, exit_code=-1, stdout="", stderr=message)`.
4. Remove the line `return run_ng_build(project_path)` for the no-NVM case.

**Rationale:** The pipeline should never silently use an unsupported Node; it should fail once with a clear, actionable error so operators install NVM and the correct Node version.

---

## Task 3: Add `.nvmrc` for frontend projects

**File:** [software_engineering_team/shared/command_runner.py](software_engineering_team/shared/command_runner.py)

**Location:** `ensure_frontend_project_initialized`, after writing scaffold files and before the final `logger.info("Frontend project initialized successfully ...")` and `return` (around lines 747-748).

**Required change:** Write a `.nvmrc` file in the project root so that anyone running `nvm use` in that directory gets the correct Node version.

**Implementation steps:**

1. After Step 5 (writing scaffold files), e.g. after `_write_if_missing(app / "app.routes.ts", _MINIMAL_APP_ROUTES_TS)`.
2. Call `_write_if_missing(cwd / ".nvmrc", ANGULAR_NODE_VERSION + "\n")` so the file contains `22.12` (or whatever `ANGULAR_NODE_VERSION` is). Use `_write_if_missing` so existing projects are not overwritten.
3. If `_write_if_missing` is not suitable for a single-line file, use the same pattern as elsewhere: `if not (cwd / ".nvmrc").exists(): (cwd / ".nvmrc").write_text(ANGULAR_NODE_VERSION + "\n", encoding="utf-8")` and log if desired.

**Rationale:** Pinning the version in the repo prevents "wrong Node" in manual and automated use and documents the requirement for the frontend.

---

## Task 4 (Optional): Verify Node version after `nvm use`

**File:** [software_engineering_team/shared/command_runner.py](software_engineering_team/shared/command_runner.py)

**Location:** Inside `run_command_with_nvm`, after the nvm install/use block and before running the user command.

**Idea:** After the `{ ... } || { ... }` block, run `node --version` in the same script and parse the output (e.g. strip `v`, split by `.`, compare major/minor to 20.19 or 22.12). If below minimum, exit the script with a non-zero code and a clear message (e.g. `echo "Node version X.Y.Z is below Angular CLI minimum v20.19 / v22.12" >&2; exit 1`) so the subprocess returns failure and you can return a `CommandResult` with that stderr. This makes environment failures explicit instead of letting Angular report "v18.17.1 detected".

**Implementation sketch:** Extend the `script` string to include something like `&& node --version | ...` or a small inline check. Keep it simple; Task 1 (braces) is the critical fix.

---

## Task 5 (Optional): Add version fallback to `run_ng_serve_smoke_test`

**File:** [software_engineering_team/shared/command_runner.py](software_engineering_team/shared/command_runner.py)

**Location:** Lines 353-356 in `run_ng_serve_smoke_test`.

**Current code:** There are no parentheses here; `nvm use` and `npx ng serve` run in the same shell, so this path is already correct. Optionally add the same fallback (try 22.12, then 22) using braces for consistency: e.g. `{ nvm install 22.12 ... && nvm use 22.12; } || { nvm install 22 ... && nvm use 22; }; npx ng serve ...`.

---

## Task 6 (Optional): Frontend agent / docs

- **Prompts:** In [software_engineering_team/frontend_agent/prompts.py](software_engineering_team/frontend_agent/prompts.py), near the line that says code must compile with `ng build`, add a note that the frontend requires Node v20.19+ or v22.12+ (e.g. use NVM and `.nvmrc` in the project).
- **README:** In [software_engineering_team/README.md](software_engineering_team/README.md) (if it exists), add one line that frontend builds require NVM and Node 22.12+ (or document the version and that the pipeline uses NVM).

---

## Summary of code changes (no tables)

- **[shared/command_runner.py](software_engineering_team/shared/command_runner.py) – `run_command_with_nvm`:** Replace the script that uses `( ... )` with one that uses `{ ... ; }` so nvm install/use run in the same shell as the command. Use double braces `{{` and `}}` in f-strings for literal `{` and `}`.
- **[shared/command_runner.py](software_engineering_team/shared/command_runner.py) – `run_ng_build_with_nvm_fallback`:** When NVM is not found, return an explicit failure `CommandResult` with a clear stderr instead of calling `run_ng_build`. Optionally include system `node --version` in the message.
- **[shared/command_runner.py](software_engineering_team/shared/command_runner.py) – `ensure_frontend_project_initialized`:** After creating the project (after writing scaffold files), write `.nvmrc` with content `ANGULAR_NODE_VERSION` (e.g. `22.12`) so the project pins the Node version.
- **(Optional)** **[shared/command_runner.py](software_engineering_team/shared/command_runner.py):** After nvm use in `run_command_with_nvm`, verify `node --version` and fail with a clear message if below 20.19 / 22.12.
- **(Optional)** **[shared/command_runner.py](software_engineering_team/shared/command_runner.py) – `run_ng_serve_smoke_test`:** Add the same 22.12/22 fallback using braces for consistency.
- **(Optional)** **[frontend_agent/prompts.py](software_engineering_team/frontend_agent/prompts.py)** and **README:** Document Node 22.12+ and NVM for frontend builds.

No changes to the orchestrator or frontend agent logic are required; the fix is in how the shell script is built and how the fallback behaves when NVM is missing.

---

## Verification

1. **Unit / manual test (NVM available):** In a environment with NVM and system Node v18, run the orchestrator (or a test that calls `run_ng_build_with_nvm_fallback` on a frontend repo). The build should use Node 22.12 or 22.x and not report "Node.js version v18.17.1 detected".
2. **No NVM:** In an environment without NVM (or with `NVM_DIR` unset and `~/.nvm/nvm.sh` missing), run the same; the pipeline should fail with the new explicit message (e.g. "NVM not found. Angular CLI requires Node v20.19+ or v22.12+ ...") instead of failing later with Angular’s version message.
3. **New frontend project:** After `ensure_frontend_project_initialized` runs, the project root should contain a `.nvmrc` file with `22.12` (or the current `ANGULAR_NODE_VERSION`).

---

## Flow after fix

```mermaid
sequenceDiagram
    participant O as Orchestrator
    participant CR as command_runner
    participant Bash as bash_c
    participant NVM as nvm
    participant NG as npx_ng_build

    O->>CR: run_ng_build_with_nvm_fallback(frontend_dir)
    alt NVM not found
        CR->>O: CommandResult(success=False, stderr="NVM not found; Node 22.12+ required...")
    else NVM found
        CR->>Bash: subprocess.run(["bash", "-c", script])
        Bash->>NVM: source nvm.sh
        Bash->>NVM: brace group nvm install 22.12 and nvm use 22.12 in same shell
        NVM->>Bash: PATH updated in this shell
        Bash->>NG: npx ng build (inherits PATH with Node 22.12)
        NG->>Bash: success
        Bash->>CR: CommandResult(success=True, ...)
        CR->>O: build passed
    end
```

Note: In the diagram, node names use underscores (e.g. `bash_c`, `npx_ng_build`) to satisfy Mermaid syntax rules that disallow spaces in node IDs.
