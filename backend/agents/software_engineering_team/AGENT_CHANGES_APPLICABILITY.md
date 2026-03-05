# Backend Agent Fixes: Applicability to Other Agents

This document details which of the 7 backend agent fixes should be applied to other software engineering agents.

---

## Backend Agent Max Cycles Fix (2025)

The following changes were implemented to reduce max cycles exceeded and erroneous code in the backend workflow:

| Change | Description |
|--------|--------------|
| **Pre-write cap** | Nested pre-write loops (pre-flight, build fix, code review fix, etc.) now use `MAX_PREWRITE_REGENERATIONS=2` instead of 6. After 2 failed attempts to add missing test routes, the task fails with a clear message instead of burning through more LLM calls. |
| **Task plan in fix loop** | `_regenerate_with_issues` now accepts optional `task_plan`. For the first 2–3 fix attempts, the plan is passed so the agent stays anchored to the original implementation intent and does not remove plan-fulfilling code. |
| **Escalation path** | At 4 same build failures: prompt suggests considering if test expectations are wrong (update test vs implementation). At 5 same failures: workflow exits early, Tech Lead receives `task_update` with `needs_followup=True`, and a follow-up fix task can be created—instead of waiting for 6. |
| **Task granularity** | Backend planning prompt enforces max 1 resource, 3 endpoints, or 1 service module per TASK. CRUD entities require 3+ tasks. |
| **QA fix_build output** | QA fix_build mode now requires `file_path`, `line_or_section`, and `recommendation` starting with Add/Remove/Change/Fix. Backend fallback extracts file:line from build output for more specific suggestions. |

**Constants (backend_agent/agent.py):**

| Constant | Default | Env Override | Description |
|----------|---------|-------------|-------------|
| `MAX_PREWRITE_REGENERATIONS` | 2 | `SW_MAX_PREWRITE_REGENERATIONS` | Max regenerations for pre-write test-route checks (pre-flight, build fix, code review fix, etc.) |
| `MAX_REVIEW_ITERATIONS` | 100 | — | Max review loop iterations per task |
| `MAX_SAME_BUILD_FAILURES` | 6 | — | Absolute max same build failures (early exit at 5 so Tech Lead can create follow-up) |

---

## Summary Table

| Fix | Frontend Feature Agent | Frontend Orchestrator | DevOps Agent | Documentation Agent |
|-----|------------------------|----------------------|--------------|-------------------|
| 1. Emergency merge on failure | **YES** | **YES** | N/A (no branches) | N/A (no build loop) |
| 2. Sync with development | **YES** | **YES** | N/A (no branches) | **YES** |
| 3. Relax file path validation | **YES** | N/A (delegates) | N/A (different output) | N/A |
| 4. Pre-flight test compatibility | **PARTIAL** | **PARTIAL** | N/A | N/A |
| 5. Better empty response handling | **YES** | N/A (delegates) | **LOW** | N/A |
| 6. Graceful degradation (Tech Lead) | **YES** | **YES** | **LOW** | N/A |
| 7. LLM output extraction | **SHARED** (llm.py) | **SHARED** | **SHARED** | **SHARED** |

---

## Agent-by-Agent Detail

### 1. Frontend Feature Agent (`frontend_team/feature_agent/agent.py`)

**Workflow:** Creates feature branch, generates Angular code, runs build verification (ng build), code review, QA, accessibility, security, DBC, then merges to development.

| Fix | Apply? | Details |
|-----|--------|---------|
| **1. Emergency merge** | **YES** | When `consecutive_same_build_failures >= MAX_SAME_BUILD_FAILURES` (line ~725), the agent returns failure without merging. Partial work (e.g. components that compile but fail tests) is lost. Add `branch_has_commits_ahead_of` check and attempt merge before returning. |
| **2. Sync with development** | **YES** | After `create_feature_branch` (line ~572), add `merge_branch(repo_path, DEVELOPMENT_BRANCH, branch_name)` so the frontend sees backend/frontend work from parallel tasks. Add `abort_merge` on failure. |
| **3. Relax validation** | **YES** | Frontend has its own `_validate_file_paths` (line 110). Add `MAX_TEST_FILE_SEGMENT_LENGTH = 60` for `.spec.ts` files. Add exemption for `*.spec.ts` from sentence-like patterns (Angular uses kebab-case, so `BAD_NAME_PATTERN` may reject `task-list-detail.component.spec.ts`-style names; consider allowing longer spec file names). |
| **4. Pre-flight check** | **PARTIAL** | Frontend uses `ng build` and Karma/Jasmine—no HTTP route compatibility. Could add pre-flight for: (a) component selectors referenced in tests but missing, (b) imports that would fail. Lower priority than backend. |
| **5. Empty response handling** | **YES** | Frontend has 2 retry attempts (line 417) vs backend's 4. When empty after retries, it returns `validated_files={}` and fails at write step. Add stub fallback: minimal `src/app/app.component.ts` or placeholder so workflow progresses. Add validation_warnings to error log. |
| **6. Graceful degradation** | **YES** | When build fails repeatedly, add `needs_followup` to `FrontendWorkflowResult`, notify Tech Lead with `review_progress(task_update=TaskUpdate(..., status="failed", needs_followup=True))` so follow-up fix tasks can be created. |
| **7. LLM output extraction** | **DONE** | Uses `shared/llm.py` and `shared/llm_response_utils.py`—already enhanced. |

