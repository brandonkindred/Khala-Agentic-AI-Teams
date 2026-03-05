---
name: Resolve run log warnings
overview: "Address the four warning categories from the run logs: ProjectOverview serialization (model_dump), Architecture Expert JSON/missing keys and diagrams, and Tech Lead validation failures due to missing user_story on backend tasks."
todos:
  - id: harden-model-to-dict
    content: "Harden model_to_dict in shared/models.py: try obj.model_dump(), on AttributeError or missing method fall back to obj.dict(), then to __dict__; document Pydantic v1/v2 compatibility."
    status: completed
  - id: audit-orchestrator-overview-serialization
    content: Audit orchestrator.py for any direct .model_dump() or .dict() on project overview; ensure only model_to_dict() is used for ProjectOverview and that raw overview is not passed to code expecting a dict.
    status: completed
  - id: arch-detect-raw-wrapper
    content: "In architecture_agent/agent.py: when data has only 'content' (or no 'overview'), treat as parse failure; build fully synthetic architecture from requirements (overview, components, document, all required diagrams) and return it without a second LLM call."
    status: completed
  - id: arch-optional-llm-retry
    content: "Optional – In shared/llm.py: when _extract_json returns the raw wrapper, consider one retry with a short 'output only valid JSON' reminder before returning wrapper (to reduce architecture parse failures)."
    status: cancelled
  - id: arch-reduce-diagram-warning
    content: "In architecture_agent/agent.py: reduce 'diagrams missing or empty for keys' noise – either log at DEBUG or replace with single INFO 'Backfilling N missing diagram(s)' when using _default_diagram()."
    status: completed
  - id: arch-reduce-missing-keys-warning
    content: "Optional – In architecture_agent/agent.py: when we have fallbacks for missing keys, downgrade 'LLM response missing keys' to DEBUG or log once with backfill summary."
    status: completed
  - id: backend-prompt-user-story
    content: "In backend_planning_agent/prompts.py: add user_story to node schema (or metadata); add instruction that each backend task node must have user_story in format 'As a [role], I want [goal] so that [benefit]'."
    status: completed
  - id: frontend-prompt-user-story
    content: "In frontend_planning_agent prompts: add user_story to node schema and same instruction as backend for frontend task nodes."
    status: completed
  - id: backend-parse-user-story
    content: "In backend_planning_agent/agent.py (_parse_graph_from_llm_output): set metadata['user_story'] from n.get('user_story') or (n.get('metadata') or {}).get('user_story', '') when building PlanningNode."
    status: completed
  - id: frontend-parse-user-story
    content: "In frontend_planning_agent/agent.py: same as backend – when parsing nodes, copy user_story into node metadata so compile_graph_to_assignment receives it."
    status: completed
  - id: planning-graph-backfill-user-story
    content: "In planning/planning_graph.py (compile_graph_to_assignment): when building Task for backend/frontend, if node.metadata.get('user_story') is empty, set default e.g. 'As a developer, I want {summary} so that the system meets the requirements.'"
    status: completed
  - id: add-tests-model-to-dict
    content: Add or extend tests in shared (e.g. test_planning_robustness or test_llm) for model_to_dict with Pydantic v1-style object (no model_dump, only dict) and for AttributeError fallback.
    status: completed
  - id: add-tests-user-story-backfill
    content: Add test that compile_graph_to_assignment produces tasks with non-empty user_story for backend/frontend when node metadata has no user_story (backfill default).
    status: completed
  - id: optional-log-message-alignment
    content: "Optional – In orchestrator.py: standardize project planning failure log messages (e.g. 'Project planning failed, attempting fallback' vs 'continuing without overview') so logs are clear and consistent."
    status: completed
isProject: false
---

# Plan: Resolve Warnings in Run Logs

From the terminal output (lines 958–991), there are four distinct warning sources to fix:

1. **Project planning failed (continuing without overview): 'ProjectOverview' object has no attribute 'model_dump'**
2. **Could not parse structured JSON from LLM response** (architecture) and **Architecture Expert: LLM response missing keys / diagrams missing or empty**
3. **Tech Lead planning pipeline validation failed: Missing 'user_story' for backend task** (and similar for other backend tasks)

---

## 1. ProjectOverview serialization (`model_dump`)

**Cause:** [orchestrator.py](software_engineering_team/orchestrator.py) (line 495) calls `model_to_dict(pp_output.overview)` after project planning succeeds. [model_to_dict](software_engineering_team/shared/models.py) in [shared/models.py](software_engineering_team/shared/models.py) (lines 164–177) uses `hasattr(obj, "model_dump")` then `obj.model_dump()`. Under Pydantic v1, `BaseModel` has `.dict()` but not `.model_dump()`. If the runtime model exposes `model_dump` in a way that fails when called (e.g. partial implementation or proxy), or if `hasattr` is True but the call raises, the exception propagates and is logged.

**Fix:**

- **Harden `model_to_dict**` in [shared/models.py](software_engineering_team/shared/models.py): Prefer `.model_dump()` when present, and on `AttributeError` (or when the method is missing) fall back to `.dict()`, then to `__dict__`. This keeps compatibility with both Pydantic v1 and v2 and avoids crashes on edge cases.
- **Orchestrator:** Keep using `model_to_dict` only (no direct `.model_dump()` on overview). Ensure the success path never passes a raw `ProjectOverview` to code that might call `.model_dump()` on it. [write_project_overview_plan](software_engineering_team/shared/development_plan_writer.py) already uses attribute access only; no change needed there.

---

## 2. Architecture Expert: JSON parse failure and missing keys/diagrams

**Cause:**

