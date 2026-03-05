---
name: Coding agents planning phase
overview: "Add a per-task planning step for the backend and frontend coding agents that runs before the first code generation: review codebase, produce a structured plan (feature intent, what to change, algorithms/data structures, tests needed), then have the coding agents use that plan to perform the implementation—output must realize the plan's what_changes and tests_needed. Implementation can live inside each coding agent (two-phase) or in a dedicated planning subagent."
todos:
  - id: shared-task-plan-model
    content: Add shared TaskPlan model (feature_intent, what_changes, algorithms_data_structures, tests_needed) with to_markdown() in shared/task_plan.py or each agent's models
    status: completed
  - id: backend-input-task-plan
    content: "Add task_plan: Optional[str] = None to BackendInput in backend_agent/models.py"
    status: completed
  - id: backend-planning-prompt
    content: Add BACKEND_PLANNING_PROMPT in backend_agent/prompts.py (task + codebase + spec → JSON with four plan keys)
    status: completed
  - id: backend-prompt-follow-plan
    content: In BACKEND_PROMPT, add rule that when Implementation plan is present the model must implement according to it (files must realize what_changes and tests_needed; use stated algorithms/data structures)
    status: completed
  - id: backend-code-prompt-plan-instruction
    content: In backend run(), when injecting task_plan, add explicit instruction text that implementation must follow the plan (e.g. implement strictly according to plan; output must realize every what_changes and tests_needed)
    status: completed
  - id: backend-plan-task-method
    content: "Implement _plan_task() in backend agent: build context, call LLM with planning prompt, parse JSON to plan, return serialized plan text"
    status: completed
  - id: backend-workflow-call-plan
    content: In run_workflow(), before clarification loop and first run(), call _plan_task() when no issues; pass plan into first BackendInput(..., task_plan=plan_text)
    status: completed
  - id: backend-regenerate-no-plan
    content: Ensure _regenerate_with_issues() and all review-loop BackendInput builds do not pass task_plan (planning only for initial gen)
    status: completed
  - id: backend-run-inject-plan
    content: In run(), when input_data.task_plan is set, prepend Implementation plan near top of context (after problem-solving header) so model uses it to perform implementation
    status: completed
  - id: frontend-input-task-plan
    content: "Add task_plan: Optional[str] = None to FrontendInput in frontend_team/feature_agent/models.py"
    status: completed
  - id: frontend-planning-prompt
    content: Add FRONTEND_PLANNING_PROMPT in frontend_team/feature_agent/prompts.py (task + codebase + spec + optional API context → JSON)
    status: completed
  - id: frontend-prompt-follow-plan
    content: In FRONTEND_PROMPT, add rule that when Implementation plan is present the model must implement according to it (files/components must realize what_changes and tests_needed)
    status: completed
  - id: frontend-code-prompt-plan-instruction
    content: In frontend run(), when injecting task_plan, add explicit instruction that implementation must follow the plan (realize what_changes and tests_needed; do not deviate unless task contradicts plan)
    status: completed
  - id: frontend-plan-task-method
    content: "Implement _plan_task() in frontend feature agent: build context (incl. api_endpoints if available), call LLM, parse JSON, return plan text"
    status: completed
  - id: frontend-workflow-call-plan
    content: In run_workflow(), before first self.run(FrontendInput(...)), call _plan_task() when no qa_issues/security_issues/a11y_issues/code_review_issues; pass plan into first FrontendInput
    status: completed
  - id: frontend-loop-no-replan
    content: Ensure subsequent loop iterations (fixing issues) do not call _plan_task() and do not pass task_plan
    status: completed
  - id: frontend-run-inject-plan
    content: In run(), when input_data.task_plan is set, prepend Implementation plan near top of context so model uses it to perform implementation
    status: completed
  - id: planning-clarification
    content: "Handle planning-step needs_clarification: either re-run planning after Tech Lead refines task, or skip plan for that round; document choice in code"
    status: cancelled
  - id: planning-repo-setup
    content: Ensure planning prompt allows minimal/short plans for repo-setup or trivial tasks and still returns valid JSON
    status: completed
  - id: planning-token-budget
    content: Keep planning prompt output instructions short (e.g. few hundred words max) and use existing_code truncation already in workflow
    status: completed
  - id: optional-persist-backend
    content: "Optional: In backend run_workflow(), after _plan_task(), write plan to plan/backend_task_<task_id>.md if plan dir exists (repo_path.parent/plan or repo_path/plan)"
    status: completed
  - id: optional-persist-frontend
    content: "Optional: In frontend run_workflow(), after _plan_task(), write plan to plan/frontend_task_<task_id>.md if plan dir exists"
    status: completed
  - id: frontend-orchestrator-plan
    content: In frontend_team/orchestrator.py, run planning once at start of implementation phase and pass task_plan into first FrontendInput for consistency with feature_agent.run_workflow
    status: completed
  - id: tests-backend-planning
    content: Add tests for backend _plan_task() (mock LLM, assert plan parsed and passed to run); test run() injects task_plan and follow-plan instruction into prompt when present
    status: completed
  - id: tests-frontend-planning
    content: Add tests for frontend _plan_task() and run() task_plan injection and follow-plan instruction; test run_workflow does not re-plan in review loop
    status: completed
  - id: verify-plan-drives-implementation
    content: "Verify end-to-end: code output reflects plan (e.g. test or manual check that generated files align with plan's what_changes and tests_needed when task_plan is set)"
    status: completed
  - id: readme-planning
    content: Update software_engineering_team/README.md flow section to mention per-task planning step (review codebase → plan → generate code)
    status: completed
