---
name: Faster delivery without sacrificing quality
overview: Improve software engineering team delivery speed by parallelizing the planning phase, tuning LLM concurrency and iteration caps, and adding optional fast paths—while keeping existing quality gates and early-exit behavior intact.
todos:
  - id: plan-tier1-helpers
    content: "Section 1: Add helper to run a single Tier 1 planning agent (api_contract, data_architecture, ui_ux, infrastructure) with existing inputs; return (agent_key, output_or_exc) for collection"
    status: completed
  - id: plan-tier1-executor
    content: "Section 1: In orchestrator.py, implement Tier 1 block using ThreadPoolExecutor (max_workers=4); run api_contract, data_architecture, ui_ux, infrastructure in parallel; pass spec_content, arch_overview, plan_dir, requirements/features"
    status: completed
  - id: plan-tier1-collect
    content: "Section 1: Collect Tier 1 results into infra_doc, data_lifecycle, ui_ux_doc; preserve try/except per-agent semantics and logger.debug('... skipped') on exception; keep artifact writes in plan_dir from worker threads or serialize"
    status: completed
  - id: plan-tier2-helpers
    content: "Section 1: Add helper to run a single Tier 2 agent (frontend_architecture, devops_planning, qa_test_strategy, security_planning) with Tier 1 outputs as inputs"
    status: completed
  - id: plan-tier2-executor
    content: "Section 1: Implement Tier 2 block with ThreadPoolExecutor; run frontend_architecture (ui_ux_doc), devops_planning (infra_doc), qa_test_strategy, security_planning (data_lifecycle) in parallel; collect devops_doc and any other outputs"
    status: completed
  - id: plan-tier3-parallel
    content: "Section 1: Implement Tier 3: run observability (infra_doc, devops_doc) and performance_doc in parallel (ThreadPoolExecutor or two futures); then call run_planning_consolidation and write_tech_lead_plan unchanged"
    status: completed
  - id: plan-preserve-writes
    content: "Section 1: Ensure plan_dir writes from parallel agents do not race (same file from two agents); add locking or single-threaded write step if any agent writes to the same path"
    status: completed
  - id: plan-tests
    content: "Section 1: Add or extend tests for tiered planning: mock agents, assert Tier 1 runs in parallel (e.g. via call order or timing), assert Tier 2 receives Tier 1 outputs, assert consolidation runs after Tier 3"
    status: completed
  - id: llm-default-concurrency
    content: "Section 2: In shared/llm.py _get_llm_concurrency_limit(), change default from 2 to 4 when ENV_LLM_MAX_CONCURRENCY is unset; keep env override behavior"
    status: completed
  - id: llm-docs-readme
    content: "Section 2: In software_engineering_team/README.md LLM table, document SW_LLM_MAX_CONCURRENCY default 4 and note that 4-6 can reduce wall-clock time for parallel planning and backend+frontend; mention GPU/memory limits"
    status: completed
  - id: minimal-skip-param
    content: "Section 3: Add optional run parameter skip_planning_agents (list[str]) and/or env SW_SKIP_PLANNING_AGENTS (comma-separated); document allowed keys (api_contract, data_architecture, ui_ux, infrastructure, frontend_architecture, devops_planning, qa_test_strategy, security_planning, observability, performance_doc)"
    status: completed
  - id: minimal-skip-wire
    content: "Section 3: In tiered planning (or sequential fallback), skip any agent whose key is in skip_planning_agents; ensure Tier 2/3 agents that depend on skipped agents receive empty or safe default inputs"
    status: completed
  - id: minimal-planning-flag
    content: "Section 3 (optional): Add minimal_planning: bool option that skips all domain planning agents (Tier 1-3); run only spec → project planning → Tech Lead ↔ Architecture → alignment/conformance → consolidation → execution; document as fast path"
    status: completed
  - id: caps-orchestrator-env
    content: "Section 4: In orchestrator.py, read MAX_ALIGNMENT_ITERATIONS from SW_MAX_ALIGNMENT_ITERATIONS (default 6) and MAX_CONFORMANCE_RETRIES from SW_MAX_CONFORMANCE_RETRIES (default 4); use in alignment and conformance loops"
    status: completed
  - id: caps-backend-env
    content: "Section 4: In backend_agent/agent.py, read MAX_REVIEW_ITERATIONS, MAX_CLARIFICATION_ROUNDS, MAX_SAME_BUILD_FAILURES from env (e.g. SW_MAX_REVIEW_ITERATIONS default 40, SW_MAX_CLARIFICATION_ROUNDS default 10, SW_MAX_SAME_BUILD_FAILURES default 6)"
    status: completed
  - id: caps-frontend-env
    content: "Section 4: In frontend_team/orchestrator.py and feature_agent/agent.py, read MAX_CODE_REVIEW_ITERATIONS, MAX_CLARIFICATION_REFINEMENTS, MAX_SAME_BUILD_FAILURES from env with current values as defaults"
    status: completed
  - id: caps-readme
    content: "Section 4: Document all new SW_MAX_* env vars in software_engineering_team/README.md with defaults and note that lowering caps can speed runs but may reduce refinement"
    status: completed
  - id: cache-standards-api
    content: "Section 5: In shared/coding_standards.py, add get_coding_standards_cached() or module-level cache that loads standards text once and returns same string on subsequent calls (per process)"
    status: completed
  - id: cache-standards-callers
    content: "Section 5: Audit backend_agent, frontend_team, code_review_agent, qa_agent, etc. for coding_standards usage; switch to cached loader where they read or resolve standards per task"
    status: completed
  - id: lightweight-keywords
    content: "Section 6: In frontend_team/orchestrator.py, add optional keywords to LIGHTWEIGHT_KEYWORDS (e.g. refactor, adjust, tweak) and/or increase LIGHTWEIGHT_MAX_DESC_LEN (e.g. 300→400) after validating on sample specs"
    status: completed
  - id: lightweight-config
    content: "Section 6 (optional): Make LIGHTWEIGHT_KEYWORDS or max desc length configurable via env (e.g. SW_FRONTEND_LIGHTWEIGHT_KEYWORDS) so users can tune without code change"
    status: cancelled
  - id: lightweight-tests
    content: "Section 6: Add or update tests in tests/test_frontend_team.py for new keywords/length; ensure no full-feature tasks are misclassified as lightweight"
    status: completed
  - id: truncation-design
    content: "Section 7: Document design for task-aware context truncation: extract route/component/file hints from task description and acceptance criteria; select relevant files first, then truncate within cap; list risks (dropping critical files)"
    status: completed
  - id: truncation-impl
    content: "Section 7 (follow-up): Implement task-aware file selection and truncation in shared or backend_agent/frontend_team; add tests and manual validation that critical files are not dropped"
    status: cancelled
  - id: parallel-tasks-design
    content: "Section 8: Document design for multiple backend (or frontend) tasks in parallel: option A clone per worker (work_path/backend_1, backend_2), option B branch-per-task with serialized merge order; list tradeoffs"
    status: completed
  - id: parallel-tasks-impl
    content: "Section 8 (only if profiling justifies): Implement parallel backend or frontend task execution with chosen approach; add tests for merge order and no cross-task file conflicts"
    status: cancelled
  - id: integration-test
    content: Run full pipeline (e.g. test_repo) with parallel planning and default concurrency 4; confirm all plan artifacts present and execution completes; compare wall-clock to sequential baseline if possible
    status: completed
  - id: readme-summary
    content: "Update software_engineering_team/README.md with a short 'Faster runs' subsection: parallel planning, SW_LLM_MAX_CONCURRENCY, optional skip_planning_agents/minimal_planning, configurable iteration caps"
    status: completed
