---
name: Extract shared infra between coding_team and software_engineering_team; break the circular dependency
overview: >
  coding_team and software_engineering_team (SE) reach into each other's internals through
  function-local imports, forming a bidirectional package dependency that makes neither team
  testable or reasonable in isolation. Promote the genuinely-shared infrastructure (job-store
  wrapper base, command_runner + error_parsing, repo scanner, LLM-recovery parser, dev-pipeline
  models, git utils) into neutral shared_* packages that both teams depend on, and invert the
  remaining coding_team -> SE engine dependencies (v2 code team leads, quality gates, code review)
  so the canonical direction is one-way (SE -> coding_team). End state: coding_team imports nothing
  from software_engineering_team, the graph is acyclic, and coding_team finally runs in CI under a
  90% coverage gate.
todos:
  - id: job-store
    content: Extract shared_job_store with the common create/get/update/list + byte-identical HITL helpers over job_service_client; reconcile the two behavioural drifts (get_submitted_answers None-coercion, list_jobs projection); keep SE and coding_team thin extension wrappers for their team-specific methods/fields/statuses.
    status: pending
  - id: command-runner
    content: Move command_runner.py + error_parsing.py into shared_command_runner (module-level stdlib-only; lazy error_parsing/git deps); repoint SE + quality_gate_tools; leave a re-export shim at the old SE path.
    status: pending
  - id: repo-context
    content: Create shared_repo_context housing read_repo_code + the extension/exclude constants; make SE repo_utils re-export them; collapse the _read_repo_code trio (backend_code_v2, frontend_code_v2, ai_agent_development_team) and coding_team._read_repo_context onto the shared scanner.
    status: pending
  - id: llm-recovery
    content: Move the stdlib-only llm_response_utils into shared_llm_recovery and upgrade coding_team Tech Lead's naive fence-strip _agent_call_json to use the shared brace-scan/JSON recovery; leave SE-coupled json_utils where it is.
    status: pending
  - id: dev-models-git-llm
    content: Promote the dev-pipeline models (Task/TaskStatus/TaskType/TaskAssignment/PlanningHierarchy) to shared_dev_models, git_utils + branch_utils to shared_git, and strands_model into llm_service, so coding_team stops importing software_engineering_team.shared.* entirely.
    status: pending
  - id: invert-engines
    content: Introduce a CodeEngineProvider protocol so coding_team.orchestrator no longer imports SE's frontend/backend_code_v2 team leads, quality_gate_tools, or code_review_agent; SE injects concrete engines when calling run_coding_team_orchestrator, and a thin SE-backed adapter (the standalone coding-team container's entrypoint, living outside the coding_team package) injects them for standalone runs.
    status: pending
  - id: ci-coverage
    content: Add a dedicated coding_team CI test job (its 22 test files never run today) plus jobs for the new shared_* packages, wire path filters, and enforce --cov-fail-under=90 on coding_team and every new module.
    status: pending
  - id: acyclic-guard
    content: Add an automated guard (import-linter contract or a unit test) asserting coding_team has zero software_engineering_team imports, so the cycle cannot silently return.
    status: pending
isProject: false
---

# Spec

## Problem

`coding_team` and `software_engineering_team` (SE) form a **bidirectional package
dependency**. SE delegates its execution phase to `coding_team`, while
`coding_team` reaches back into SE for shared utilities *and* for SE's actual
specialist agents. This makes neither team importable, testable, or reasonable in
isolation — the test suites paper over it with `sys.modules` stubbing
(`coding_team/tests/test_github_source.py`, `test_branch_prep_recovery.py`).

The good news, and the proof the fix works: `job_service_client.py` (the job
backend) and the git-ops helpers are already single-sourced, and several SE agents
already delegate repo scanning to `shared.repo_utils.read_repo_code`. The rest
should follow the same model — promoted to a **neutral** location both teams
depend on, rather than one team importing the other's internals.

## Corrections to the original framing (verified against the current tree)

The work item was filed against a slightly older tree. Four of its specifics have
drifted and the plan is built on the **verified** state below, not the original
line references:

1. **The cycle is entirely function-local, not import-time.** Every cross-team
   edge is a deferred import inside a function body, and the package `__init__.py`
   files do not chain them (`coding_team/__init__.py` is `__all__ = []`). So there
   is **no import-time `ImportError`** today — the damage is architectural
   (testability, reasoning, drift), which reframes success from "make it import"
   to "make the graph acyclic and keep it that way."
   - coding_team -> SE (deferred): `orchestrator.py:649` (`shared.strands_model`),
     `:697/:701` (`frontend_code_v2_team`/`backend_code_v2_team`), `:720`
     (`shared.repo_utils`), `:1549/:1628/:2049` (`shared.git_utils`), `:1862`
     (`quality_gate_tools`), `api/main.py:1473` (`code_review_agent`). Top-level
     (one-directional, not part of the runtime cycle): `v2_team_worker.py:11-21`
     (`shared.branch_utils`, `shared.git_utils`, `shared.models`),
     `api/main.py:76` (`shared.git_utils`).
   - SE -> coding_team (deferred): `orchestrator.py:338`
     (`coding_team.hitl.answers_to_resolved`), `:663`
     (`coding_team.models.CodingTeamPlanInput`), `:2250` +
     `temporal/activities.py:575` (`coding_team.orchestrator.run_coding_team_orchestrator`).

2. **coding_team does NOT run its own build/test/lint subprocess.** The work item
   says its "build/test/lint subprocess invocation is scattered/inlined." It is
   not: coding_team's only own subprocess is git (`api/main.py:2006 _git`). All
   build/test/lint flows through the `quality_gate_tools` seam
   (`orchestrator.py:1862 _run_quality_gates`) into
   `software_engineering_team.orchestrator._run_build_verification` ->
   `command_runner`/`error_parsing`. So the acceptance item "route coding_team
   through the shared command runner" is *already true transitively* — the real
   defect is that the seam is a coding_team -> SE import. Extraction still pays
   off (single home, no cross-team edge), but the framing is "sever the seam,"
   not "de-duplicate scattered subprocess calls."

3. **There is only one `_read_repo_context`.** It lives in
   `coding_team/orchestrator.py:730-784` and already sources its filter constants
   from `shared.repo_utils`. The function the work item points at in
   `backend_code_v2_team/orchestrator.py:88-114` is a *different* function,
   `_read_repo_code` (a `@staticmethod`, `max_chars`-truncating). The genuine
   copy-paste is a **trio** of `_read_repo_code` static methods —
   `backend_code_v2_team` (`:88`), `frontend_code_v2_team` (`:102`),
   `ai_agent_development_team` (`:65`) — none of which use the already-shared
   `repo_utils.read_repo_code` that `backend_agent` and `documentation_agent`
   delegate to. So this is "finish an in-progress consolidation," not "merge two
   identical readers."

4. **The recovery parsers are asymmetric and partly dead.**
   `shared/llm_response_utils.py` (brace-scan + file extraction) is **stdlib-only
   and has zero production callers** — referenced only by tests and docs.
   `shared/json_utils.py` is **SE-coupled** (`shared.deduplication`,
   `shared.continuation`, `shared.llm`) and used only by SE's PRA agent; its
   "recovery" is continuation-on-truncation + chunk/merge, not salvage parsing.
   coding_team's only LLM-JSON parse is a naive fence-strip + `json.loads` with
   retry-or-default (`tech_lead_agent/agent.py:131-137`). So "share the recovery
   parsers" concretely means: promote the stdlib-only salvage parser to neutral
   ground and give coding_team's Tech Lead the resilience it lacks; leave the
   SE-coupled `json_utils` alone.

## What is actually shared (the extraction targets)