isProject: false
---

# Per-task planning for backend and frontend coding agents

## Current behavior

- **Backend** (`[software_engineering_team/backend_agent/agent.py](software_engineering_team/backend_agent/agent.py)`): `run_workflow()` creates a feature branch, then immediately calls `self.run(BackendInput(...))` with task, requirements, spec, architecture, and existing code. No planning step; the model goes straight to generating files.
- **Frontend** (`[software_engineering_team/frontend_team/feature_agent/agent.py](software_engineering_team/frontend_team/feature_agent/agent.py)`): Same pattern—branch, then `self.run(FrontendInput(...))` in a loop with no prior planning.
- **Existing “planning”** in the repo is project-level: Tech Lead uses `BackendPlanningAgent` / `FrontendPlanningAgent` in `[planning_team](software_engineering_team/planning_team/)` to produce a **PlanningGraph** (task IDs and dependencies), not per-task implementation plans (what to change, algorithms, tests).

So the gap is **per-task implementation planning** before writing code.

## Goal

When each coding agent receives a task, before the first code generation it should:

1. **Review the codebase** (existing code is already read and passed as context; planning will use it explicitly).
2. **Produce a plan** covering:
  - **Feature intent** – what the feature is meant to achieve from the task details.
  - **What needs to change** – files/modules to add or modify.
  - **Algorithms / data structures** – choices for efficiency and correctness.
  - **Tests needed** – what to add or update to verify behavior.
3. **Use that plan to perform the implementation**: the coding agent must treat the plan as the authoritative guide. Code generation is not optional interpretation—the agent implements the task **according to** the plan: the files and modules produced must realize the plan’s “what needs to change”, the algorithms/data structures chosen in the plan must be used, and the tests written must cover “tests needed” from the plan.

Planning should run only for **initial** code generation, not in the review/fix loop (when QA, security, or code review issues are already present).

## Design options


| Approach                    | Description                                                                                                                                                                                                                   | Pros                                                                                | Cons                                                         |
| --------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| **A. In-agent (two-phase)** | Add a planning step inside `BackendExpertAgent` and `FrontendExpertAgent`: one LLM call for the plan, then pass the plan into the existing `run()` and prompt.                                                                | No new agents; minimal wiring; plan always in context for code gen.                 | Two LLM calls per task; prompt surface grows.                |
| **B. Planning subagent**    | New agent(s), e.g. `BackendTaskPlanningAgent` and `FrontendTaskPlanningAgent` (or one shared `TaskPlanningAgent` with domain). Workflow calls the subagent first, then passes the plan into `BackendInput` / `FrontendInput`. | Clear separation; reusable; easy to persist plans to `plan/` and test in isolation. | More modules; orchestrator/workflow must wire and pass plan. |


Recommendation: **Option A (in-agent)** for fewer moving parts and a single place to maintain behavior. Option B is a good follow-up if you want to persist plans to `plan/` or reuse planning elsewhere.

## Plan structure

Introduce a small, structured plan so it can be passed into the code prompt and optionally persisted:

- **feature_intent** (str): What the feature is meant to achieve.
- **what_changes** (str or list): Files/modules to add or modify; high-level change list.
- **algorithms_data_structures** (str): Key algorithmic or data-structure choices for efficiency/correctness.
- **tests_needed** (str): What unit/integration tests to add or update.

Serialize to a short markdown or bullet block for injection into the code-generation prompt (e.g. a “**Implementation plan**” section).

