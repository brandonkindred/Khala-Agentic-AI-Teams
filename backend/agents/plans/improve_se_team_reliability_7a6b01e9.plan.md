---
name: Improve SE team reliability
overview: "Address the root causes of the recurring errors and warnings seen in the orchestrator logs: backend pytest running without project dependencies, LLM non-JSON responses producing no files, frontend build failures (formGroup, typos), and ensure the same Python is used for pip and pytest."
todos:
  - id: backend-pip-before-pytest
    content: In orchestrator _run_build_verification (backend), run pip install -r requirements.txt with sys.executable in backend_dir before run_pytest; treat pip failure as non-fatal (log warning).
    status: completed
  - id: run-pytest-python-exe
    content: Add optional python_exe parameter to run_pytest in command_runner; use it instead of _find_python() when provided. In _run_build_verification call run_pytest(backend_dir, python_exe=sys.executable).
    status: completed
  - id: content-fallback-backend
    content: In backend agent, when data.get('files') is empty and data.get('content') is non-empty, parse markdown code blocks for path+body and build a files dict before validation.
    status: completed
  - id: content-fallback-frontend
    content: In frontend agent, when data.get('files') is empty and data.get('content') is non-empty, parse markdown code blocks for path+body and build a files dict before validation.
    status: completed
  - id: llm-json-strip-noise
    content: (Optional) In llm _extract_json, on first parse failure try stripping leading/trailing noise (e.g. 'Here is the JSON:') and retry once before falling back to {content}.
    status: completed
  - id: prompt-json-only
    content: "Add one line to backend and frontend prompts: respond with only a single JSON object, no text before or after."
    status: completed
  - id: formgroup-prompt
    content: "In frontend prompts.py add bullet: when using formGroup/formControlName/formArrayName, component must import ReactiveFormsModule (standalone) or NgModule must import it."
    status: completed
  - id: repair-reactive-forms
    content: In command_runner add repair that scans component .html for formGroup/formControlName and ensures corresponding .ts imports ReactiveFormsModule; call from ensure_frontend_dependencies_installed.
    status: completed
  - id: normalize-double-at-angular
    content: Add repair or post-process that normalizes @@angular to @angular in frontend .ts (and optionally .html) files; run before or during frontend build/deps step.
    status: completed
  - id: prompt-template-property-names
    content: "In frontend prompts add line: template bindings and property names must exactly match the component class (no typos)."
    status: completed
  - id: allow-empty-init-py
    content: In backend _validate_file_paths allow known boilerplate paths (e.g. tests/__init__.py, **/__init__.py) with empty content by auto-setting minimal content before validation.
    status: completed
isProject: false
---

# Plan: Reduce software engineering team errors and warnings

From the logs you shared, failures cluster into a few clear causes. Below are the root causes and concrete code changes to make them rare or non-existent.

---

## 1. Backend: `ModuleNotFoundError: No module named 'sqlalchemy'` (and pytest import errors)

**What’s happening**

- When a **backend task starts**, the orchestrator calls `ensure_backend_project_initialized(backend_dir)`, which runs `pip install -r requirements.txt` **once** using `sys.executable` ([command_runner.py](software_engineering_team/shared/command_runner.py) around 1170–1176).
- The **agent then writes** new code and often **updates** `requirements.txt` (e.g. adds `sqlalchemy`). No second `pip install` runs.
- **Build verification** runs `run_python_syntax_check` and `run_pytest` using `_find_python()` ([command_runner.py](software_engineering_team/shared/command_runner.py) 631–654, 656–673), which resolves to `"python"` or `"python3"` on `PATH` and may not be the same interpreter that ran `pip install`, and in any case the **new** deps from the updated `requirements.txt` were never installed.

So pytest runs in an environment where the agent-added dependencies (e.g. sqlalchemy) are not installed, and the agent cannot fix that by changing code — hence “Build failed 3 times with the same error”.

**Changes**

- **Install backend deps immediately before pytest**  
In [orchestrator.py](software_engineering_team/orchestrator.py) `_run_build_verification` for `agent_type == "backend"`, before calling `run_pytest(backend_dir)`:
  - If `backend_dir / "requirements.txt"` exists, run `sys.executable -m pip install -r requirements.txt` with `cwd=backend_dir` (same as in `ensure_backend_project_initialized`). Use a short timeout and treat pip failure as non-fatal (log warning) so a broken requirements.txt doesn’t block the run; pytest will still run and may pass for tests that don’t need the new deps.
