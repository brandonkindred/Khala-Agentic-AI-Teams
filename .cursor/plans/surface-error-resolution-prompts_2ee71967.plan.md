---
name: surface-error-resolution-prompts
overview: Adjust LLM prompt logging so that logs highlight the prompts and context used when agents are resolving errors, without dumping the full initial task prompt.
todos:
  - id: analyze-logging-helper
    content: Review and adjust log_llm_prompt so it logs metadata-only for mode=\"initial\" but keeps truncated body logging for mode=\"problem_solving\", and document the behavior difference.
    status: completed
  - id: refine-truncation-policy
    content: Evaluate and possibly reduce DEFAULT_MAX_PROMPT_LOG_CHARS and per-agent MAX_PROMPT_LOG_CHARS so problem-solving logs capture headers and key issue context without excessive body.
    status: completed
  - id: backend-problem-solving-context
    content: In BackendExpertAgent.run, log structured problem-solving context summarizing QA, security, and code review issue counts whenever has_issues is true.
    status: completed
  - id: backend-problem-solving-header
    content: In BackendExpertAgent.run, log an excerpt of the Backend problem-solving header text built with build_problem_solving_header so it is easy to see exactly how the LLM is instructed to fix errors.
    status: completed
  - id: frontend-problem-solving-context
    content: In FrontendExpertAgent.run, log structured Angular problem-solving context summarizing QA, accessibility, security, and code review issue counts whenever has_issues is true.
    status: completed
  - id: frontend-problem-solving-header
    content: In FrontendExpertAgent.run, log an excerpt of the Angular problem-solving header text (with _ANGULAR_PROBLEM_SOLVING_INSTRUCTIONS) so you can see what the LLM is told when fixing frontend errors.
    status: completed
  - id: ensure-regeneration-coverage
    content: Verify that all regeneration paths (e.g., _regenerate_with_issues for backend and problem-solving turns in frontend workflow) go through the same logging paths and emit problem-solving context/header logs.
    status: completed
  - id: tests-prompt-utils-initial-vs-problem-solving
    content: Extend tests in tests/test_prompt_utils.py to assert that initial-mode logs do not contain prompt body substrings while problem-solving logs do, preserving existing expectations on mode and length fields.
    status: completed
  - id: tests-backend-logging
    content: Add caplog-based tests for BackendExpertAgent.run that verify problem-solving context and header logs appear when issues are present and are absent when there are no issues.
    status: completed
  - id: tests-frontend-logging
    content: Add caplog-based tests for FrontendExpertAgent.run that verify problem-solving context and header logs appear when issues are present and are absent when there are no issues.
    status: completed
  - id: docs-observability
    content: Update engineering docs (e.g., software_engineering_team/README.md) to explain how to grep for mode=problem_solving and the new header/context tags to understand how agents instruct the LLM when resolving errors.
    status: completed
  - id: docs-log-tuning
    content: Document how to tune prompt logging verbosity via DEFAULT_MAX_PROMPT_LOG_CHARS and per-agent MAX_PROMPT_LOG_CHARS, including recommended defaults for production vs debugging.
    status: completed
isProject: false
---

# Surface Error-Resolution LLM Prompts

## Goals

- **Reduce noise** from huge "initial" LLM prompts in logs.
- **Surface the prompts/context used for error resolution** (build failures, QA/security findings, code review issues).
- **Keep behavior backward compatible** for tests and existing observability, changing only what’s necessary.

## Current Behavior

- **Prompt logging helper**: `[software_engineering_team/shared/prompt_utils.py](software_engineering_team/shared/prompt_utils.py)` defines `log_llm_prompt(log, agent_label, mode, task_hint, prompt, max_chars)`.
  - Logs lines like `"Frontend LLM prompt (mode=initial, task=..., length=71080): <truncated_prompt>"` at `INFO` with up to 4000 chars of body.
  - `mode` is currently either `"initial"` or `"problem_solving"`, but `log_llm_prompt` treats them identically.
- **Frontend agent usage**: `[software_engineering_team/frontend_agent/agent.py](software_engineering_team/frontend_agent/agent.py)`
  - `FrontendExpertAgent.run` builds `context_parts` with task, spec, architecture, existing code, and any QA/accessibility/security/code review issues.
  - When any of those issue lists are non-empty, it prepends a problem-solving header using `build_problem_solving_header(...)` and sets `mode = "problem_solving"`; otherwise `mode = "initial"`.
  - It calls `log_llm_prompt(logger, "Frontend", mode, task_hint, prompt, MAX_PROMPT_LOG_CHARS)` **once per LLM call**, before the `for attempt in range(2)` loop that may append an `empty_retry_prompt`.
- **Backend agent usage**: `[software_engineering_team/backend_agent/agent.py](software_engineering_team/backend_agent/agent.py)`
  - `BackendExpertAgent.run` follows the same pattern: build context, prepend `build_problem_solving_header` when there are QA/security/code review issues, set `mode`, and call `log_llm_prompt(logger, "Backend", mode, task_hint, prompt, MAX_PROMPT_LOG_CHARS)`.
  - Additional problem-solving LLM calls (e.g., from `_regenerate_with_issues`) also flow through `run`, so they already use `mode="problem_solving"` when issues are present.
- **Result**: logs show **all** prompts (initial and problem-solving) with large bodies. You currently see the noisy `mode=initial` prompt, and although problem-solving prompts are logged too, they are hard to distinguish and still include a lot of template/spec content.