### Using the plan to drive implementation

The plan must **drive** implementation, not sit alongside it. Concretely:

- **Code-generation prompt**: When `task_plan` is present, the prompt must state clearly that the model must **implement the task according to the Implementation plan**: the “files” (and “code”) output must realize the plan’s *what_changes* (files/modules to add or modify), use the *algorithms_data_structures* choices described in the plan, and include the *tests_needed* (unit/integration tests) from the plan. The plan is the primary specification for what to build; the task description and requirements provide context but the plan refines them into a concrete implementation checklist.
- **Placement**: The Implementation plan block should appear near the top of the code-generation context (after any problem-solving header) so the model sees it before the rest of the task and existing code.
- **Wording**: Use explicit instructions such as “Implement the task strictly according to the Implementation plan below. Your output must realize every item under ‘What changes’ and ‘Tests needed’, and use the algorithms/data structures described. Do not deviate from the plan unless the task description explicitly contradicts it.”

This ensures the coding agents actually use the plan to perform the implementation for the provided task.

## Implementation outline

### 1. Shared plan model and prompts (optional but useful)

- **Location**: e.g. `software_engineering_team/shared/task_plan.py` (or in each agent’s `models.py`).
- **Model**: Pydantic or dataclass with the four fields above; add a `to_markdown()` (or `to_context_string()`) for the code prompt.
- **Planning prompt**: Add a prompt (backend- and frontend-specific or parameterized by domain) that:
  - Takes: task description, requirements, existing code (truncated), spec snippet, architecture.
  - Asks for: feature intent, what to change, algorithms/data structures, tests needed.
  - Requests JSON output with those four keys for reliable parsing.

### 2. Backend agent

- **Models** (`[backend_agent/models.py](software_engineering_team/backend_agent/models.py)`): Add optional `task_plan: Optional[str] = None` to `BackendInput` (plan text to inject into the code prompt).
- **Agent** (`[backend_agent/agent.py](software_engineering_team/backend_agent/agent.py)`):
  - In `run_workflow()`, **before** the clarification loop and first `self.run()`: if there are no `qa_issues` / `security_issues` / `code_review_issues`, call a new internal method (e.g. `_plan_task()`) that:
    - Builds context from task, existing code (from repo), spec, architecture.
    - Calls the LLM with the planning prompt; parses JSON into the plan model; serializes to string.
  - Pass the resulting plan into the first `BackendInput(..., task_plan=plan_text)`.
  - In the review loop, when calling `_regenerate_with_issues()`, do **not** pass a plan (or pass `None`); planning is only for initial generation.
- **Prompts** (`[backend_agent/prompts.py](software_engineering_team/backend_agent/prompts.py)`): Add `BACKEND_PLANNING_PROMPT`. In the main code-generation prompt, add a clear rule: when “Implementation plan” is present, the model **must implement the task according to that plan**—output must realize the plan’s what_changes and tests_needed and use the stated algorithms/data structures; the plan is the authoritative guide for what to build.

In `run()`: when `input_data.task_plan` is present, prepend the Implementation plan near the top of the context (after any problem-solving header) with explicit wording that implementation must follow the plan (e.g. “Implement the task strictly according to the Implementation plan below. Your files output must realize every item under ‘What changes’ and ‘Tests needed’.”).

### 3. Frontend agent

- **Models** (`[frontend_team/feature_agent/models.py](software_engineering_team/frontend_team/feature_agent/models.py)`): Add optional `task_plan: Optional[str] = None` to `FrontendInput`.
- **Agent** (`[frontend_team/feature_agent/agent.py](software_engineering_team/frontend_team/feature_agent/agent.py)`):
  - In `run_workflow()`, **before** the first `self.run(FrontendInput(...))` in the loop (and only when there are no issues to fix), call a planning step (e.g. `_plan_task()`) with task, existing code, spec, architecture, and optionally API/backend context; get back plan text.
  - Pass `task_plan=plan_text` into the first `FrontendInput(...)`.
  - Do not re-plan in subsequent loop iterations when fixing QA/a11y/security/code review issues.
- **Prompts** (`[frontend_team/feature_agent/prompts.py](software_engineering_team/frontend_team/feature_agent/prompts.py)`): Add `FRONTEND_PLANNING_PROMPT`. In the main code-generation prompt, add a rule: when “Implementation plan” is present, the model **must implement the task according to that plan**—files/components must realize the plan’s what_changes and tests_needed; the plan is the authoritative guide.

