---
name: fix-project-planning-failures
overview: Identify and fix the root cause of project planning failures in the orchestrator, and harden the planning phase so it never silently skips and can gracefully recover from errors.
todos:
  - id: add-serialization-helper
    content: Add a Pydantic-version-agnostic helper to convert models (including ProjectOverview) to dicts and wire it into orchestrator planning serialization.
    status: completed
  - id: implement-fallback-overview
    content: Implement a fallback ProjectOverview builder from ProductRequirements and integrate it into the project planning path in the orchestrator.
    status: completed
  - id: tighten-orchestrator-error-handling
    content: Refine orchestrator planning error handling to use the helper, prefer recovery paths, and only fail the job when both planning and fallback fail.
    status: completed
  - id: ensure-downstream-overview-usage
    content: Update architecture and tech-lead inputs to consistently receive a non-null project_overview dict when the job continues.
    status: completed
  - id: add-planning-robustness-tests
    content: Add and update tests to cover serialization compatibility, fallback overview behavior, and orchestrator responses to planning failures.
    status: completed
isProject: false
---

## Fix project planning failures and make planning robust

### Current behavior and root cause

- **Orchestrator planning step**
  - The orchestrator invokes the Project Planning agent in `[software_engineering_team/orchestrator.py](software_engineering_team/orchestrator.py)` inside `run_orchestrator`:
    - It builds `ProjectPlanningInput` (requirements, spec, repo summary).
    - Calls `project_planning_agent.run(pp_input)`.
    - Then does `project_overview = pp_output.overview.model_dump()`.
    - If any exception is raised in this block, it logs `"Project planning failed (continuing without overview): %s"` and proceeds with `project_overview = None`.
- **Project planning models**
  - `ProjectOverview` is defined in `[software_engineering_team/project_planning_agent/models.py](software_engineering_team/project_planning_agent/models.py)` as a `pydantic.BaseModel`.
  - Other related models: `Milestone`, `RiskItem`, `ProjectPlanningInput`, `ProjectPlanningOutput`.
- **Downstream consumers**
  - `ArchitectureInput.project_overview` in `[software_engineering_team/architecture_agent/models.py](software_engineering_team/architecture_agent/models.py)` is typed as `Optional[Dict[str, Any]]`, so the orchestrator is expected to pass a **dict**, not a Pydantic model.
  - The Tech Lead input (in `tech_lead_agent.models`) also receives `project_overview` and expects a serializable structure.
- **Root cause of the observed error**
  - The error `'ProjectOverview' object has no attribute 'model_dump'` indicates that Pydantic v1 is active:
    - In Pydantic v1, models expose `.dict()`; `.model_dump()` is a Pydantic v2 method.
    - The orchestrator calls `pp_output.overview.model_dump()` **without** an `hasattr(..., "model_dump")` guard, unlike other parts of the codebase that support both v1 and v2.
  - When this `AttributeError` is raised, the orchestrator logs the warning and continues without a project overview, effectively **skipping the planning phase** from the perspective of downstream agents.

### Design goals

- **Never skip planning phases**
  - Ensure `Architecture` and `Tech Lead` always receive some form of `project_overview` (either the LLM-generated one or a deterministic fallback), unless the job is explicitly marked as failed.
- **Graceful recovery on planning failures**
  - Handle known, recoverable errors (like `AttributeError` on `.model_dump`) in-place and retry with a safe alternative (e.g., `.dict()`).
  - For harder failures (LLM or parsing issues), generate a **fallback ProjectOverview** from `ProductRequirements` without abandoning planning.
- **Maintain Pydantic v1/v2 compatibility**
  - Centralize the logic for turning models into plain dicts so future model changes do not reintroduce subtle version issues.
- **Improve observability**
  - Make logs and tests clearly show when planning used a fallback path, and assert that downstream components still get a non-null overview.

### Proposed changes

#### 1. Introduce a version-agnostic serialization helper

