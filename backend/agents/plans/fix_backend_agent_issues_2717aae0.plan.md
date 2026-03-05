---
name: Fix Backend Agent Issues
overview: "The backend agent has multiple critical issues causing it to not commit actual code: (1) Merges to development branch never happen, (2) Many tasks produce zero files, (3) Work doesn't accumulate across tasks. This plan addresses root causes with targeted fixes."
todos:
  - id: emergency-merge
    content: Add emergency merge logic to merge partial work even when build fails repeatedly
    status: completed
  - id: sync-development
    content: Add sync-with-development step before code generation so tasks can build on previous work
    status: completed
  - id: relax-validation
    content: Relax file path validation rules that reject valid test file names
    status: completed
  - id: preflight-check
    content: Add pre-flight test compatibility check before running build verification
    status: completed
  - id: empty-handling
    content: Improve empty response handling with better diagnostics and stub fallback
    status: completed
  - id: graceful-degradation
    content: Add graceful degradation options instead of hard exit on build failures
    status: completed
  - id: output-extraction
    content: Enhance LLM output extraction to handle more edge cases
    status: completed
isProject: false
---

# Fix Backend Agent Not Committing Actual Code

## Problem Summary

Investigation of `/home/deepthought/Dev/agent-written-apps/todo/backend` reveals:

- **27 planned tasks** with detailed specs for auth, CRUD, rate limiting, audit logging, etc.
- **Only 7 Python files exist** totaling ~500 lines (most is test code)
- **20 feature branches created** but development branch has ONLY "Initial commit"
- **NO merges to development** - All work is stranded on isolated feature branches

## Root Cause Analysis

### Issue 1: Merges Never Happen (CRITICAL)

Looking at git graph, all feature branches diverge from "Initial commit" and never merge back:

```
* 19e82fb feat(validation): ... (feature/backend-tests-task-validation)
| * 0f6538a feat(api): ...      (feature/backend-tests-error-handling)
|/  
| * 9fe1ebc feat(api): ...      (feature/backend-audit-logger-service)
|/  
* 30eb208 Initial commit        (development, main)
```

**Why merges fail:**

1. Build verification fails repeatedly (pytest errors), causing early exit at [agent.py:1314-1324](software_engineering_team/backend_agent/agent.py)
2. The workflow sets `repeated_build_failure_reason` and exits before Step 7 (merge)
3. When exits happen, feature branches are left dangling

**Evidence:** The current branch `feature/backend-tests-task-validation` has test files that reference non-existent endpoints (test_validation_endpoints.py tests POST/PATCH /tasks but main.py only has /health).

### Issue 2: Many Tasks Produce Zero Files

Multiple branches have NO commits beyond "Initial commit":

- `feature/backend-auth-token-hashing`
- `feature/backend-auth-token-store-schema`
- `feature/backend-task-data-models`
- etc.

**Why tasks produce no files:**

1. LLM returns `needs_clarification=true` but Tech Lead refinement loop exhausts
2. File path validation rejects all files (overly strict rules at [agent.py:47-105](software_engineering_team/backend_agent/agent.py))
3. Empty retry loop (4 attempts) fails to get valid output
4. JSON parse failures return `{content: raw_text}` wrapper with no files

### Issue 3: Tests Reference Non-Existent Code

Current state has tests for validation endpoints but NO endpoints exist:

```python
# tests/test_validation_endpoints.py references:
# POST /tasks, PATCH /tasks/{id}
# But app/main.py only has:
# GET /health
```

This creates circular failure: tests fail -> build fails -> no merge -> next task starts from scratch.

### Issue 4: Work Doesn't Accumulate

Each task creates a new feature branch from development, but since development never gets updates, each task starts from the "Initial commit" state. Task N can't build on Task N-1's work.

## Proposed Fixes

### Fix 1: Emergency Merge on Failure (Priority: HIGH)

When workflow exits due to repeated build failures, still attempt to merge if ANY code was committed:

```python:624:640:software_engineering_team/backend_agent/agent.py
# At line ~1314, before returning failure:
if repeated_build_failure_reason is not None:
    # NEW: Attempt partial merge if any files were committed
    code, _ = _run_git(repo_path, ["git", "log", "--oneline", f"{DEVELOPMENT_BRANCH}..HEAD"])
    if code == 0:
        # There are commits to merge - try it
        merge_ok, _ = merge_branch(repo_path, branch_name, DEVELOPMENT_BRANCH)
        if merge_ok:
            logger.info("[%s] Partial merge successful despite build failures", task_id)
    checkout_branch(repo_path, DEVELOPMENT_BRANCH)
    return BackendWorkflowResult(...)
```

### Fix 2: Relax File Path Validation

Current validation rejects valid paths like `test_task_crud_qa.py`. Relax the rules:

- Increase `MAX_TEST_FILE_SEGMENT_LENGTH` from 45 to 60
- Allow more underscored words in test files (currently rejects 5+ words)
- Add common test prefixes to allowed patterns

Location: [agent.py:29-44](software_engineering_team/backend_agent/agent.py) and [repo_writer.py:23-50](software_engineering_team/shared/repo_writer.py)

### Fix 3: Better Empty Response Handling

When LLM returns empty files after 4 retries, the agent should:

1. Log detailed diagnostics (prompt length, response preview, validation rejections)
2. Fallback to generating stub files to keep workflow progressing
3. Create a follow-up task for the Tech Lead to address

Location: [agent.py:1936-1978](software_engineering_team/backend_agent/agent.py)

### Fix 4: Pre-flight Test Compatibility Check

Before running build verification, check if tests reference endpoints that don't exist:

```python
def _check_test_endpoint_compatibility(repo_path: Path) -> List[str]:
    """Return list of endpoints referenced in tests but missing from main.py."""
    # Parse tests/ for client.get/post/etc patterns
    # Parse app/main.py for @app.route/@router decorations
    # Return missing endpoints
```

If incompatible, regenerate code with targeted fix instruction instead of running doomed build.

### Fix 5: Accumulative Development Branch Updates

Add a "sync with development" step before code generation:

1. Before Step 2 (generate code), merge development into feature branch
2. This ensures the agent sees all previously committed code
3. Prevents "each task starts from scratch" problem

Location: Add after [agent.py:557](software_engineering_team/backend_agent/agent.py)

### Fix 6: Graceful Degradation for Build Failures

Instead of exiting after MAX_SAME_BUILD_FAILURES, offer options:

1. Merge anyway with failing tests (mark task as "partial")
2. Skip failing tests and continue review loop
3. Create follow-up fix task for Tech Lead

### Fix 7: Improve LLM Output Extraction

The `_extract_json` method in [llm.py:729-811](software_engineering_team/shared/llm.py) has multiple fallback paths but often returns `{content: raw_text}`. Enhance to:

1. Try extracting code blocks as individual files when JSON parse fails
2. Parse markdown file headers (e.g., `## app/main.py`) as file paths
3. Use heuristic file extraction more aggressively

## File Changes Summary


| File                     | Changes                                                                            |
| ------------------------ | ---------------------------------------------------------------------------------- |
| `backend_agent/agent.py` | Fixes 1, 3, 4, 5, 6 - Emergency merge, empty handling, pre-flight check, sync step |
| `shared/repo_writer.py`  | Fix 2 - Relax validation rules                                                     |
| `shared/git_utils.py`    | Fix 5 - Add sync branch helper                                                     |
| `shared/llm.py`          | Fix 7 - Better output extraction                                                   |


## Implementation Order

1. Fix 1 (emergency merge) - Prevents losing all work
2. Fix 5 (accumulative updates) - Enables task dependencies
3. Fix 2 (relax validation) - Stops rejecting valid files
4. Fix 4 (pre-flight check) - Prevents circular test failures
5. Fix 3 (empty handling) - Better diagnostics
6. Fix 6 (graceful degradation) - Better failure modes
7. Fix 7 (output extraction) - Handle edge cases

