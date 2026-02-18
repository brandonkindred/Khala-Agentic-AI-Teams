---
name: Improve problem-solving mode detail
overview: Improve the detail and accuracy of build/test failure feedback so the backend (and frontend) agent can fix issues without mis-targeting files or misinterpreting errors. Changes span error parsing (all failing tests, status-code-specific guidance), agent feedback format, and problem-solving instructions.
todos:
  - id: parse-all-failed-lines
    content: "error_parsing: Use re.findall to collect every FAILED path::test_name; build list of (file_path, test_name) for all failures; dedupe by (file, test_name)"
    status: pending
  - id: traceback-file-regex
    content: "error_parsing: Add regex to detect traceback file:line (e.g. tests/test_foo.py:277) in raw_excerpt near assertion"
    status: pending
  - id: primary-from-traceback
    content: "error_parsing: Set file_path to traceback file when it appears in failing-tests list; else first failing test file; keep single ParsedFailure"
    status: pending
  - id: message-summary-all-tests
    content: "error_parsing: Set message to include 'N tests failed: file::test1, file::test2, ...' when multiple; keep single-failure message backward compatible"
    status: pending
  - id: suggestion-all-tests
    content: "error_parsing: Set suggestion to 'Fix the following failing tests: <list>. Ensure each test sends required auth when endpoint requires it.' when multiple; else existing suggestion format"
    status: pending
  - id: playbook-401-constant
    content: "error_parsing: Add PLAYBOOK_401_UNAUTHORIZED constant (401 = unauthenticated; add auth in test client; do not disable auth in app)"
    status: pending
  - id: playbook-401-append
    content: "error_parsing: When expected_got or assertion line indicates got 401, append PLAYBOOK_401_UNAUTHORIZED to playbook_hint"
    status: pending
  - id: detect-401-in-output
    content: "error_parsing: Detect 401 via expected_match (E +401 / E -200) or assertion_line containing '401' and status_code"
    status: pending
  - id: playbook-403-404
    content: "error_parsing: (Optional) Add PLAYBOOK_403_FORBIDDEN and PLAYBOOK_404_NOT_FOUND; append when 403/404 detected in assertion"
    status: pending
  - id: build-feedback-failing-tests-section
    content: "error_parsing build_agent_feedback: Add 'Failing tests:' section listing each file::test_name when PYTEST_ASSERTION and we have multiple or primary has file_path"
    status: pending
  - id: build-feedback-interpretation-401
    content: "error_parsing build_agent_feedback: Add 'Interpretation:' line before Playbook when primary is PYTEST_ASSERTION and 401 playbook present"
    status: pending
  - id: build-feedback-keep-suggestion-playbook
    content: "error_parsing build_agent_feedback: Keep Suggestion and Playbook order; ensure Raw output still last; no duplicate content"
    status: pending
  - id: prompt-utils-test-failure-bullet
    content: "prompt_utils: Add bullet 'For test failures: use Failing tests and Interpretation; if expected 200 got 401 fix by sending auth in test, not disabling auth in app'"
    status: pending
  - id: prompt-utils-fix-indicated-file-bullet
    content: "prompt_utils: Add bullet 'Fix the code or tests indicated by the error (file and assertion); do not change unrelated files'"
    status: pending
  - id: backend-cr-file-path-optional
    content: "backend_agent: (Optional) In _build_code_review_issues_for_build_failure, extract 'Fix tests/<path>' from build_errors and set file_path so (file: ...) is correct"
    status: pending
  - id: test-multiple-failed-lines
    content: "test_error_parsing: Add test with 2+ FAILED lines (different files); assert message/suggestion list all; assert file_path is traceback file when in excerpt"
    status: pending
  - id: test-single-failed-backward-compat
    content: "test_error_parsing: Ensure single FAILED line still produces one ParsedFailure with same file_path/message shape as before (backward compat)"
    status: pending
  - id: test-401-playbook
    content: "test_error_parsing: Add test with 'assert 200 == 401' and E +401 E -200; assert playbook_hint contains 401 and 'auth' and 'test client'"
    status: pending
  - id: test-feedback-interpretation
    content: "test_error_parsing: Add test that build_agent_feedback([ParsedFailure with 401 playbook]) includes 'Interpretation:' and 401/auth wording"
    status: pending
  - id: test-feedback-failing-tests-section
    content: "test_error_parsing: Add test that build_agent_feedback for multi-test failure includes 'Failing tests:' and list of file::test_name"
    status: pending
  - id: adjust-existing-assertion-test
    content: "test_error_parsing: Adjust test_parse_pytest_assertion_extracts_test_and_assertion if new logic changes output; keep assertions for file_path and message"
    status: pending
  - id: test-prompt-utils-instructions
    content: "test_prompt_utils: Add or update test that default instructions include test-failure and 401/auth bullet text"
    status: pending
  - id: run-error-parsing-tests
    content: Run pytest software_engineering_team/tests/test_error_parsing.py and test_prompt_utils.py; fix any regressions
    status: pending
  - id: run-backend-agent-tests
    content: Run backend_agent tests that use code_review_issues; ensure problem-solving block still built correctly
    status: pending
  - id: manual-smoke
    content: "Manual smoke: trigger a backend pytest 401 failure in a test run; confirm agent receives Failing tests, Interpretation, and correct file_path in prompt"
    status: pending