- **Add helper**
  - Implement a small utility function (either in a shared helper such as `[software_engineering_team/shared/models.py](software_engineering_team/shared/models.py)` or a new helper module) that safely converts Pydantic models (v1/v2) to plain dicts:
    - If object has `model_dump()`, use it.
    - Else, if it has `.dict()`, use that.
    - Else, fall back to `dataclasses.asdict` or `vars()` as needed.
  - This helper should be reusable in places that currently have repeated patterns like `x.model_dump() if hasattr(x, "model_dump") else x.dict()`.
- **Adopt helper where it matters for planning**
  - Use this helper for `ProjectOverview` serialization in the orchestrator first (the highest priority), and optionally refactor other call sites (e.g., QA/security issue lists) in a later pass.

#### 2. Fix project overview serialization in the orchestrator

- **Update planning block in `run_orchestrator**` (in `orchestrator.py`):
  - Replace:
    - `project_overview = pp_output.overview.model_dump()`
  - With logic that:
    - Uses the new serialization helper to obtain a dict from `pp_output.overview` in a Pydantic-version-agnostic way.
    - Catches `AttributeError` from `.model_dump`/`.dict` if needed and still attempts fallback paths.
- **Preserve plan writing**
  - Keep the `write_project_overview_plan(path, pp_output.overview)` call as-is (it expects the Pydantic model), but guard it independently so a failure to write the markdown file cannot poison the whole planning step.
- **Stronger error handling semantics**
  - Narrow the exception handling around project planning so that:
    - Serialization issues (like `AttributeError`) are handled as **recoverable** and do not trigger the `"Project planning failed (continuing without overview)"` path.
    - Only genuinely unrecoverable planning errors (e.g., repeated LLM failures or completely invalid JSON) fall through to the broader recovery logic described below.

#### 3. Add deterministic fallback planning for hard failures

- **Fallback ProjectOverview construction**
  - When the planning agent raises a non-rate-limit exception that cannot be easily recovered (e.g., JSON schema mismatch), build a simple `ProjectOverview` directly from `requirements`:
    - `primary_goal`: use `requirements.title` or a short summary from `requirements.description`.
    - `secondary_goals`: optionally include key acceptance criteria or constraints.
    - `milestones`: generate 1–3 coarse milestones, e.g. `"M1: Foundational backend & data"`, `"M2: Frontend & UX"`, `"M3: Hardening & polish"`.
    - `risk_items`: stub list mentioning generic risks (e.g., ambiguity in spec, performance, security).
    - `delivery_strategy`: a generic but explicit strategy (e.g., "vertical slices" or "backend-first plus incremental UI").
  - Implement this fallback as a pure-Python function in the project planning module (e.g., `build_fallback_overview_from_requirements(requirements: ProductRequirements) -> ProjectOverview`).
- **Integrate fallback into orchestrator**
  - In the `project_planning_agent` block of `run_orchestrator`:
    - On generic `Exception` (excluding `LLMRateLimitError`, which is already handled specially), **attempt** to build a fallback `ProjectOverview` using the above function.
    - Serialize the fallback into `project_overview` dict and continue the pipeline.
    - Log clearly that the fallback path was used, including the original error.
    - Only if both the LLM-based planning and the fallback construction fail should the orchestrator:
      - Log a high-severity error.
      - Mark the job failed (or at minimum avoid claiming success at the end) instead of silently proceeding without any planning output.

#### 4. Ensure downstream agents always see a non-null overview

- **ArchitectureInput**
  - Confirm that `ArchitectureInput.project_overview` is still populated with a dict (LLM-generated or fallback) when planning succeeds or recovers.
  - Adjust the orchestrator so `project_overview` is guaranteed non-null when invoking `ArchitectureInput` unless the job is being hard-failed.
- **Tech Lead input**
  - In `tech_lead_agent` input construction (in `orchestrator.py`), ensure `project_overview` always receives the same dict structure passed to `ArchitectureInput`.
  - If necessary, align the Tech Lead models’ type hints to accept the dict form and document that they may receive either rich or fallback overviews.