In `run()`: when `input_data.task_plan` is set, prepend the Implementation plan near the top of the context with explicit instructions that implementation must follow the plan (realize what_changes and tests_needed; do not deviate unless the task contradicts the plan).

### 4. Frontend orchestrator

- `[frontend_team/orchestrator.py](software_engineering_team/frontend_team/orchestrator.py)` calls `self.feature_agent.run(FrontendInput(...))` in the implementation loop. For consistency:
  - Either run the same planning step once at the start of the implementation phase and pass `task_plan` into the first `FrontendInput`, or
  - Rely on the feature agent’s own planning when the orchestrator is not used (direct `FrontendExpertAgent.run_workflow()`). If both paths should plan, add a small shared helper (e.g. “produce task plan for frontend”) and call it from both the feature agent and the orchestrator so behavior and prompts stay aligned.

### 5. Optional: persist plans

- In `run_workflow()` (backend and frontend), after producing the plan, if a `plan/` directory exists (e.g. `repo_path.parent / "plan"` or `repo_path / "plan"` as in the README), write the plan to e.g. `plan/backend_task_<task_id>.md` or `plan/frontend_task_<task_id>.md` for traceability and debugging.

### 6. Clarification and edge cases

- **Clarification**: If the agent returns `needs_clarification` in the **planning** step, you can either (a) treat it like today and let the Tech Lead refine the task, then re-run planning, or (b) skip plan and pass no plan into the first code gen. Prefer (a) so the plan reflects the refined task.
- **Empty/repo-setup tasks**: For tasks that are mostly “setup repo / add .gitignore”, planning may be minimal; the planning prompt should allow short plans and still output valid JSON.
- **Token budget**: Planning uses existing_code (already truncated in workflows). Keep planning output short (e.g. a few hundred words) so the combined plan + code prompt stays within limits.

## Flow summary (in-agent option)

```mermaid
sequenceDiagram
  participant W as run_workflow
  participant P as _plan_task
  participant R as run
  participant LLM as LLM

  W->>W: Create feature branch
  W->>P: plan = _plan_task(task, existing_code, spec, arch)
  P->>LLM: Planning prompt (task + codebase + spec)
  LLM->>P: JSON plan
  P->>W: plan_text
  W->>R: run(BackendInput(..., task_plan=plan_text))
  R->>LLM: Code prompt = Implementation plan + task + existing_code + ...
  LLM->>R: files
  R->>W: BackendOutput
  W->>W: Write files, build, review loop (no planning)
```

The code prompt includes the Implementation plan plus explicit instructions that the model must **implement the task according to the plan** (realize what_changes and tests_needed); the plan drives what gets built.



## Files to touch (in-agent approach)

- `[software_engineering_team/backend_agent/agent.py](software_engineering_team/backend_agent/agent.py)`: Add `_plan_task()`, call it before first `run()`, pass plan into `BackendInput`; in `run()`, inject `task_plan` into context.
- `[software_engineering_team/backend_agent/models.py](software_engineering_team/backend_agent/models.py)`: Add `task_plan: Optional[str]` to `BackendInput`; optionally add a small `TaskPlan` model for parsing.
- `[software_engineering_team/backend_agent/prompts.py](software_engineering_team/backend_agent/prompts.py)`: Add `BACKEND_PLANNING_PROMPT`; mention in main prompt that “Implementation plan” must be followed when present.
- `[software_engineering_team/frontend_team/feature_agent/agent.py](software_engineering_team/frontend_team/feature_agent/agent.py)`: Same: `_plan_task()`, pass plan into first `FrontendInput`, inject plan in `run()`.
- `[software_engineering_team/frontend_team/feature_agent/models.py](software_engineering_team/frontend_team/feature_agent/models.py)`: Add `task_plan: Optional[str]` to `FrontendInput`.
- `[software_engineering_team/frontend_team/feature_agent/prompts.py](software_engineering_team/frontend_team/feature_agent/prompts.py)`: Add `FRONTEND_PLANNING_PROMPT`; document “Implementation plan” in code prompt.
- Optionally `[software_engineering_team/frontend_team/orchestrator.py](software_engineering_team/frontend_team/orchestrator.py)`: Run planning once before the implementation loop and pass `task_plan` into the first `FrontendInput` so behavior matches when using the orchestrator.

If you later switch to a **subagent** (Option B), the same plan structure and prompt content can live in a new `TaskPlanningAgent` (or backend/frontend-specific planners); the only change is that `run_workflow()` would call that agent instead of `_plan_task()` and pass its output into `BackendInput` / `FrontendInput` as today.