---

### 2. Frontend Orchestrator (`frontend_team/orchestrator.py`)

**Workflow:** Alternative frontend pipeline (design → architecture → implementation → quality gates). Has its own `run_workflow` that duplicates logic from `FrontendExpertAgent`.

| Fix | Apply? | Details |
|-----|--------|---------|
| **1. Emergency merge** | **YES** | Same pattern as feature agent: when `consecutive_same_build_failures >= MAX_SAME_BUILD_FAILURES` (line ~383), attempt emergency merge before returning failure. |
| **2. Sync with development** | **YES** | After `create_feature_branch` (line ~190), add sync step: `merge_branch(repo_path, DEVELOPMENT_BRANCH, branch_name)`. |
| **3. Relax validation** | N/A | Delegates to feature agent for code generation; validation is in feature agent. |
| **4. Pre-flight check** | **PARTIAL** | Same as feature agent—lower priority. |
| **5. Empty response handling** | N/A | Delegates to feature agent. |
| **6. Graceful degradation** | **YES** | Add Tech Lead notification and `needs_followup` when build fails repeatedly. |
| **7. LLM output extraction** | **DONE** | Shared. |

---

### 3. DevOps Agent (`devops_agent/agent.py`)

**Workflow:** No feature branches. Plans, generates YAML/Dockerfile, writes directly to repo, runs build verification. Loop: generate → validate → write → verify.

| Fix | Apply? | Details |
|-----|--------|---------|
| **1. Emergency merge** | **N/A** | No feature branches; writes directly to repo. Partial work is already on disk. No merge step. |
| **2. Sync with development** | **N/A** | No branches. |
| **3. Relax validation** | **N/A** | Uses `_validate_devops_output` for pipeline_yaml, dockerfile, etc.—different validation, not file paths. |
| **4. Pre-flight check** | **N/A** | No test/route compatibility; validates YAML syntax and structure. |
| **5. Empty response handling** | **LOW** | Returns `DevOpsWorkflowResult(success=False)` when `write_agent_output` fails (no files). Could add stub (e.g. minimal `.github/workflows/ci.yml`) but DevOps outputs are structured differently. Low value. |
| **6. Graceful degradation** | **LOW** | When build fails repeatedly, could notify Tech Lead. DevOps is often invoked by Tech Lead for specific tasks; a follow-up "fix CI" task could help. Would need `append_task_fn` or similar to add fix task. |
| **7. LLM output extraction** | **DONE** | Shared. |

---

### 4. Documentation Agent (`documentation_agent/agent.py`)

**Workflow:** Creates feature branch `feature/docs/{task_id}`, generates README/CONTRIBUTORS, writes files, merges to development. No build verification loop.

| Fix | Apply? | Details |
|-----|--------|---------|
| **1. Emergency merge** | **N/A** | No build verification loop; merge either succeeds or fails. No "repeated failure" scenario. |
| **2. Sync with development** | **YES** | After `create_feature_branch` (line ~396), add `merge_branch(path, DEVELOPMENT_BRANCH, branch_name)` so doc updates see latest code. Add `abort_merge` on failure. |
| **3. Relax validation** | **N/A** | Uses `write_files_and_commit` with explicit `files_to_write` dict built from result—no path validation. |
| **4. Pre-flight check** | **N/A** | No tests. |
| **5. Empty response handling** | **N/A** | Builds `files_to_write` from `result.readme_content`, etc. If empty, skips write and returns "no changes"—already handled. |
| **6. Graceful degradation** | **N/A** | No build failure loop. |
| **7. LLM output extraction** | **DONE** | Shared. |

---

## Implementation Priority

1. **High:** Frontend Feature Agent – Fixes 1, 2, 3, 5, 6 (same productivity issues as backend)
2. **High:** Frontend Orchestrator – Fixes 1, 2, 6 (emergency merge, sync, graceful degradation)
3. **Medium:** Documentation Agent – Fix 2 (sync with development)
4. **Low:** DevOps Agent – Fix 6 (Tech Lead notification on repeated failure)

---

## Shared vs Agent-Specific

| Component | Shared? | Location |
|-----------|---------|----------|
| `branch_has_commits_ahead_of` | YES | `shared/git_utils.py` (already added) |
| `abort_merge` | YES | `shared/git_utils.py` (already added) |
| Relaxed validation (repo_writer) | YES | `shared/repo_writer.py` (already applied) |
| LLM output extraction | YES | `shared/llm.py`, `shared/llm_response_utils.py` (already applied) |
| Emergency merge logic | NO | Per-agent (backend done; frontend needs it) |
| Sync step | NO | Per-agent (backend done; frontend, docs need it) |
| Pre-flight test check | NO | Backend-specific (pytest/TestClient); frontend equivalent would differ |
| Empty stub fallback | NO | Per-agent (backend: app/main.py; frontend: src/app/app.component.ts) |
| Tech Lead notification on failure | NO | Per-agent (backend done; frontend, devops need it) |
