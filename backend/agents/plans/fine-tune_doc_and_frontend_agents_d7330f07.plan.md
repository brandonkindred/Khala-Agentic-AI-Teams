---
name: Fine-tune doc and frontend agents
overview: Fix the Documentation Agent to reliably create README.md when it doesn't exist, and constrain the Frontend Agent to produce only browser-compatible Angular code (no Python/backend code).
todos:
  - id: doc-prompt
    content: Update documentation_agent/prompts.py to clarify readme_changed must be true when creating from scratch
    status: completed
  - id: doc-agent-run
    content: Update documentation_agent/agent.py run() method to force readme_changed when README is missing
    status: completed
  - id: doc-agent-workflow
    content: Update documentation_agent/agent.py run_full_workflow() to force readme_changed when README is missing
    status: completed
  - id: fe-prompt
    content: Add FRONTEND-ONLY constraints to frontend_agent/prompts.py (no Python, browser-only, connect to backend API)
    status: completed
  - id: fe-validation
    content: Enforce src/ path prefix and allowed file extensions in frontend_agent/agent.py _validate_file_paths()
    status: completed
isProject: false
---

# Fine-Tune Documentation and Frontend Agents

## Problem 1: Documentation Agent Not Writing README.md

**Root cause analysis:** The Documentation Agent at [documentation_agent/agent.py](software_engineering_team/documentation_agent/agent.py) already handles missing files gracefully -- `_read_file()` returns `""` and the prompt says "(none -- create from scratch)". There is also a safety check (lines 152-154) that catches when the LLM returns content but mistakenly sets `readme_changed: false`. However, two weaknesses remain:

1. **Prompt weakness:** The LLM prompt at [documentation_agent/prompts.py](software_engineering_team/documentation_agent/prompts.py) describes `readme_changed` as "true if the README was meaningfully updated" -- creating a file from scratch may not register as an "update" to the LLM.
2. **No forced creation:** When no README exists at all, the agent should unconditionally treat this as `readme_changed = true` at the code level, not rely solely on the LLM's judgment.

**Changes:**

### A. Strengthen the prompt ([documentation_agent/prompts.py](software_engineering_team/documentation_agent/prompts.py))

In `DOCUMENTATION_README_PROMPT`, update the output format description for `readme_changed`:

```
- "readme_changed": boolean -- true if the README was meaningfully updated OR if no README existed and one was created from scratch. When the current README is "(none)", this MUST be true.
```

### B. Force creation when README is missing ([documentation_agent/agent.py](software_engineering_team/documentation_agent/agent.py))

In the `run()` method, after the existing safety check at line 152, add a second safety net: if `existing_readme` was empty (file didn't exist) and the LLM returned any `readme_content`, force `readme_changed = True`. This catches the case where the LLM generates a README but doesn't flag it as changed:

```python
# Safety: if no README existed and content was generated, force creation
if readme_content and not input_data.existing_readme:
    readme_changed = True
```

In `run_full_workflow()`, after reading existing docs at line 296, add a flag to track that the README didn't exist. Then after `self.run(input_data)` returns, if README didn't exist and the result has content, override `readme_changed` to `True`:

```python
readme_missing = not existing_readme

# ... after result = self.run(input_data) ...

if readme_missing and result.readme_content and not result.readme_changed:
    logger.info("DocAgent [%s]: README.md did not exist, forcing creation", task_id)
    result.readme_changed = True
```

---

## Problem 2: Frontend Agent Producing Python/Backend Code

**Root cause analysis:** The Frontend Agent at [frontend_agent/agent.py](software_engineering_team/frontend_agent/agent.py) has a path pattern `ANGULAR_PATH_PATTERN = re.compile(r"^src/")` that is **defined but never used** in `_validate_file_paths()`. The validation function checks naming conventions but does NOT:

- Enforce that all files start with `src/` (Angular project structure)
- Validate file extensions (`.py` files pass through unchecked)
- Reject server-side/backend code

The prompt at [frontend_agent/prompts.py](software_engineering_team/frontend_agent/prompts.py) mentions Angular but never explicitly prohibits Python or server-side code, and never states the frontend agent should connect to a backend API rather than write its own.

**Changes:**

### A. Add explicit frontend-only constraints to the prompt ([frontend_agent/prompts.py](software_engineering_team/frontend_agent/prompts.py))

Add a new "CRITICAL CONSTRAINTS" section near the top of the prompt, after the expertise list:

```
**CRITICAL CONSTRAINTS -- FRONTEND ONLY:**
- You are a FRONTEND-ONLY agent. Everything you produce MUST run in a web browser.
- NEVER write Python, Java, or any server-side/backend code. You do NOT write APIs, routes, database models, or server middleware.
- ONLY produce files with these extensions: .ts, .html, .scss, .css, .json, .spec.ts
- ALL file paths MUST start with "src/" (Angular project root). Any file outside src/ is WRONG.
- For data, ALWAYS connect to REST API endpoints provided by the Backend Engineer. Use Angular's HttpClient to call the API. NEVER implement your own backend, database, or server logic.
- If API endpoint details are not provided, define an Angular service with placeholder endpoint URLs and document them with TODO comments for later integration.
```

### B. Enforce path and extension validation in code ([frontend_agent/agent.py](software_engineering_team/frontend_agent/agent.py))

Update `_validate_file_paths()` to use the existing `ANGULAR_PATH_PATTERN` and add file extension validation:

- Add an allowed extensions set:

```python
_ALLOWED_EXTENSIONS = frozenset({
    ".ts", ".html", ".scss", ".css", ".json", ".spec.ts",
})
```

- In `_validate_file_paths()`, add two new checks before the existing segment validation:

1. **Path prefix check:** Reject files that don't start with `src/`:

```python
if not ANGULAR_PATH_PATTERN.match(path):
    warnings.append(f"Path does not start with 'src/' (not Angular project structure): '{path}'")
    continue
```

1. **Extension check:** Reject files with non-browser extensions (like `.py`):

```python
ext = "." + path.rsplit(".", 1)[-1] if "." in path else ""
if ext not in _ALLOWED_EXTENSIONS:
    warnings.append(f"File extension '{ext}' is not a browser-compatible frontend file: '{path}'")
    continue
```

---

## Summary of Files to Edit

- [documentation_agent/prompts.py](software_engineering_team/documentation_agent/prompts.py) -- Strengthen `readme_changed` description to require `true` when creating from scratch
- [documentation_agent/agent.py](software_engineering_team/documentation_agent/agent.py) -- Add forced README creation when file is missing (in both `run()` and `run_full_workflow()`)
- [frontend_agent/prompts.py](software_engineering_team/frontend_agent/prompts.py) -- Add "FRONTEND ONLY" constraints block prohibiting Python/backend code
- [frontend_agent/agent.py](software_engineering_team/frontend_agent/agent.py) -- Enforce `src/` path prefix and allowed file extensions in `_validate_file_paths()`