isProject: false
---

# Improve problem-solving mode for build/test failures

## Problem

The backend agent repeatedly hits the same pytest failure (e.g. expected 200, got 401) and keeps patching the wrong place because:

1. **Wrong file suggested** – Only the **first** `FAILED tests/...` line in pytest output is parsed, so the agent is told "Fix tests/test_auth_middleware.py" while the actual failing tests are in `tests/test_task_endpoints.py` (e.g. `test_toggle_completion_updates_status`, `test_delete_task_removes_only_tenant_owned_tasks`).
2. **Generic playbook** – The playbook says "Fix the failing assertion. Check test expectations and implementation." It does not explain that **401 = unauthenticated request**, so the agent does not know the fix is to add auth (e.g. `Authorization` header or fixture) in the **test client**, not to change auth middleware.
3. **No interpretation** – The prompt does not spell out "where it originates" or "how to fix it" in a way that avoids re-reading the codebase.

## Approach

- **Enrich parsed failures** so feedback lists **all** failing tests and includes **status-code-specific** interpretation and playbook text.
- **Improve agent feedback** so the string passed to the agent has a clear "What failed", "Interpretation", and "How to fix" structure.
- **Tighten problem-solving instructions** so the LLM is explicitly told how to interpret 401/403/404 and where to apply the fix (test vs app).

---

## 1. Parse all failing tests and add status-code interpretation ([shared/error_parsing.py](software_engineering_team/shared/error_parsing.py))

**Current behavior:** `parse_pytest_failure` uses `re.search` and captures only the first `FAILED path::test_name` match. One `ParsedFailure` is produced with a single `file_path` and `suggestion` (e.g. "Fix tests/test_auth_middleware.py").

**Changes:**

- Use `**re.findall**` (or a loop over matches) to collect every `FAILED tests/test_<name>.py::test_<name>` (or equivalent) from the output. Build a list of `(file_path, test_name)` for all failures.
- Set **primary failure** to the first (or the one whose assertion appears in the excerpt), but set **message/suggestion/raw** so they reflect that **all** listed tests failed. For example:
  - **message**: include a line like "N tests failed: tests/test_task_endpoints.py::test_toggle_completion_updates_status, tests/test_task_endpoints.py::test_delete_task_removes_only_tenant_owned_tasks, ...".
  - **suggestion**: "Fix the following failing tests: . Ensure each test's requests satisfy the assertion (e.g. if assertion expects 200, provide valid auth when the endpoint requires it)."
  - **file_path**: prefer the file that appears in the **assertion block** (e.g. the `tests/test_foo.py:line` in the traceback) when detectable; otherwise the first failing test file. This avoids suggesting `test_auth_middleware.py` when the traceback points at `test_task_endpoints.py`.
- **Status-code-specific playbook:** When the assertion or expected/got pattern shows a **response status** (e.g. `assert response.status_code == 200` and `got 401`):
  - **401**: Set or append a dedicated playbook line, e.g.  
  `"401 Unauthorized means the request was not authenticated. Fix by ensuring the test client sends the required auth (e.g. Authorization header or token) for protected endpoints. Do not disable or bypass auth in the application."`
  - Optionally add short hints for **403** (forbidden) and **404** (not found) so the agent knows whether to fix permissions or routing/data.
- Keep a single `ParsedFailure` for the assertion block so existing `build_agent_feedback` and orchestrator behavior remain compatible; the new fields (full list of failing tests, interpretation) can be carried in `message`, `suggestion`, and `playbook_hint`.

**Implementation notes:**

- Regex for multiple FAILED lines: e.g. `re.findall(r"FAILED\s+([a-zA-Z0-9_/.-]+test_[a-zA-Z0-9_]+\.py)(?:::(test_[a-zA-Z0-9_]+))?", text)` to get all (file, test_name) pairs.
- Prefer **traceback file** for `file_path`: look for a line like `tests/test_task_endpoints.py:277` in the raw excerpt (near the assertion) and use that file if it appears in the failing-tests list.
- Add a constant, e.g. `PLAYBOOK_401_UNAUTHORIZED = "401 Unauthorized means..."`, and append it to `playbook_hint` when `expected_got` or assertion line indicates status code 401.

---

## 2. Add "Interpretation" and "Failing tests" to agent feedback ([shared/error_parsing.py](software_engineering_team/shared/error_parsing.py))

**In `build_agent_feedback`:**