## Proposed Changes

### 1. Change `log_llm_prompt` to treat `initial` vs `problem_solving` differently

- **File**: `[software_engineering_team/shared/prompt_utils.py](software_engineering_team/shared/prompt_utils.py)`
- **Behavior change**:
  - When `mode == "initial"`:
    - Log **metadata only**, *without* the prompt body, e.g.:
      - `"Frontend LLM prompt (mode=initial, task=..., length=71080)"` (no `: %s` and no body in the format string).
    - Keep `length=` and `mode=initial` in the log line so existing tests and searches still work.
  - When `mode == "problem_solving"`:
    - Keep logging the truncated prompt body, but explicitly mark it as problem-solving, e.g. by prefixing the message with `"Problem-solving"` or similar tag:
      - `"Frontend Problem-solving LLM prompt (mode=problem_solving, task=..., length=..., ..."`.
    - Consider slightly lowering `DEFAULT_MAX_PROMPT_LOG_CHARS` (e.g. 4000 → 2000) to keep logs focused while still capturing the header and the beginning of the issues section.
- **Rationale**:
  - This immediately stops dumping the huge initial template/spec into logs while still recording that a prompt was sent (with its size and mode).
  - It keeps full visibility for **error-resolution prompts** (where `mode="problem_solving"`), which is what you want to inspect when debugging failures.

### 2. Add explicit “problem-solving header” logging in backend and frontend agents

- **Files**:
  - `[software_engineering_team/backend_agent/agent.py](software_engineering_team/backend_agent/agent.py)`
  - `[software_engineering_team/frontend_agent/agent.py](software_engineering_team/frontend_agent/agent.py)`
- **Backend** (`BackendExpertAgent.run`):
  - After constructing `issue_summaries` and `build_problem_solving_header(...)` when `has_issues` is true, but before building the full `prompt`, log a concise, dedicated message such as:
    - One structured line summarizing counts:
      - `"Backend problem-solving context: qa_issues=%d, security_issues=%d, code_review_issues=%d"`.
    - And/or a short multi-line log of the generated header:
      - `logger.info("Backend problem-solving header for LLM:\n%s", header[:800])`.
  - This captures the **exact text** you prepend to the LLM prompt when fixing issues, without re-logging the entire template/spec.
- **Frontend** (`FrontendExpertAgent.run`):
  - Mirror the same pattern when `has_issues` is true, using the Angular-specific problem-solving header from `_ANGULAR_PROBLEM_SOLVING_INSTRUCTIONS`.
  - Log the counts and an excerpt of the header, similar to backend.
- **Rationale**:
  - These logs give you a **clear, compact view** of how the agent is being instructed to resolve errors (the “problem-solving mode” header) without noise from the rest of the prompt body.
  - They are easy to grep (e.g., for `"problem-solving header"` or `"problem-solving context"`) and directly tied to the moments when the agent is responding to failures and review feedback.

### 3. Keep existing tests working and add focused coverage

- **File**: `[software_engineering_team/tests/test_prompt_utils.py](software_engineering_team/tests/test_prompt_utils.py)`
- **Update expectations**:
  - Ensure the existing tests still pass by keeping substrings they assert on:
    - `"TestAgent LLM prompt"`, `"mode=initial"`, and `"length="` should still appear in the initial-mode log line even though the body is no longer logged.
    - The `problem_solving` truncation test should still see `"truncated"` and `"total_length=..."` in the `mode="problem_solving"` path.
- **Add tests** (if desired for extra safety):
  - A test that confirms **initial-mode logs do not contain the prompt body** substring.
  - Optional tests that verify the new backend/frontend "problem-solving header" logs are emitted when issue lists are non-empty (using `caplog`).

### 4. How you’ll consume the new logs

- After changes:
  - **To see error-resolution prompts**:
    - Search logs for `"mode=problem_solving"` or for the new tag you choose (e.g. `"Problem-solving LLM prompt"`).
    - These entries contain the truncated **full prompt string**, which now includes the problem-solving header and, at least in part, the issues and instructions passed to the LLM.
  - **To see the high-signal instructions for error fixing** without wading through prompt bodies:
    - Search for `"problem-solving header for LLM"` or `"problem-solving context"` from backend/frontend agents.
    - These logs show the constructed header and summarized issue counts, i.e. the **exact wording** the agents use to tell the LLM how to fix errors.
  - **To confirm initial prompts without noise**:
    - Search for `"mode=initial"` to confirm that a prompt was sent and its size, without seeing any of the giant template/spec content.

## Todos

- **analyze-logging-helper**: Review and adjust `log_llm_prompt` so it logs metadata-only for `mode="initial"` but keeps (slightly truncated) body logging for `mode="problem_solving"`, preserving current test expectations.
- **augment-backend-logging**: In `BackendExpertAgent.run`, log a concise problem-solving header/context whenever `has_issues` is true, so backend error-resolution prompts are easily visible.
- **augment-frontend-logging**: In `FrontendExpertAgent.run`, mirror the backend changes to log the Angular problem-solving header/context when `has_issues` is true.
- **extend-tests-for-logging**: Update or add tests under `software_engineering_team/tests/` to validate the new logging behavior for initial vs problem-solving prompts and, optionally, the new problem-solving header logs.

