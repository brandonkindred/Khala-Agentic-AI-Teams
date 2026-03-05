---
name: Prevent build failure warnings
overview: "The warnings in the logs stem from pytest assertion failures that the backend agent cannot fix within 3 retries. The plan addresses root causes: generic assertion feedback, missing test context, and duplicate logging—so the agent succeeds more often and log noise is reduced."
todos: []
isProject: false
---

# Prevent Build Failure Warnings

## Source causes of the warnings

The logs show four distinct warning/error emissions:


| Log line                                                                             | Source                                                                              | Trigger                                                               |
| ------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| `Tests failed for task backend-auth-middleware: failure_class=pytest_assertion`      | [orchestrator.py](software_engineering_team/orchestrator.py) line 429               | `_run_build_verification` when pytest fails and parsed failures exist |
| `[backend-auth-middleware] WORKFLOW [7] Build FAILED: [pytest_assertion]...`         | [backend_agent/agent.py](software_engineering_team/backend_agent/agent.py) line 531 | `build_verifier` returns False                                        |
| `[backend-auth-middleware] WORKFLOW [7] Build failed 3 times with the same error...` | [backend_agent/agent.py](software_engineering_team/backend_agent/agent.py) line 552 | Same build error (first 800 chars) repeats 3 times                    |
| `[backend-auth-middleware] Backend FAILED after 1035.6s: Build failed 3 times...`    | [orchestrator.py](software_engineering_team/orchestrator.py) line 713               | Backend workflow exits with `success=False`                           |


**Root cause chain:**

1. **Proximate**: Pytest tests in `test_auth_middleware.py` fail (assertion or status code mismatch).
2. **Underlying**: The agent-generated auth middleware does not match test expectations.
3. **Why the agent gets stuck**:
  - **Generic feedback**: For `PYTEST_ASSERTION`, [error_parsing.py](software_engineering_team/shared/error_parsing.py) uses a generic message ("One or more tests failed") and suggestion ("Review the failing test(s)"). It does not extract test name, assertion line, or expected vs actual.
  - **No test context**: The agent does not receive the failing test file content, so it cannot see the exact expectations (e.g. `assert response.status_code == 401`).
  - **Same-error detection**: The backend agent uses the first 800 chars of `build_errors` as the signature. For assertion failures, the structure is identical across retries; only the raw excerpt varies. If the same test keeps failing with the same assertion, the output can be identical, triggering the 3-strike stop.
  - **Escalation is vague**: At 2+ repeats, an escalation issue is added ("Focus ONLY on fixing this specific error") but does not inject the failing test file or more specific assertion details.

---

## Plan to prevent these warnings

### 1. Parse pytest assertion failures more granularly

**File**: [shared/error_parsing.py](software_engineering_team/shared/error_parsing.py)

- Add regex patterns to extract from pytest output:
  - **Test name**: e.g. `test_invalid_auth_header` from `FAILED tests/test_auth_middleware.py::test_invalid_auth_header`
  - **Test file**: `tests/test_auth_middleware.py`
  - **Assertion line**: e.g. `assert 200 == 401` or `assert response.status_code == 401`
- For `FailureClass.PYTEST_ASSERTION`, populate `ParsedFailure` with:
  - `file_path`: failing test file
  - `message`: e.g. `test_invalid_auth_header failed: assert 200 == 401 (expected 401, got 200)`
  - `suggestion`: Include test name and assertion so the agent knows what to fix.
- Update `build_agent_feedback` so the suggestion and raw excerpt highlight these fields.

**Rationale**: Targeted feedback (test name + assertion) helps the LLM fix the right behavior instead of guessing.

---

### 2. Inject failing test file content on repeated assertion failures

**File**: [backend_agent/agent.py](software_engineering_team/backend_agent/agent.py)