isProject: false
---

# Faster Software Engineering Team Delivery (Without Sacrificing Quality)

## Current bottlenecks (from codebase)

- **Planning:** ~12 domain planning agents run strictly sequentially after Architecture/Tech Lead alignment ([orchestrator.py](software_engineering_team/orchestrator.py) ~1058–1182). Each only needs `arch_overview`/`spec_content`/`plan_dir` or a prior agent’s output.
- **LLM concurrency:** Single global semaphore caps concurrent Ollama calls at **2** ([shared/llm.py](software_engineering_team/shared/llm.py) ~567–585). Backend and frontend workers already run in parallel, so they contend for this limit.
- **Execution:** Backend and frontend queues are already parallelized; within each domain, tasks run one-by-one by design (shared repos/branches).
- **Quality:** Per-task workflows already exit early when all gates pass ([backend_agent/agent.py](software_engineering_team/backend_agent/agent.py) ~1214–1220). Frontend has a lightweight path that skips design for fix/resolve-style tasks ([frontend_team/orchestrator.py](software_engineering_team/frontend_team/orchestrator.py) ~117–118, 212–221).

---

## 1. Parallelize domain planning agents (high impact)

**Idea:** Run planning agents in dependency tiers so independent agents run in parallel instead of one after another.

**Dependencies (from [orchestrator.py](software_engineering_team/orchestrator.py) 1058–1182):**