- When the primary failure is `PYTEST_ASSERTION` and we have a list of failing tests (or a multi-test message), add a **"Failing tests:"** section that lists each `file::test_name` (and optionally the assertion line for the primary failure). This makes "where it originates" explicit.
- When the primary failure has a status-code-specific playbook (e.g. 401), add an **"Interpretation:"** line before the Playbook, e.g.  
`"Interpretation: The test expected a successful response (e.g. 200) but got 401. The endpoint requires authentication; the test request is missing or has invalid auth. Fix the test to send the required auth header or use an authenticated test client."`
- Keep **Suggestion** and **Playbook** as they are today, but ensure they reference the correct file(s) and the new 401/403/404 hints when applicable.

This keeps the feedback self-contained so the LLM does not need to re-read the codebase to understand what failed and how to fix it.

---

## 3. Stronger problem-solving instructions ([shared/prompt_utils.py](software_engineering_team/shared/prompt_utils.py))

**In `build_problem_solving_header`, default instructions:**

- Add one or two bullets focused on **test/build failures**, for example:
  - "For test failures: use the **Failing tests** and **Interpretation** sections to identify the exact tests and cause. If the failure shows **expected 200, got 401**, the request is unauthenticated—fix by making the test send the required auth header or token; do not disable auth in the app."
  - "Fix the code or tests indicated by the error (file and assertion). Do not change unrelated files or tests."

This aligns agent behavior with the improved feedback (correct file, 401 = add auth in test).

---

## 4. Use parsed failure file in backend code-review issue when available ([backend_agent/agent.py](software_engineering_team/backend_agent/agent.py))

**Current behavior:** `_build_code_review_issues_for_build_failure(build_errors)` only uses the raw string. It sets `file_path` to `""` (or `app/main.py` for exception-handler pattern) and suggestion to "Fix the compilation/test errors", so the issue's "file" hint is unhelpful.

**Change:**

- When `build_errors` is the feedback string from the orchestrator, it already contains the parsed "Suggestion: Fix tests/...". Optionally, **re-parse** the failure from the **pytest stdout/stderr** if available in the workflow. That would require the build_verifier to return structured data (e.g. parsed failures + raw string). A lighter approach: **parse the feedback string** in the backend to extract the first "Fix tests/...`path and use it as`file_path`for the code review issue, so the issue's`(file: ...)` line points at the actual failing test file. That way the agent sees a consistent file target in both the description body and the file_path field.
- Alternatively, leave `_build_code_review_issues_for_build_failure` as-is and rely on the improved **description** content (from build_agent_feedback) and the new **Interpretation** and **Failing tests** sections so the agent has enough detail without changing the backend API. Recommended: implement §1 and §2 first; only add backend re-parse or feedback parsing if we still see wrong-file behavior.

---

## 5. Tests and validation

- **[tests/test_error_parsing.py](software_engineering_team/tests/test_error_parsing.py):**
  - Add a test with **multiple** `FAILED` lines (e.g. two different test files). Assert that the parsed failure lists **all** failing tests in message or suggestion and that `file_path` prefers the file that appears in the assertion traceback when present.
  - Add a test with **401** in the assertion (e.g. "assert 200 == 401" / "got 401"). Assert that the parsed failure's `playbook_hint` (or message) includes the 401-specific guidance.
  - Assert that `build_agent_feedback` for that 401 case includes an "Interpretation:" (or equivalent) section.
- **Existing tests:** Adjust `test_parse_pytest_assertion_extracts_test_and_assertion` if the new logic changes the single-failure message/suggestion format; keep backward compatibility for single FAILED line.

---

## Summary of file changes


| File                                                                                                           | Change                                                                                                                                                                                                                                        |
| -------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [software_engineering_team/shared/error_parsing.py](software_engineering_team/shared/error_parsing.py)         | Parse all FAILED lines; prefer traceback file for `file_path`; add 401/403/404 playbook constants and use them when status-code assertion detected; in `build_agent_feedback` add "Failing tests:" and "Interpretation:" for assertion + 401. |
| [software_engineering_team/shared/prompt_utils.py](software_engineering_team/shared/prompt_utils.py)           | Add default-instruction bullets for test failures and 401 (use Failing tests/Interpretation; fix auth in test, not app).                                                                                                                      |
| [software_engineering_team/backend_agent/agent.py](software_engineering_team/backend_agent/agent.py)           | Optional: parse feedback string for "Fix tests/..." and set code review issue `file_path` so the issue points at the right file.                                                                                                              |
| [software_engineering_team/tests/test_error_parsing.py](software_engineering_team/tests/test_error_parsing.py) | New tests: multiple FAILED lines, 401 playbook and Interpretation in feedback.                                                                                                                                                                |


These updates make the problem-solving payload explicit about **what failed**, **where** (all failing tests and correct file), and **how to fix it** (e.g. 401 → add auth in test), so the LLM can resolve the issue without extra codebase lookup.