- **JSON parse:** [OllamaLLMClient._extract_json](software_engineering_team/shared/llm.py) (around 655–658) logs the warning and returns `{"content": text.strip()}` when JSON parsing fails. The architecture agent then receives an object without the expected keys.
- **Missing keys/diagrams:** [architecture_agent/agent.py](software_engineering_team/architecture_agent/agent.py) (59–64, 183–188) logs when required keys or diagram keys are missing. The agent already backfills overview, document, and diagrams with defaults, so execution continues but warnings are noisy.

**Fixes:**

- **Improve Architecture JSON robustness (optional but recommended):**
  - In [architecture_agent/agent.py](software_engineering_team/architecture_agent/agent.py): When `data` is effectively the raw wrapper (e.g. only `"content"` key and no `"overview"`), treat it as parse failure: log once, then build a fully synthetic architecture from requirements (overview, components, document, all required diagrams) so downstream agents still get a valid structure without relying on a second LLM call.
  - Alternatively or in addition: in [shared/llm.py](software_engineering_team/shared/llm.py), consider a single retry with a short “output only valid JSON” reminder when the first response fails to parse (to reduce parse failures without changing architecture agent contract).
- **Reduce diagram warning noise:** When the agent backfills missing diagram keys with `_default_diagram()` (lines 189–194), either:
  - Downgrade the “diagrams missing or empty for keys: …” log to DEBUG, or
  - Log it once at INFO as “Backfilling N missing diagram(s)” instead of listing every key. This keeps the behavior (defaults) but avoids alarming logs when backfill is working as intended.

---

## 3. Tech Lead planning pipeline validation: missing `user_story`

**Cause:** [shared/task_validation.py](software_engineering_team/shared/task_validation.py) (58–61) requires `user_story` for backend and frontend tasks. Tasks are built in [planning/planning_graph.py](software_engineering_team/planning/planning_graph.py) (around 276) with `user_story=node.metadata.get("user_story", "")`. Backend (and frontend) planning agents do not currently ask the LLM to populate `user_story` in node metadata, so many backend nodes have no `user_story` and the resulting tasks fail validation. [tech_lead_agent/agent.py](software_engineering_team/tech_lead_agent/agent.py) (377–380) logs “Tech Lead planning pipeline validation failed” and returns `None`, triggering fallback to monolithic task generation.

**Fixes:**

- **Source (preferred):** Require `user_story` in planner output so it flows into `metadata`:
  - In [backend_planning_agent/prompts.py](software_engineering_team/backend_planning_agent/prompts.py): Extend the node schema to include a `user_story` field (or require it inside `metadata`). Add a short instruction: each backend task node should have a user story in the form “As a [role], I want [goal] so that [benefit].”
  - In [frontend_planning_agent](software_engineering_team/frontend_planning_agent): Make the same change so frontend task nodes include `user_story` (or `metadata.user_story`).
  - In [backend_planning_agent/agent.py](software_engineering_team/backend_planning_agent/agent.py) (and frontend equivalent): When parsing nodes, set `metadata["user_story"] = n.get("user_story") or (n.get("metadata") or {}).get("user_story", "")` so the graph node carries it and [planning_graph.compile_graph_to_assignment](software_engineering_team/planning/planning_graph.py) can pass it through to `Task.user_story`.
- **Fallback in graph→task conversion:** In [planning/planning_graph.py](software_engineering_team/planning/planning_graph.py), when building a `Task` for backend or frontend, if `node.metadata.get("user_story")` is empty, set a default (e.g. “As a developer, I want {summary} so that the system meets the requirements.”) so validation passes even when the planner omits it. This ensures robustness without blocking on prompt compliance.

---

## 4. Summary and order of work


| Warning                                  | Root cause                                                         | Primary fix                                                                                                  |
| ---------------------------------------- | ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------ |
| ProjectOverview `model_dump`             | Pydantic v1 / serialization edge case                              | Harden `model_to_dict` with try/except and `.dict()` fallback                                                |
| Architecture JSON parse / missing keys   | LLM returns non-JSON or partial JSON                               | Synthetic architecture when response is wrapper; optional LLM retry; lower log level for backfilled diagrams |
| Missing `user_story` (validation failed) | Backend/frontend planners don’t emit `user_story` in node metadata | Add `user_story` to planner prompts and parsing; backfill default in `compile_graph_to_assignment`           |


Implementing (1) prevents the project planning serialization warning. Implementing (2) reduces architecture-related warnings and improves resilience. Implementing (3) removes the Tech Lead validation warnings and keeps the planning pipeline path instead of falling back to monolithic generation.

### Suggested implementation order

1. **ProjectOverview (quick win):** `harden-model-to-dict` → `audit-orchestrator-overview-serialization` → `add-tests-model-to-dict`
2. **user_story (unblocks pipeline):** `backend-prompt-user-story` → `backend-parse-user-story` → `frontend-prompt-user-story` → `frontend-parse-user-story` → `planning-graph-backfill-user-story` → `add-tests-user-story-backfill`
3. **Architecture (resilience + noise):** `arch-detect-raw-wrapper` → `arch-reduce-diagram-warning` → `arch-reduce-missing-keys-warning` (optional: `arch-optional-llm-retry`)
4. **Polish:** `optional-log-message-alignment` if desired

---

## Optional: Log message alignment

The exact string “Project planning failed (continuing without overview)” was not found in the repo; the closest is “Project planning LLM failed, attempting fallback overview” in the orchestrator. If this message is coming from another service or an older branch, consider standardizing the wording so that when project planning fails (e.g. exception in `model_to_dict` or fallback), the log clearly states whether the job is continuing without an overview or attempting fallback, to avoid confusion.