- **Tier 1** (inputs: `spec_content`, `arch_overview`, `plan_dir`, plus features/requirements where used): `api_contract`, `data_architecture`, `ui_ux`, `infrastructure` → no cross-deps; can run in parallel.
- **Tier 2:** `frontend_architecture` (needs `ui_ux_doc`), `devops_planning` (needs `infra_doc`), `qa_test_strategy`, `security_planning` (needs `data_lifecycle` from data_architecture). After Tier 1 completes, run these in parallel (e.g. frontend_arch + devops + qa_test_strategy + security_planning in a thread pool).
- **Tier 3:** `observability` (needs `infra_doc`, `devops_doc`), `performance_doc`. Run after Tier 2, optionally in parallel with each other.
- **Then:** `run_planning_consolidation` and write Tech Lead plan (unchanged).

**Implementation:** In `orchestrator.py`, replace the sequential `if agents.get("api_contract"): ... if agents.get("data_architecture"): ...` block with a small dependency graph: Tier 1 → `concurrent.futures.ThreadPoolExecutor` (or similar) with 4 workers, collect outputs into `infra_doc`, `data_lifecycle`, `ui_ux_doc`; Tier 2 same pattern; Tier 3 then consolidation. Preserve existing `try/except` and `logger.debug("... skipped: %s", e)` so a single agent failure does not break the rest. Keep writing artifacts to `plan_dir` from the same threads (or serialize writes if needed to avoid races).

**Quality:** No change to artifacts or decisions; only execution order and concurrency. Same inputs and outputs per agent.

---

## 2. Increase LLM concurrency (medium impact)

**Idea:** Default `SW_LLM_MAX_CONCURRENCY` is 2 ([shared/llm.py](software_engineering_team/shared/llm.py)). With backend and frontend workers running in parallel, and (after change 1) multiple planning agents in parallel, raising the limit allows more concurrent LLM calls and better CPU/GPU utilization.

**Implementation:**

- In [shared/llm.py](software_engineering_team/shared/llm.py), keep reading from `ENV_LLM_MAX_CONCURRENCY` but change the default from `2` to `4` (or document that users can set 4–6 for faster runs when the Ollama/server can handle it).
- In [README.md](software_engineering_team/README.md), document that increasing `SW_LLM_MAX_CONCURRENCY` (e.g. to 4–6) can reduce wall-clock time when running with parallel planning and backend+frontend workers.

**Quality:** No change to prompts or logic; only more concurrent requests. Users with limited GPU/memory can keep the limit at 2.

---

## 3. Optional “minimal planning” or skip-list (medium impact, configurable)

**Idea:** For smaller or time-sensitive runs, allow skipping some domain planning agents so planning finishes sooner while still keeping Architecture, Tech Lead, alignment, and conformance.

**Implementation:**

- Add an optional parameter (e.g. `skip_planning_agents: list[str]` or env `SW_SKIP_PLANNING_AGENTS=observability,performance_doc`) and, in the planning block (or in the new tiered execution), skip any agent whose key is in that set.
- Alternatively, a single flag like `minimal_planning: bool` that skips all of: API Contract, Data Arch, UI/UX, Infra, Frontend Arch, DevOps Planning, QA Test Strategy, Security Planning, Observability, Performance doc—so only spec → project planning → Tech Lead ↔ Architecture (alignment + conformance) → consolidation (with whatever consolidation can do without those artifacts) → execution. Use only when the user explicitly opts in.

**Quality:** Clearly documented as a fast path; full planning remains the default. Useful for experiments or when artifacts from skipped agents are not required.

---

## 4. Make iteration caps configurable (low–medium impact)

**Idea:** Current caps are hardcoded (e.g. backend `MAX_REVIEW_ITERATIONS = 40`, orchestrator `MAX_REVIEW_ITERATIONS = 20`, `MAX_ALIGNMENT_ITERATIONS = 6`, `MAX_CONFORMANCE_RETRIES = 4`). Making them configurable (env or API option) lets users trade off speed vs. strictness without code changes.

**Implementation:**

- In [orchestrator.py](software_engineering_team/orchestrator.py): read `MAX_ALIGNMENT_ITERATIONS` / `MAX_CONFORMANCE_RETRIES` from env (e.g. `SW_MAX_ALIGNMENT_ITERATIONS`, `SW_MAX_CONFORMANCE_RETRIES`) with current values as defaults.
- In [backend_agent/agent.py](software_engineering_team/backend_agent/agent.py) and [frontend_team/orchestrator.py](software_engineering_team/frontend_team/orchestrator.py): same for `MAX_REVIEW_ITERATIONS`, `MAX_CLARIFICATION_REFINEMENTS`, `MAX_SAME_BUILD_FAILURES` (backend/frontend).
- Document in README. Defaults remain as today so behavior is unchanged unless the user overrides.

