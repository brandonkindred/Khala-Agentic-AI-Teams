---
name: improve-agent-robustness-from-run-logs
overview: High-level improvements to the backend, frontend, and orchestration agents so they better interpret build/test failures, align with tests/specs, and avoid repeating the kinds of errors seen in the recent run logs.
todos:
  - id: analyze-agent-error-patterns
    content: Review recent logs and categorize failure patterns (build, test, parsing, domain-specific) to validate this plan against real data.
    status: completed
  - id: enhance-test-and-spec-awareness
    content: Update backend and frontend agents to read and extract expectations from relevant tests and specs before generating code.
    status: completed
  - id: implement-structured-error-parsing
    content: Extend shared command runner/log utilities to parse build and test failures into structured error objects consumed by agents.
    status: completed
  - id: add-empty-output-and-json-guards
    content: Harden agents against empty code outputs and JSON parsing failures by adding re-prompt and repair strategies.
    status: completed
  - id: improve-orchestrator-memory
    content: Modify the orchestrator to feed previous failure summaries into subsequent agent runs and cap repeated retries.
    status: completed
  - id: add-domain-guardrails
    content: Introduce domain-specific guardrails for database schema, authentication middleware, and frontend routing/component resolution.
    status: completed
  - id: tighten-qa-security-dbc-integration
    content: Shift key QA/security/DBC rules into primary agent prompts so they are respected in the first implementation pass.
    status: completed
  - id: add-observability-for-failures
    content: Standardize logging and metrics so recurring failure classes can be tracked and used to tune agent behavior over time.
    status: completed
isProject: false
---

## Improve Agents Based on Recent Run Logs

### 1. Make agents test- and spec-aware up front

- **Read tests before coding**: For each task, the backend and frontend agents should explicitly read relevant test files (for example, `backend/tests/test_models.py`, `backend/tests/test_auth_middleware.py`, and `frontend` routing/component tests) and extract expectations (symbols to export like `Base`, required tables like `api_tokens`, expected routes/components like `TaskFormComponent`).
- **Derive invariants from tests/spec**: Convert those expectations into a short, explicit checklist (e.g., "`app.database` must export `Base`", "`ApiToken` table must exist in metadata", "`./components/task-form/task-form.component` must resolve"). Keep this checklist in the agent's prompt context so that generated changes are validated against it before writing files.

### 2. Stronger build/test failure understanding and recovery

- **Parse failures structurally**: Enhance the shared error-handling layer (for example, in `[software_engineering_team/shared/command_runner.py](software_engineering_team/shared/command_runner.py)` and any log-parsing utilities) to normalize `pytest`, `ng build`, and SQLAlchemy errors into structured objects (error type, file, line, probable cause).
- **Map failure types to playbooks**:
  - ImportErrors like `cannot import name 'Base' from 'app.database'` → suggest adding an explicit `Base` export in `app/database.py` and ensuring models inherit from it.
  - SQL errors like `no such table: api_tokens` → suggest adding the table model to metadata and ensuring `Base.metadata.create_all` is called in the app factory or test fixtures.
  - Frontend bundler errors like `Could not resolve "./components/task-form/task-form.component"` → suggest creating the missing component file or fixing the import path/route.
- **Automatic targeted remediation**: When a build/test step fails, the respective agent should automatically do one remediation iteration that focuses solely on the failure (no new unrelated features), then rerun only the relevant test/build command.

### 3. Prevent "no-op" or empty-code completions

- **Guard against zero-change outputs**: In agents that expect file edits (for example, `[software_engineering_team/frontend_agent/agent.py](software_engineering_team/frontend_agent/agent.py)`, `[software_engineering_team/backend_agent/agent.py](software_engineering_team/backend_agent/agent.py)`), add a hard check: if the proposed plan yields 0 files changed or 0 non-whitespace code characters, treat that as a failure and automatically re-prompt the LLM with the previous attempt and an explanation of why it was rejected.
- **More robust JSON/plan parsing**: Where the system currently logs `Could not parse structured JSON from LLM response`, improve parsing to:
  - Attempt tolerant JSON repair.
  - If still invalid, ask the LLM to output the plan again using a slimmer, strongly constrained schema (or function-calling if available).

### 4. Better cross-iteration memory of errors in the orchestrator

- **Feed last failure into next attempt**: Update the orchestrator logic in `[software_engineering_team/orchestrator.py](software_engineering_team/orchestrator.py)` to always pass the previous build/test failure summary into the next agent invocation as explicit context (e.g., "Previous `ng build` failed with unresolved import for `./components/task-form/task-form.component`.").
- **Cap pointless retries**: If the same error repeats N times (e.g., 2–3), the orchestrator should:
  - Escalate by instructing the agent to focus exclusively on that error and to show a concrete diff to fix it.
  - Optionally downgrade the task or flag it for human review instead of looping.

### 5. Domain-specific guardrails for common patterns

- **Database/model changes**:
  - When new models are added, verify they are imported into the main `models/__init__.py` and included in metadata.
  - Ensure the app startup/tests path that creates the database always runs `Base.metadata.create_all(bind=engine)` using the same `Base` used by models, preventing `no such table` at runtime.
- **Authentication middleware**:
  - Require that any new middleware has accompanying tests (or matches existing tests) verifying success and failure paths.
  - Add a pre-merge checklist ensuring the middleware handles missing DB schema gracefully (e.g., explicit error message or initialization step) rather than raising raw `OperationalError`.
- **Frontend routing/components**:
  - Enforce that any new route using `./components/...` is validated by checking that the referenced file exists and exports the expected symbol before committing.

### 6. Improve QA/Security/DBC integration so they catch issues earlier

- **Shift left QA and security rules**: Incorporate key QA and security rules (hash cost factor sanity, validation for multi-tenancy fields, indexes on frequently queried columns) into the main backend agent prompt, instead of letting only the QA/Security agents discover them after the fact.
- **DBC comments as constraints, not post-hoc docs**: When the DBC agent adds Design by Contract comments, those invariants (e.g., tenant isolation, non-null constraints) should be summarized and fed back into future backend tasks as hard requirements.

### 7. Observability and telemetry for agent failures

- **Tag and aggregate failure classes**: Standardize log messages (including those in `[software_engineering_team/shared/llm.py](software_engineering_team/shared/llm.py)`) so you can track how often each class of error occurs (empty completion, JSON parsing, missing table, unresolved import, etc.).
- **Use metrics to tune prompts**: Periodically review which failure types are most frequent and refine agent prompts and remediation playbooks accordingly.

These changes focus on making each agent more aware of tests/specs, better at understanding concrete failure signals, and more disciplined in how they iterate on fixes, which should directly reduce the kinds of errors and repeated failures seen in the provided logs.