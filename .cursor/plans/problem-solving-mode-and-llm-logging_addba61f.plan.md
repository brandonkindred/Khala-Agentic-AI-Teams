---
name: problem-solving-mode-and-llm-logging
overview: Introduce a structured problem-solving mode for software engineering agents on build/test failures and log the LLM request prompts used, starting with backend and frontend agents while keeping the design extensible.
todos: []
isProject: false
---

## Goal

Enhance the backend and frontend software engineering agents so that, when downstream commands (builds/tests) fail, they enter an explicit **problem-solving mode** rather than simply regenerating with the same generic prompt, and ensure that the **LLM request prompts are logged** (truncated for safety). Design the changes so other agents can opt into the same behavior later.

## Key Design Decisions

- **Problem-solving trigger**: Activate problem-solving mode whenever an agent is called with one or more concrete issues (build/test failures translated into `code_review_issues`, `qa_issues`, `security_issues`, or `accessibility_issues`).
- **Prompt shaping**: When in problem-solving mode, prepend a clear instruction block emphasizing root-cause analysis, minimal and localized changes, and direct use of the provided error output to drive fixes.
- **Prompt logging**: Log the final assembled prompt string right before each LLM call inside the backend and frontend agents, truncated to a safe length (e.g. 4000 characters) and tagged with agent, task, and mode (initial vs problem-solving).
- **Extensibility**: Keep the problem-solving / logging utilities small and generic so they can be reused by other agents (QA, security, code review, tech lead) later without structural changes.

## Files to Inspect and Update

- **Backend agent**
  - Read and modify `[software_engineering_team/backend_agent/agent.py](software_engineering_team/backend_agent/agent.py)`
    - In `BackendExpertAgent.run`, detect when any of `qa_issues`, `security_issues`, or `code_review_issues` are non-empty.
    - Build a `problem_solving_header` string that:
      - States that the agent is in **PROBLEM_SOLVING MODE**.
      - Summarizes the kinds of issues present (e.g. build error, test failure, QA bug, security finding).
      - Explicitly instructs the model to: (1) identify likely root cause from the error details, (2) propose minimal, targeted code edits, (3) keep passing tests/features intact, (4) avoid broad rewrites, and (5) focus on resolving the provided issues before adding new features.
    - Prepend this header to `context_parts` only when issues are present, so initial implementations remain as-is.
    - Before calling `self.llm.complete_json(...)`,
      - Assemble the final `prompt` string as done today.
      - Log a line like `"Backend LLM prompt (mode=problem_solving|initial, task=..., length=N): <truncated_prompt>"` using `logger.info`, truncating the prompt to a reasonable maximum length (e.g. first 4000 characters plus an indication if truncated).
- **Frontend agent**
  - Read and modify `[software_engineering_team/frontend_agent/agent.py](software_engineering_team/frontend_agent/agent.py)`
    - In `FrontendExpertAgent.run`, detect when any of `qa_issues`, `security_issues`, `accessibility_issues`, or `code_review_issues` are non-empty.
    - Add an analogous problem-solving header for frontend with Angular-specific guidance, for example:
      - Use the provided compiler/test errors (e.g. NG8002, TS errors) to locate the offending components/templates.
      - Require minimal, localized edits rather than recreating large portions of the app.
      - Preserve existing working routes, DI configuration, and forms; only adjust what’s necessary to resolve the errors and issues.
    - Prepend this header to `context_parts` when issues are present.
    - Log the final prompt before `self.llm.complete_json(...)` similarly to the backend agent, including mode, task ID/description snippet, and truncated content.

## Aligning with Existing Error Handling

- **Backend build/test failures**
  - In `BackendExpertAgent.run_workflow` (in `backend_agent/agent.py`), build/test failures are already translated into `code_review_issues` via `_build_code_review_issues_for_build_failure` and fed back through `_regenerate_with_issues`.
  - After adding problem-solving mode in `run`, these retries automatically gain the richer, issue-focused prompting, without changing `run_workflow` control flow.
- **Frontend build failures (e.g. NG8002)**
  - In `FrontendExpertAgent.run_workflow`, failed `ng build` calls currently create a synthetic `code_review_issues` entry (`"ng build failed: ..."`) and then re-enter the loop.
  - With the new problem-solving header in `run`, these re-tries will now:
    - Explicitly call out the Angular error (e.g. `Can't bind to 'formGroup'...`).
    - Instruct the LLM to: (a) identify which component/template is missing `ReactiveFormsModule` or has an invalid binding, and (b) produce the minimal fixes (imports, bindings) to resolve it.

## Logging Strategy

- **Location of logging**
  - Implement logging at the agent level (in `BackendExpertAgent.run` and `FrontendExpertAgent.run`) so each call to `complete_json` is accompanied by a clear, human-readable prompt log.
  - Optionally add small helper functions (module-local) to centralize prompt truncation and tagging, e.g. `_log_llm_prompt(logger, agent_name, mode, task_hint, prompt)`.
- **Content and safety**
  - Log the **full assembled prompt string**, but truncate it to a maximum length (e.g. 4000 characters) to avoid excessive log size.
  - Add a suffix like `"... [truncated, total_length=N]"` when truncation occurs.
  - Keep logs at `INFO` level for now so they are visible in normal server logs; if noise becomes an issue later, they can be downgraded to `DEBUG` without code changes.

## Extensibility Hooks

- Optionally, add a small utility in a shared module (e.g. `software_engineering_team/shared/prompt_utils.py`) that provides:
  - A `build_problem_solving_header(issue_summaries: dict, domain_hint: str) -> str` function used by both backend and frontend agents.
  - A `log_llm_prompt(logger, agent_label, mode, task_hint, prompt, max_chars=4000)` helper.
- Future agents (QA, Security, Code Review, Tech Lead, Integration, Acceptance Verifier) can:
  - Call the shared helpers to enter a similar problem-solving mode when they are invoked specifically to diagnose or iteratively fix issues.
  - Gain consistent prompt logging behavior by using the common `log_llm_prompt` helper.

## Testing & Verification

- **Unit / integration tests**
  - Extend or add tests under `software_engineering_team/tests/` to verify:
    - Backend and frontend agents, when invoked with non-empty issues arrays, prepend the problem-solving header into the prompt (can be tested by monkeypatching a `DummyLLMClient` that captures the `prompt` argument).
    - The logging calls are made and include the expected tags (`mode`, agent name, truncation indicator when applicable).
    - Existing workflows (`run_workflow` for backend and frontend) still pass their current tests, including build failure handling for backend tests and Angular NG8002 scenarios.
- **Manual verification**
  - Run a representative job that triggers:
    - A backend test failure (like the `Tenant` validation error in the provided logs) and
    - A frontend Angular `NG8002` build failure.
  - Confirm in the server logs that:
    - The subsequent LLM calls are preceded by problem-solving headers that refer to the specific errors.
    - The LLM prompts are logged (truncated) with mode and task details.

## Todos

- **backend-problem-mode**: Implement problem-solving mode header construction and prompt logging in `BackendExpertAgent.run`.
- **frontend-problem-mode**: Implement problem-solving mode header construction and prompt logging in `FrontendExpertAgent.run`.
- **shared-prompt-utils**: (Optional) Extract common helpers for problem-solving headers and prompt logging into a small shared utility module for future reuse.
- **tests-update**: Add/extend tests to cover problem-solving mode behavior and prompt logging for backend and frontend agents, and run the existing test suite to ensure no regressions.