- **Use the same Python for pip and pytest**  
  - In [command_runner.py](software_engineering_team/shared/command_runner.py), add an optional parameter to `run_pytest(project_path, test_path="", python_exe=None)`. When `python_exe` is provided, use it instead of `_find_python()`.
  - In `_run_build_verification` (backend branch), after the pip install step above, call `run_pytest(backend_dir, python_exe=sys.executable)` so the same interpreter that just got the updated deps is used for pytest.

This removes the vast majority of “ModuleNotFoundError” and “Build failed 3 times with the same error” cases caused by missing deps.

---

## 2. “Could not parse structured JSON from LLM response” → “produced no files and no code”

**What’s happening**

- When the model returns non-JSON or malformed JSON, [OllamaLLMClient._extract_json](software_engineering_team/shared/llm.py) (502–524) falls back to `{"content": text.strip()}` and logs “Could not parse structured JSON from LLM response; returning raw content wrapper”.
- Backend and frontend agents only use `data.get("files", {})` and `data.get("code")` ([backend_agent/agent.py](software_engineering_team/backend_agent/agent.py) 1017–1025, [frontend_agent/agent.py](software_engineering_team/frontend_agent/agent.py) similar). They do **not** handle `data.get("content")`, so they end up with no files and no code and log “produced no files and no code. Task may have failed.”

**Changes**

- **Content fallback in backend and frontend agents**  
When `data.get("files")` is empty (or missing) and `data.get("content")` is a non-empty string:
  - Try to parse the content for **markdown code blocks** (e.g. `path/to/file.ext` or `json`with a convention like first line = path). If you can extract path + body pairs, build a`files`dict and merge it into (or use as)`raw_files` before validation.
  - Optionally, if a single code block exists and looks like one file, map it to a single inferred path (e.g. from task or from “write to X” in the prompt). This avoids dropping the entire response when the model wraps JSON in markdown.
- **Stricter JSON extraction (optional)**  
In `_extract_json`, when the first JSON parse fails, try stripping common leading/trailing noise (e.g. “Here is the JSON:”) and retry once before falling back to `{"content": ...}`. This can reduce how often the fallback is used.
- **Prompt / system message**  
The system message in [llm.py](software_engineering_team/shared/llm.py) (533–535) already says “Respond with a single valid JSON object only”. Consider adding one line in the backend/frontend prompts: “You must respond with only a single JSON object; no text before or after it.” to further reduce non-JSON output.

These changes make “produced no files and no code” rare when the model actually produced code in a non-standard wrapper.

---

## 3. Frontend: NG8002 (formGroup), NG1 (wrong property), TS2307 (@@angular)

**What’s happening**

- **NG8002: Can't bind to 'formGroup'**  
The template uses `formGroup` / `formControlName` but the component (or its module) does not import `ReactiveFormsModule`. The prompt mentions “reactive forms” but does not explicitly require importing `ReactiveFormsModule` when using `formGroup`.
- **NG1: Property 'activeFilterIndex' does not exist**  
Template references a property that doesn’t exist on the component (naming/typo). No automatic repair today.
- **TS2307: Cannot find module '@@angular/core/testing'**  
Typo: `@@angular` instead of `@angular`. Likely from the model; no normalization in the pipeline.

**Changes**

- **Prompt**  
In [frontend_agent/prompts.py](software_engineering_team/frontend_agent/prompts.py), add a short bullet under Angular/template rules: “When using `formGroup`, `formControlName`, or `formArrayName` in a template, the component must import `ReactiveFormsModule` in its `imports` array (standalone) or the declaring NgModule must import `ReactiveFormsModule`.”
- **Repair: ReactiveFormsModule**  
In [command_runner.py](software_engineering_team/shared/command_runner.py), add a repair (similar to `_ensure_provide_animations_in_config`) that runs before or as part of frontend build/dependency steps: scan `.component.ts` and `.component.html` under the frontend app; if an HTML file contains `formGroup` (or `formControlName`) and the corresponding component’s `.ts` file does not import `ReactiveFormsModule`, add the import and add `ReactiveFormsModule` to the component’s `imports` array. Call this from `ensure_frontend_dependencies_installed` (or from the same place that calls the existing `_ensure_provide_animations_in_config`).
- **Normalize @@angular**  
When writing frontend files (e.g. in the frontend agent’s file write path or in a shared writer), normalize `@@angular` → `@angular` in file contents (or at least in `.ts`/`.html`). Alternatively, add a small repair that scans `src/**/*.ts` for `'@@angular` and `"@@angular` and replaces with `@angular`. This prevents TS2307 from this typo.
- **activeFilterIndex / property mismatches**  
Harder to auto-fix. Mitigations: (1) In the frontend prompt, add a line: “Template bindings and property names must exactly match the component class (e.g. no typos like activeFilterIndex vs activeFilter).” (2) Optionally, when build fails with an NG1 “Property X does not exist… Did you mean Y?”, include that exact line in the context for the next LLM call so the model can fix the typo.