**Quality:** Preserves current defaults; power users can lower caps for faster (and potentially less refined) runs.

---

## 5. Cache coding standards and static context per run (low impact)

**Idea:** Coding standards and other static prompt fragments are likely loaded or concatenated repeatedly per task. Caching them once per run reduces redundant I/O and string work.

**Implementation:**

- In [shared/coding_standards.py](software_engineering_team/shared/coding_standards.py) (or at first use in orchestrator/backend/frontend), add a module-level or run-scoped cache for the resolved standards text and pass that into agents instead of re-reading/re-resolving every time.
- If any agent currently reads from disk on every invocation, switch to a single load at the start of the run (or when the agent is first constructed) and reuse.

**Quality:** Same content; only fewer redundant loads.

---

## 6. Expand lightweight frontend path (low impact)

**Idea:** Frontend already skips the design phase for “lightweight” tasks ([frontend_team/orchestrator.py](software_engineering_team/frontend_team/orchestrator.py) `_is_lightweight_task`: short description with keywords like fix, resolve, update, patch). Slightly expanding this (e.g. more keywords or a higher `LIGHTWEIGHT_MAX_DESC_LEN`) can skip design for more implementation-only tasks without materially affecting quality.

**Implementation:**

- Consider adding keywords (e.g. "refactor", "adjust", "tweak") or increasing `LIGHTWEIGHT_MAX_DESC_LEN` (e.g. from 300 to 400) after validating on a few sample specs that no full-feature tasks are misclassified. Optionally make these configurable via env or task metadata.

**Quality:** Limit expansion to clearly implementation-only/fix-style tasks; keep full design for net-new features.

---

## 7. Smarter context truncation (optional, higher risk)

**Idea:** Limits like `MAX_EXISTING_CODE_CHARS = 40000` and `MAX_API_SPEC_CHARS = 20000` keep context manageable. A “smarter” truncation could prefer files relevant to the current task (e.g. by route/component name) so the model sees the most relevant code within the same cap. This can improve both speed (smaller prompts) and relevance (fewer irrelevant lines).

**Implementation:** More involved: would require task-aware file selection (e.g. from task description or acceptance criteria) and then truncation within that subset. Consider as a follow-up; document as an option rather than a first-step change.

**Quality:** Can improve relevance; must validate that critical files are never dropped.

---

## 8. Multiple backend or frontend tasks in parallel (advanced, high complexity)

**Idea:** Today, backend runs one task at a time and frontend runs one task at a time (with backend and frontend concurrent). Running multiple backend (or frontend) tasks in parallel would require either separate working directories (e.g. clone per task) or strict branch/lock discipline and merge order. This could significantly reduce wall time when there are many independent tasks.

**Implementation:** Would require cloning `work_path/backend` (and optionally `work_path/frontend`) per parallel worker, or implementing a queue with branch-per-task and serialized merges. High implementation and maintenance cost; only worth it if profiling shows task execution dominates total time after planning is parallelized.

**Quality:** Same workflows per task; coordination and merge order must preserve correctness.

---

## Suggested order of work

1. **Parallelize domain planning (section 1)** – largest win for planning-heavy runs.
2. **Increase default LLM concurrency and document it (section 2)** – quick, low-risk.
3. **Configurable iteration caps (section 4)** – enables tuning without code changes.
4. **Optional minimal planning or skip-list (section 3)** – for users who want a fast path.
5. **Cache coding standards (section 5)** and **expand lightweight frontend (section 6)** – small, safe improvements.
6. Consider **smarter truncation (7)** and **parallel tasks per domain (8)** only if profiling shows they are necessary.

---

## Flow after changes (conceptual)

```mermaid
flowchart LR
  subgraph planning [Planning]
    Spec[Spec + Project Planning]
    TL[Tech Lead]
    Arch[Architecture]
    Align[Alignment + Conformance]
    T1[Tier 1: API Contract, Data Arch, UI/UX, Infra]
    T2[Tier 2: Frontend Arch, DevOps, QA Strategy, Security]
    T3[Tier 3: Observability, Perf Doc]
    Consolidate[Consolidation]
    Spec --> TL
    Spec --> Arch
    TL --> Align
    Arch --> Align
    Align --> T1
    T1 --> T2
    T2 --> T3
    T3 --> Consolidate
  end
  subgraph exec [Execution]
    Prefix[Prefix tasks]
    Backend[Backend worker]
    Frontend[Frontend worker]
  end
  Consolidate --> Prefix
  Prefix --> Backend
  Prefix --> Frontend
```

Tier 1/2/3 run with internal parallelism (thread pool); execution remains as today with backend and frontend in parallel.