| Piece | Current location | Dependency profile | Consumers |
|---|---|---|---|
| Job-store wrapper | `coding_team/job_store.py` (279 LOC) + `SE/shared/job_store.py` (373 LOC) | Both thin wrappers over `job_service_client.JobServiceClient`; identical HITL methods; `cache_dir` vestigial | coding: 2 files; SE: 6 files |
| command_runner | `SE/shared/command_runner.py` (2230 LOC) | Module-level stdlib-only; lazy `error_parsing` + `git_utils` | SE + (transitively) coding via quality gates |
| error_parsing | `SE/shared/error_parsing.py` (679 LOC) | Pure stdlib, fully self-contained | command_runner only |
| Repo scanner | `SE/shared/repo_utils.read_repo_code` (exists) + trio of `_read_repo_code` + coding `_read_repo_context` | repo_utils is self-contained bar sensitive-path helpers | backend_agent, documentation_agent (delegate); trio + coding (don't) |
| Salvage parser | `SE/shared/llm_response_utils.py` | Stdlib-only, dead in prod | tests/docs only |
| Dev models | `SE/shared/models.py` (Task/TaskStatus/TaskType/…) | pydantic only | SE widely; coding via `v2_team_worker.py:19-21` |
| Git utils | `SE/shared/git_utils.py` (45 KB) + `branch_utils.py` | git subprocess helpers | SE widely; coding via 5 sites |
| strands_model | `SE/shared/strands_model.py` | imports only `llm_service` (neutral) | SE v2 phases/tool agents; coding `:649` |

## Goals

1. One neutral home per shared concern; both teams import the neutral module.
2. **coding_team imports nothing from `software_engineering_team`** — the cycle is
   gone and cannot come back (guarded).
3. Behaviour parity: existing coding_team and SE tests pass; the two known
   job-store drifts are reconciled deliberately, not accidentally.
4. coding_team's build/test/lint keeps routing through the shared command runner —
   now via a neutral module instead of a cross-team import.
5. `_read_repo_code` / `_read_repo_context` exist in exactly one place.
6. 90% line coverage + DbC docstrings (`Preconditions:` / `Postconditions:` /
   `Invariants:`) on all new/changed code, per `CLAUDE.md`.

## Non-goals

- Merging or consolidating SE's specialist agents themselves (the v2 code teams,
  quality-gate tools, code-review agent). Those are tracked separately; this work
  **decouples** coding_team from them via dependency inversion, it does not move
  or rewrite them.
- Changing job lifecycle semantics, the job-service HTTP contract, or any
  team-specific status/field. Team extensions are preserved verbatim.
- Touching `json_utils.py`'s SE-specific continuation/chunk machinery.
- Reworking the SE -> coding_team delegation. `SE -> coding_team` is the
  *correct*, acyclic direction and is kept.

# Implementation Plan

The work splits into two halves. **Part A** (modules `job-store`,
`command-runner`, `repo-context`, `llm-recovery`, `dev-models-git-llm`) is the
mechanical, lower-risk extraction — each module can land as its own PR and each
one removes coding_team -> SE edges as a side effect. **Part B**
(`invert-engines`) is the higher-effort dependency inversion that severs the last,
non-infra coding_team -> SE edges. **Part C** (`ci-coverage`, `acyclic-guard`)
makes the result verifiable and durable.

Every new package follows the verified `shared_<domain>` convention: flat package
under `backend/agents/`, `__init__.py` re-exporting a curated public API with an
explicit `__all__`, single-responsibility submodules, a `README.md`, and a
`tests/` dir. These are pure utilities, so they use **no** registration idiom
(neither Pattern A import-time side effects nor Pattern B lifespan hooks) — they
match `shared_env_config` in being import-safe and stateless.

Migration uses the **extract-then-shim** technique throughout: create the neutral
module, leave a thin re-export at the old path so nothing breaks mid-flight, flip
call sites, then delete the shim in the same PR once its consumers are moved.

## Part A — Neutral infra modules

### A1. `shared_job_store` (todo: job-store)

Both `job_store.py` files are module-level function libraries over the same
pooled `job_service_client.get_job_service_client(team=...)`; the canonical
`JOB_STATUS_*` constants already live in `job_service_client.py`. The extraction
is a **base + thin extensions**, not a merge:

1. New `shared_job_store/` exporting the pieces that are byte-identical across the
   two files, each taking an injected `team` (via a small `JobStore` factory bound
   to a team string, or module functions parameterised by team):
   - Identical today: `get_job`, `add_pending_questions`, `submit_answers`,
     `is_waiting_for_answers`, `get_submitted_answers`.
   - Common-shape-with-drift, unified with explicit params: `create_job`
     (SE's `job_type` and coding's `plan_input` both become optional kwargs),
     `update_job` (keep coding's explicit `heartbeat: bool=True`), `list_jobs`
     (see reconciliation below).
2. **Reconcile the two verified drifts deliberately:**
   - `get_submitted_answers`: adopt coding's `data.get("submitted_answers") or []`
     (coerces a stored `None`), which strictly dominates SE's `.get(..., [])`.
   - `list_jobs`: the shared function returns the **raw** client list; the SE
     summary-projection (`job_id/status/repo_path/job_type/created_at`) and the
     `active_only` vs `running_only`/`job_type` filters move into each team's thin
     wrapper. Do not silently give one team the other's filtering.
3. Each team keeps a small `job_store.py` that imports the shared base and adds its
   own surface: SE keeps `reset_job`, `delete_job`, `mark_*`, `request_cancel`,
   `is_cancel_requested`, `start_job_heartbeat_thread`, `update_task_state`,
   `update_job_team_progress`, `add_task_result`, `get_stale_after_seconds`, and
   its `JOB_STATUS_AGENT_CRASH` / `PAUSED_LLM_CONNECTIVITY` / `ALREADY_COMPLETE`
   + `LLM_UNREACHABLE_AFTER_RETRIES` / `LLM_SEMANTIC_EXHAUSTION` sentinels; coding
   keeps `update_job_task_graph`, `claim_resume`, `release_resume_claim` and the
   resume-lease constants. `create_job`'s SE-only `record_association_safe` profile
   call stays in coding's wrapper (it is coding-only today).
4. Repoint the 8 importers (coding: `orchestrator.py:23`, `api/main.py:55/65`;
   SE: `orchestrator.py:49`, `api/main.py:45/65`, `temporal/activities.py:18`,
   `product_requirements_analysis_agent/agent.py`, `.../auto_answer.py`,
   `shared/cost_tracker.py`). The SE test fixture `patched_job_store`
   (`SE/tests/conftest.py:51`) must patch the shared module's client seam.

### A2. `shared_command_runner` (todo: command-runner)

`command_runner.py` is stdlib-only at module scope; it lazily imports
`error_parsing` (in `CommandResult.parsed_failures`) and `git_utils` (in
`ensure_backend_project_initialized`). `error_parsing.py` is pure stdlib.

1. New `shared_command_runner/` with `runner.py` (= `command_runner.py`) and
   `error_parsing.py`; `__init__.py` re-exports the public API (`run_command`,
   `run_command_with_nvm`, `run_pytest`, `run_linter`, `run_frontend_build`,
   `CommandResult`, `parse_command_failure`, `build_agent_feedback`,
   `FailureClass`, `ParsedFailure`, `normalize_error_signature`, the `*_TIMEOUT`
   constants, …).
2. The lazy `git_utils` import inside `ensure_backend_project_initialized` becomes
   an import of `shared_git` (A5) — keep it lazy so the runner stays git-free at
   import time.
3. Leave `SE/shared/command_runner.py` and `error_parsing.py` as re-export shims
   for one PR, flip SE's consumers and `quality_gate_tools`, then delete the shims.
4. coding_team gains a **direct** dependency on `shared_command_runner` only once
   Part B routes its quality gates through an injected runner; until then it keeps
   reaching the runner transitively (no regression).

### A3. `shared_repo_context` (todo: repo-context)

1. New `shared_repo_context/` housing the scanner `read_repo_code(repo_path,
   extensions=None, *, exclude_dirs=None)` plus the constants
   `FULL_STACK_EXTENSIONS`, `BACKEND_EXTENSIONS`, `FRONTEND_EXTENSIONS`,
   `DOCUMENTATION_EXTENSIONS`, `REPO_EXCLUDE_DIRS`, `REPO_INSPECT_EXCLUDE_DIRS`.
   Keep SE-specific helpers (`is_sensitive_path`, `read_files_as_dict`,
   `read_repo_files_as_dict`, `truncate_for_context`) in `repo_utils` for now;
   `repo_utils` re-exports the scanner + constants so existing SE imports and the
   `agent_repo_tools` drift-comment guarantee still hold.
2. Collapse the copy-paste onto the scanner:
   - The `_read_repo_code` trio (`backend_code_v2_team:88`,
     `frontend_code_v2_team:102`, `ai_agent_development_team:65`) become thin
     wrappers calling `read_repo_code(repo_path, <domain extensions>,
     exclude_dirs=<domain excludes>)` with a `max_chars` post-truncation via the
     existing `truncate_for_context`. This also fixes their latent bugs (whole-tree
     `rglob` stat; `backend_code_v2` missing `dist`/`.angular` excludes).
   - coding_team's `_read_repo_context` keeps its 80-file / full-content contract
     but imports its constants from `shared_repo_context` instead of
     `SE.shared.repo_utils` (removing the `orchestrator.py:720` edge). If its
     `os.walk`+prune traversal can be expressed as a `read_repo_code` option, fold
     it in; otherwise keep the coding-specific walk and share only constants (its
     "no content truncation" rule is intentional and must be preserved).

### A4. `shared_llm_recovery` (todo: llm-recovery)

1. New `shared_llm_recovery/` = `llm_response_utils.py` verbatim (stdlib-only):
   `extract_files_from_content`, `heuristic_extract_files_from_content`,
   `extract_task_assignment_from_content`, `extract_single_python_block`.
2. Leave a re-export shim at `SE/shared/llm_response_utils.py` (its only callers
   are SE tests) and repoint those tests.
3. **Deliver the actual benefit:** replace coding_team's naive
   `_agent_call_json` (`tech_lead_agent/agent.py:131-137`, called from 5 sites:
   `:173/:261/:299/:333/:413`) with a helper that tries `json.loads` first, then
   falls back to the shared brace-scan salvage before the current
   retry-or-default path. This is the concrete resilience coding_team's Tech Lead
   lacks today.
4. `json_utils.py` stays in SE. If any of its truly-generic helpers
   (`default_decompose_by_sections`, `default_merge_results`) are wanted by
   coding later, extract them separately; they are not needed now.

### A5. `shared_dev_models` + `shared_git` + strands_model (todo: dev-models-git-llm)

These are the remaining `SE/shared/*` modules coding_team imports; moving them is
what lets coding_team drop `software_engineering_team` from its imports entirely.

1. `shared_dev_models/` = the dev-pipeline models from `SE/shared/models.py`
   both teams use at the v2 boundary: `Task`, `TaskStatus`, `TaskType`,
   `TaskUpdate`, `TaskAssignment`, `TaskPlan`, `StoryPlan`, `Epic`, `Initiative`,
   `PlanningHierarchy`, plus `model_to_dict` and the tool/architecture models.
   Repoint `coding_team/v2_team_worker.py:19-21` and SE's own consumers. Note the
   two Task families are distinct: coding_team's own task-graph models
   (`coding_team/models.py`) stay put; only SE's boundary models move.
2. `shared_git/` = `git_utils.py` + `branch_utils.py` (already cross-imported —
   the work item's proof-of-concept, just not yet in a neutral home). Repoint the
   coding sites (`v2_team_worker.py:11-12`, `api/main.py:76`,
   `orchestrator.py:1549/1628/2049`) and SE's many sites; the
   `shared_command_runner` lazy import (A2) targets this.
3. Fold `strands_model.py` into `llm_service` (it imports only
   `llm_service.get_strands_model` + stdlib, so it is effectively already neutral).
   Repoint `coding_team/orchestrator.py:649` and SE's v2 phases/tool agents.
4. Extract-then-shim each; delete shims once consumers are flipped.

After Part A, the only coding_team -> SE edges left are the **engine** imports:
`frontend_code_v2_team`/`backend_code_v2_team` (`:697/:701`), `quality_gate_tools`
(`:1862`), and `code_review_agent` (`api/main.py:1473`).

## Part B — Invert the engine dependencies (todo: invert-engines)

coding_team uses SE's *specialist agents* to do the actual work. Moving those
agents is out of scope, so instead **invert control**: coding_team defines the
interface, SE supplies the implementation. coding_team already receives injected
callables (`update_job_fn`/`get_job_fn` at `orchestrator.py:1060`), so this
extends an established seam.

1. Define a `CodeEngineProvider` protocol in coding_team (or `shared_dev_models`)
   with the three capabilities coding_team currently imports from SE:
   - `build_implementation_team_lead(kind: str, llm) -> TeamLead` (replaces the
     direct `FrontendCodeV2TeamLead`/`BackendCodeV2TeamLead` construction in
     `_build_implementation_worker`, `orchestrator.py:682-709`).
   - `run_quality_gates(...)` (replaces the `quality_gate_tools` import in
     `_run_quality_gates`, `:1850-1936`).
   - `run_code_review(...)` (replaces `code_review_agent` in `api/main.py:1473`).
2. `run_coding_team_orchestrator` accepts an optional `engine_provider`. When SE
   calls it (`SE/orchestrator.py:2250`, `temporal/activities.py:575`), SE passes a
   concrete provider built from its own agents — SE already owns those imports, so
   no new edge is created.
3. **Composition root for the standalone coding_team service.** The standalone
   team is **not** mounted in-process by `unified_api` — it runs as its own
   container (`docker/docker-compose.yml` `coding-team-service:8103`,
   `TEAM_MODULE=coding_team.api.main`) that `unified_api` only **reverse-proxies**
   over HTTP (`unified_api/main.py:114` maps `coding_team ->
   CODING_TEAM_SERVICE_URL`; `unified_api/config.py`'s `parent_team_key` and the
   `in_process=False` default are metadata/proxy-routing only). A Python
   `CodeEngineProvider` object therefore **cannot** be injected from `unified_api`
   across that process boundary — the earlier draft of this plan was wrong on that
   point. Instead, put the composition root **inside the coding-team service
   process but outside the `coding_team` package**: add a thin SE-backed
   adapter/entrypoint module (e.g. `coding_team_service/main.py`) that imports both
   `coding_team` (its app factory) and SE (the engines), builds the SE-backed
   provider, and becomes the container's `TEAM_MODULE`. `coding_team/api/main.py`
   becomes an app factory and `run_coding_team_orchestrator` gains the
   `engine_provider` param the adapter supplies. This keeps `coding_team.*` free of
   SE imports while the **service process** still has SE importable — which it
   already must today, since the current deferred `from software_engineering_team
   ...` imports only work because SE is installed in the coding-team container. SE's
   own orchestrator path is unaffected: it runs inside the SE process and injects
   its provider directly.
4. Provider resolution stays lazy/defensive: the existing behaviour where a
   missing quality-gate seam degrades gracefully (`orchestrator.py:1929`
   `ImportError` -> skip) maps to "no provider injected -> skip gates," preserving
   today's semantics.

End state: the `coding_team` **package** has zero `software_engineering_team`
imports; the SE-backed provider is assembled in exactly two places, both outside
that package — (a) the SE process when SE calls `run_coding_team_orchestrator`, and
(b) the small `coding_team_service` adapter that is the standalone container's
entrypoint. SE -> coding_team remains (deferred, one-directional); the package
graph is acyclic.

## Part C — Make it verifiable and durable

### C1. CI + coverage (todo: ci-coverage)

Verified gap: **`coding_team/tests/` never runs in CI.** `.github/workflows/ci.yml`
has no `coding_team` matrix key; editing `coding_team/**` only triggers the
`software_engineering` entry, which runs `pytest software_engineering_team/tests/`
with `--cov=software_engineering_team`. coding_team's 22 test files and its code
coverage are unmeasured. This must be fixed for the acceptance criteria to mean
anything.

1. Add a `coding_team` entry to the CI test matrix: `tests="coding_team/tests/"`,
   `source="coding_team"`, `--cov-fail-under=90`, `needs_node=True` (its gates
   drive frontend builds transitively). coding_team has no own `conftest.py`/
   `pyproject.toml`, so the root `backend/conftest.py` (in-process job service +
   `fake_job_client` + env defaults) applies as-is.
2. Add dedicated jobs for each new `shared_*` package's `tests/` (mirroring the
   existing `shared_postgres` job), and add the new packages to the
   `shared_backend` path-filter fan-out so a change to shared infra re-runs both
   teams' suites.
3. Keep the `software_engineering` path filter covering `coding_team/**` too, so
   cross-team integration paths still exercise on either side's change.

### C2. Acyclic guard (todo: acyclic-guard)

Add an automated contract so the cycle cannot silently return:

- Preferred: an `import-linter` "forbidden" contract (`coding_team` must not
  import `software_engineering_team`), run in the lint job.
- Minimal fallback if adding a tool is undesirable: a unit test that walks
  `coding_team/**.py` and asserts no `software_engineering_team` import token
  appears (AST-based, ignoring strings/comments).

The guard scopes to the `coding_team/` **package** only. The new
`coding_team_service` adapter (Part B3) is the single sanctioned place that
imports both packages, so it lives **outside** `coding_team/` and is deliberately
exempt — that is precisely what makes it the composition root rather than a cycle.

# Verification

- **Behaviour parity — job store.** The reconciled `get_submitted_answers` returns
  `[]` for a stored `None` (previously SE would raise); `list_jobs` returns the
  same shape each team returned before (raw for coding, projected summary for SE).
  Both teams' existing job-store tests (`coding_team/tests/test_job_store_*.py`,
  `SE/tests/test_job_store_heartbeat.py`, the `patched_job_store` fixture users)
  pass unchanged.