Implementing the prompt + ReactiveFormsModule repair + @@angular normalization will remove most of the recurring frontend build failures you saw.

---

## 4. Backend: “Build failed 3 times with the same error”

This is largely a **consequence** of (1). Once backend verification runs `pip install -r requirements.txt` and uses `sys.executable` for pytest, the “same error” will usually be real test/code failures, which the agent can address. No separate “3 times” logic change is strictly required; optionally you could add a single retry of “run pip install then re-run build” when the last error is `ModuleNotFoundError` or `ImportError`, but the above change makes that rarely necessary.

---

## 5. Empty file content (e.g. `tests/__init__.py`) rejected

Log: “Backend output validation: Empty file content for 'tests/**init**.py' - skipping”. The agent sometimes returns empty content for boilerplate files; [backend_agent/agent.py](software_engineering_team/backend_agent/agent.py) `_validate_file_paths` rejects empty content and the file is skipped, which can leave the tree inconsistent.

**Change**

- Allow **known boilerplate paths** to have empty or whitespace-only content: when the path is exactly `tests/__init__.py` (or a short allowlist), treat it as valid and write an empty string or a single line `'"""Tests package."""\n'` so the file exists. Alternatively, when content is empty for `**/__init__.py`, auto-set content to `'"""Package."""\n'` before validation so the file is not skipped. This avoids “empty file content - skipping” for these cases and keeps the test package importable.

---

## Implementation order


| Priority | Item                                                       | Where                                                                                                                                                     |
| -------- | ---------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1        | Install deps before pytest + use sys.executable for pytest | [orchestrator.py](software_engineering_team/orchestrator.py), [command_runner.py](software_engineering_team/shared/command_runner.py)                     |
| 2        | Content fallback when LLM returns raw content              | [backend_agent/agent.py](software_engineering_team/backend_agent/agent.py), [frontend_agent/agent.py](software_engineering_team/frontend_agent/agent.py)  |
| 3        | ReactiveFormsModule repair + formGroup prompt              | [command_runner.py](software_engineering_team/shared/command_runner.py), [frontend_agent/prompts.py](software_engineering_team/frontend_agent/prompts.py) |
| 4        | @@angular normalization                                    | File write path or repair in [command_runner.py](software_engineering_team/shared/command_runner.py) / frontend flow                                      |
| 5        | Template property name prompt + optional NG1 in feedback   | [frontend_agent/prompts.py](software_engineering_team/frontend_agent/prompts.py)                                                                          |
| 6        | Allow empty/boilerplate **init**.py                        | [backend_agent/agent.py](software_engineering_team/backend_agent/agent.py) `_validate_file_paths` or write step                                           |


---

## Summary

- **Backend**: Run `pip install -r requirements.txt` in the backend dir before every pytest, and run pytest with `sys.executable` so the same interpreter has the agent-added dependencies. This eliminates the majority of `ModuleNotFoundError` and “Build failed 3 times” loops.
- **LLM fallback**: When the response is wrapped as `{"content": "..."}`, parse code blocks (and optionally a single block) into a `files` dict so the agents still produce files instead of “no files and no code”.
- **Frontend**: Add an explicit formGroup/ReactiveFormsModule rule and a repair that adds the import when needed; normalize `@@angular` to `@angular`; tighten the prompt for template property names. Optionally feed NG1 “Did you mean Y?” back into the next fix attempt.

Together, these keep the same workflow and agent design but make the errors and warnings you observed rare or non-existent in normal runs.