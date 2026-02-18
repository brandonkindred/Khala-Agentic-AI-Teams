---
name: Fix backend agent failures
overview: The backend agent fails because the DummyLLM's prompt matching routes backend prompts to the DevOps response handler. This is the primary bug. The sequential execution model is already correct but needs minor hardening.
todos:
  - id: fix-elif-chain
    content: "Convert DummyLLM if-chain to elif-chain and reorder: backend before devops"
    status: completed
  - id: narrow-backend-extensions
    content: Change backend _read_repo_code default extensions from [.py,.ts,.tsx,.java,.yml,.yaml] to [.py,.java]
    status: completed
  - id: verify-sequential-logging
    content: Add assertion/log in orchestrator main loop confirming single-agent execution
    status: completed
  - id: test-run
    content: Restart the API server and verify backend tasks now succeed
    status: completed
isProject: false
---

# Fix Backend Agent Failures and Ensure Sequential Agent Execution

## Root Cause Analysis

### Primary Bug: DummyLLM Prompt Routing Mismatch

The backend agent consistently produces `0 files, 0 code` because the DummyLLM in `[shared/llm.py](software_engineering_team/shared/llm.py)` matches the backend agent's prompt against the **DevOps handler** instead of the **backend handler**.

The matching cascade (using `if`, not `elif`) checks DevOps first at line 368:

```368:368:software_engineering_team/shared/llm.py
        if "devops" in lowered or "pipeline" in lowered:
```

...before the backend check at line 378:

```378:378:software_engineering_team/shared/llm.py
        if "backend" in lowered and "language" in lowered and ("code" in lowered or "files" in lowered):
```

**Why it triggers:** The backend agent reads existing repo code via `_read_repo_code()` (line 269 of `agent.py`), which reads `.yml`/`.yaml` files. By the time backend tasks run, DevOps tasks have already committed YAML files containing the word "pipeline" (e.g., `# CI Pipeline`) into the `devops/` directory. These strings appear in the prompt's existing-code section, causing `"pipeline" in lowered` to be `True`, so the DevOps handler fires first.

The DevOps response has no `files` or `code` keys that the backend expects, resulting in:

```
Backend: produced no files and no code. Task may have failed.
```

### Sequential Execution: Already Correct

The orchestrator in `[orchestrator.py](software_engineering_team/orchestrator.py)` is a single-threaded synchronous `while` loop (line 463). Tasks execute strictly one at a time. The documentation agent's `run_full_workflow()` is called synchronously within `_run_tech_lead_review()` (line 248), blocking until complete before the next task starts. No concurrency primitives are needed because there is no concurrency. However, the interleaving of backend/frontend in the execution queue is purely about ORDER, not parallel execution.

## Changes

### 1. Fix DummyLLM prompt matching cascade (`shared/llm.py`)

Convert the prompt-matching `if` statements (lines 132-461) from `if...if...if` to `if...elif...elif`. This ensures the first match wins and prevents DevOps from catching backend prompts.

Additionally, tighten the DevOps match to require BOTH `"devops"` AND a devops-specific keyword (not just `"pipeline"` alone, which leaks into backend prompts via existing code):

**Before:**

```python
if "devops" in lowered or "pipeline" in lowered:
```

**After (two options, pick the cleaner one):**

```python
elif ("devops" in lowered or "pipeline" in lowered) and "language" not in lowered:
```

Or more surgically, just make the whole chain `elif`:

```python
elif "devops" in lowered or "pipeline" in lowered:
```

The `elif` alone fixes the issue because the more-specific code-review, security, and tech-lead matches all come before DevOps and will match first when appropriate. The backend match (which also checks for `"language"` and `"code"/"files"`) will then be checked before DevOps since we should reorder the chain to put more-specific matches first.

**Recommended approach:** Convert to `elif` chain AND move the backend match BEFORE the DevOps match, since the backend prompt is a strict superset of what DevOps might accidentally match.

### 2. Reorder DummyLLM match priority (`shared/llm.py`)

Move the backend agent match (line 378) to BEFORE the DevOps agent match (line 368). The backend match has three conditions (`"backend"`, `"language"`, `"code"/"files"`) making it far more specific than the DevOps two-condition match (`"devops" or "pipeline"`). More specific matches should come first.

New order in the elif chain:

1. Architecture (most specific -- 3 unique keywords)
2. Tech Lead codebase analysis
3. Tech Lead spec analysis
4. Tech Lead evaluate QA
5. Tech Lead security
6. Tech Lead review progress
7. Tech Lead refine task
8. Tech Lead plan
9. Code review agent
10. Security agent
11. **Backend agent** (moved up -- 3 conditions)
12. **Frontend agent** (3 conditions)
13. **DevOps agent** (moved down -- broad 2-condition match)
14. Documentation agent (README)
15. Documentation agent (contributors)
16. Tech Lead trigger docs
17. DbC Comments agent
18. QA / integration test
19. Spec parsing
20. Default fallback

### 3. Add explicit guard in backend `_read_repo_code` (`backend_agent/agent.py`)

Exclude `.yml`/`.yaml` files from the backend agent's existing-code reader. The backend agent only needs `.py` files for context. Reading DevOps YAML files adds noise and (with the DummyLLM) causes the routing bug. Even with a real LLM, including infrastructure YAML in a Python coding prompt is not useful.

**Before** (line 102-103):

```python
if extensions is None:
    extensions = [".py", ".ts", ".tsx", ".java", ".yml", ".yaml"]
```

**After:**

```python
if extensions is None:
    extensions = [".py", ".java"]
```

This is a defense-in-depth fix -- the elif chain fix is the primary fix, but narrowing the context makes the backend agent more focused regardless of LLM provider.

### 4. Verify sequential execution with logging (`orchestrator.py`)

The code is already sequential, but add a clear assertion/log at the top of the main loop iteration to confirm no concurrent execution. This satisfies the user's requirement for explicit single-agent-at-a-time enforcement without changing the existing (correct) architecture.