- When `consecutive_same_build_failures >= 2` and the failure is `pytest_assertion`:
  - Parse the build_errors (or use `parsed_failures` if available) to get the failing test file path.
  - Read that test file from the repo.
  - Add a code-review issue or append to the escalation that includes: "Failing test expectations (from {file}):\n`\n{content}\n`"
- This gives the agent the exact assertions to satisfy.

**Implementation note**: The build_verifier returns a string (`build_agent_feedback` output). The backend agent does not receive `ParsedFailure` objects directly. Options:

- (A) Have `_run_build_verification` return `(ok, errors, parsed_failures)` and pass parsed failures through; or
- (B) Re-parse the build_errors string in the backend agent to extract the test file (e.g. regex for `tests/test_*.py` in the raw output). Option (B) is simpler and avoids changing the build_verifier contract.

---

### 3. Refine same-error signature for assertion failures

**File**: [backend_agent/agent.py](software_engineering_team/backend_agent/agent.py)

- For `pytest_assertion` failures, the first 800 chars of `build_errors` are often the same (header + suggestion + playbook). The varying part is the raw excerpt.
- Change the signature logic:
  - If the raw output contains a test name (e.g. `::test_`), include a normalized test-name segment in the signature so that "same error" means "same failing test with same assertion."
  - Alternatively: use a hash of the last 1500 chars (where the assertion usually is) instead of the first 800, so assertion changes are detected.
- Goal: Avoid stopping after 3 retries when the agent is making progress (e.g. fixing one test but another still fails).

---

### 4. Improve escalation when same error repeats

**File**: [backend_agent/agent.py](software_engineering_team/backend_agent/agent.py)

- When `consecutive_same_build_failures >= 2`, the escalation issue says "Focus ONLY on fixing this specific error" but does not add concrete context.
- Enhance the escalation to:
  - Include the failing test file path (from parsing).
  - If we inject test content (item 2), reference it: "The failing test expects ... (see test file above)."
  - Add a playbook hint: "Read the failing test's assertions line-by-line and ensure the implementation satisfies each one."

---

### 5. Reduce duplicate logging (optional)

**Files**: [orchestrator.py](software_engineering_team/orchestrator.py), [backend_agent/agent.py](software_engineering_team/backend_agent/agent.py)

- Both the orchestrator and the backend agent log when tests fail. The orchestrator logs at the start of `_run_build_verification`; the backend agent logs when it receives the failure.
- Options:
  - **A**: In the orchestrator, downgrade the "Tests failed for task" log from WARNING to INFO when the failure will be passed to the agent (i.e. when the agent is in its retry loop). This is hard to detect without more context.
  - **B**: In the orchestrator, log "Tests failed" only at DEBUG when the build_verifier is used by the backend agent (always the case for backend tasks). Keep WARNING only for "final" failures (e.g. when the task is abandoned). Requires distinguishing "agent will retry" vs "task failed."
  - **C**: Leave both logs as-is; they serve different audiences (orchestrator = task-level, agent = workflow iteration). Document that both are expected.

**Recommendation**: Start with (C). If log volume is still an issue, add a `log_level` parameter to `_run_build_verification` or a flag like `agent_will_retry` so the orchestrator can log at INFO when the agent will retry.

---

## Summary


| Change                                                          | File(s)                | Effect                        |
| --------------------------------------------------------------- | ---------------------- | ----------------------------- |
| Parse assertion details (test name, assertion, expected/actual) | error_parsing.py       | More targeted agent feedback  |
| Inject failing test file content on 2+ repeats                  | backend_agent/agent.py | Agent sees exact expectations |
| Refine same-error signature for assertions                      | backend_agent/agent.py | Fewer premature stops         |
| Improve escalation text                                         | backend_agent/agent.py | Clearer instructions on retry |
| (Optional) Reduce duplicate "Tests failed" log                  | orchestrator.py        | Less log noise                |


The primary lever is **better feedback and context** (items 1–4). That should reduce how often the agent hits the 3-strike limit, which in turn reduces the warnings. Item 5 is a secondary improvement for log clarity.