- **Behavior when planning truly impossible**
  - Define a clear policy: if even fallback construction fails, the orchestrator should **stop the job early** with `JOB_STATUS_FAILED` and a clear error message, rather than running architecture/tech-lead without any planning.

#### 5. Improve logging and observability for planning

- **Make planning status explicit**
  - Refine logging around planning to distinguish:
    - `Project Planning: success (LLM-based)`
    - `Project Planning: success via fallback overview (LLM failed: <reason>)`
    - `Project Planning: hard failure (no overview available)`
  - Include the overview type (LLM vs fallback) in a field written to the job store if appropriate, so APIs/clients can surface it.
- **Update existing warning message**
  - Replace the current generic warning `"Project planning failed (continuing without overview)"` with messaging that reflects the new semantics:
    - For recoverable errors: log that the issue was recovered and a fallback overview is being used.
    - For unrecoverable errors: log that the orchestrator is failing the job specifically because planning could not be completed.

#### 6. Strengthen tests around planning robustness

- **Unit tests for serialization helper**
  - Add tests (e.g., in `software_engineering_team/tests/test_planning_validation.py` or a new planning test module) that:
    - Verify the helper returns correct dicts for Pydantic v1 models (with `.dict()`).
    - Verify it also works for any Pydantic v2 models (with `.model_dump()`) present in the codebase.
- **ProjectPlanningAgent and fallback tests**
  - Add tests for `ProjectPlanningAgent.run` to ensure it returns `ProjectPlanningOutput` with a valid `ProjectOverview` given typical JSON from the LLM.
  - Add tests for the new `build_fallback_overview_from_requirements` to assert the structure and fields of the generated overview for a sample `ProductRequirements`.
- **Orchestrator behavior tests**
  - Extend or add orchestrator tests (e.g., in `[software_engineering_team/tests/test_orchestrator.py](software_engineering_team/tests/test_orchestrator.py)`) to cover:
    - **Happy path**: planning succeeds, `ArchitectureInput.project_overview` is non-null and corresponds to the LLM-produced overview.
    - **Serialization failure**: simulate a `ProjectOverview` instance without `.model_dump()` and assert that the orchestrator still supplies a dict to downstream agents (using the helper), without logging a planning failure.
    - **Hard planning failure**: mock the planning agent to raise a generic exception, assert that the fallback overview is built, and that architecture and Tech Lead receive it.
    - **Total failure**: mock both planning and fallback to fail and assert that the job is marked failed and that architecture is **not** run.

### High-level flow after changes (Mermaid)

```mermaid
flowchart TD
  specRepo[Spec & Repo] --> requirementsParser[parse_spec_*]
  requirementsParser --> projectPlanning[ProjectPlanningAgent.run]
  projectPlanning -->|LLM success| overviewModel[ProjectOverview (Pydantic)]
  projectPlanning -->|LLM error| fallbackBuilder[build_fallback_overview]
  fallbackBuilder --> overviewModel
  overviewModel --> serializer[model_to_dict helper]
  serializer --> overviewDict[project_overview dict]
  overviewDict --> architectureAgent[ArchitectureAgent]
  overviewDict --> techLeadAgent[TechLeadAgent]
  projectPlanning -->|fatal error & fallback fail| failJob[Mark job failed]
```



### Summary of intended outcome

- **Root cause addressed**: eliminate the `model_dump`/Pydantic version mismatch by using a safe serialization helper.
- **Planning robustness**: ensure project planning never silently disappears; downstream agents always receive an overview or the job is clearly failed.
- **Graceful recovery**: introduce deterministic fallback planning so transient LLM or schema issues don’t cause the system to abandon the planning phase.
- **Confidence via tests**: expand tests to lock in the new behavior and prevent regressions as the planning pipeline evolves.