- **Behaviour parity — command runner.** SE build/test/lint tests
  (`test_review_changed_files.py` and the command-runner unit tests) pass against
  the neutral module; coding_team's quality-gate path (`_run_quality_gates`)
  produces identical `CommandResult`/`ParsedFailure` output before and after.
- **Repo scanner.** The trio's outputs are unchanged for a fixture repo (same
  headers, same `max_chars` truncation); coding_team's `_read_repo_context` still
  emits full-content, 80-file output. One `read_repo_code` implementation remains.
- **LLM recovery.** New unit tests drive coding_team's Tech Lead parser through a
  fenced-JSON, a brace-embedded-JSON, and a truncated response, asserting salvage
  succeeds where the old fence-strip returned the hard-coded default.
- **Acyclic.** The guard (C2) fails on any reintroduced `coding_team ->
  software_engineering_team` import. Manual check: `grep -rn
  "software_engineering_team" backend/agents/coding_team --include=*.py` returns
  nothing outside tests' historical stubs (which are removed as their reason
  disappears).
- **Coverage.** New `shared_*` modules and `coding_team` each hit
  `--cov-fail-under=90`; `make lint` (ruff, line-length 120) clean; DbC docstrings
  present on every new/changed public function.
- **End-to-end.** An SE run (planning -> adapter -> `run_coding_team_orchestrator`
  with an injected provider) and a standalone `/api/coding-team` run (provider
  injected by unified_api) both complete, exercising both composition roots.

# Impact

- **Decoupling.** coding_team becomes independently importable and testable;
  the bidirectional package dependency collapses to a one-way SE -> coding_team
  edge plus a shared lower layer. The `sys.modules` stubbing in coding_team tests
  can be deleted.
- **De-duplication.** One job-store base, one command runner, one repo scanner,
  one salvage parser, one set of dev-pipeline models — removing ~250 LOC of
  wrapper duplication and four near-identical repo readers, and killing dead code
  (`llm_response_utils` gains a real caller instead of being test-only).
- **Coverage.** coding_team enters CI under a 90% gate for the first time,
  closing a silent hole where 22 test files and all coding_team code went
  unmeasured.
- **Risk.** Part A is mechanical and PR-per-module. Part B is the load-bearing
  change; its main risk is the standalone-coding_team composition root — mitigated
  by keeping provider resolution lazy and preserving the graceful-degradation
  semantics of the current `ImportError`-skip. If Part B is deferred, Part A alone
  still removes all coding_team -> `SE.shared.*` edges and every duplication,
  leaving only the three engine imports to sever later.

# Sequencing (suggested PRs)

1. `shared_command_runner` (A2) — self-contained, no behaviour change, immediate.
2. `shared_repo_context` (A3) — self-contained; fixes the trio's latent bugs.
3. `shared_llm_recovery` (A4) — self-contained; adds coding_team resilience.
4. `shared_job_store` (A1) — touches 8 importers + the SE test fixture.
5. `shared_dev_models` + `shared_git` + strands_model (A5) — drops the last
   `SE.shared.*` edges from coding_team.
6. `invert-engines` (B) — the dependency inversion; largest blast radius.
7. `ci-coverage` + `acyclic-guard` (C) — land alongside/after (6) so the guard
   goes green only once the cycle is actually gone.
