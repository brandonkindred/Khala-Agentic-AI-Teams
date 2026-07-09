# SE Team Refactor — #2, #3, #4, #5 Design

**Status:** Approved 2026-07-09
**Scope:** Four independent refactors of the software engineering team that reduce code complexity (#2, #5), improve performance (#3, #4), and reduce memory/disk pressure (#3). None change externally observable behavior except where explicitly noted (deleting dead code and correcting stale docs).

All file paths and line numbers below were verified against the working tree on 2026-07-08/09.

---

## Refactor #2 — Collapse the backend/frontend code-v2 review fork

### Problem
`backend_code_v2_team` and `frontend_code_v2_team` carry ~1,800 lines of near-duplicate review/execution/orchestrator code. The recent `shared/` collapse (commit aaf2f117) explicitly deferred the review-phase fork. What remains:

- `phases/review.py`: backend 1059 lines vs frontend 580. `_run_llm_review`, `_run_qa_agent`, `_run_security_agent`, `_run_build_verification` are byte-identical wrappers (differing only in a literal build-label string and the team's injected `ReviewIssue`/prompts). `run_review` and `run_microtask_review` are duplicated with divergences in: `language` default, `lint_agent_type`, build-verify label, blocking predicate (`is_blocking` vs `any_blocking`), lint severity remap (backend yes, frontend no), `ToolAgentPhaseInput` field set (backend includes `existing_code`/`spec_context`/`language`, frontend omits), and recommendation-issue `source` prefix (`tool_` vs none). `run_documentation_self_review` is functionally identical in both (thin delegates to shared `review_utils`).
- `phases/execution.py`: `run_execution_with_review_gates` duplicated (backend 165-752, frontend 134-725), identical signature, identical skeleton (microtask dep-check → coding → `while not phase_failed` code-review/QA/security gates → documentation self-review → rollback), differing in which review functions are called and the gate model.
- `orchestrator.py`: backend 574 vs frontend 575; `*DevelopmentAgent.run_workflow` and `*TeamLead.run_workflow` scaffolds are byte-aligned in ordering, differing in tool-agent roster, `_read_repo_code` extension/exclude sets, and pre-flight detection.

### Approach (decision: parameterize, keep both gate models)
Move the genuinely shared code into `software_engineering_team/shared/v2_review.py`; preserve the two different gate models as thin per-team glue.

**New `ReviewConfig` (frozen dataclass):**
```python
@dataclass(frozen=True)
class ReviewConfig:
    language: str                          # "python" | "typescript"
    lint_agent_type: str                   # "backend" | "frontend"
    build_verify_label: str                # "backend_code_v2" | "frontend_code_v2"
    blocking: Callable[[str], bool]        # is_blocking | any_blocking
    severity_remap: Mapping[str, str] | None  # backend _lint_severity_map | None
    tool_phase_fields: frozenset[str]      # {"existing_code","spec_context","language"} | frozenset()
    source_prefix: str | None              # "tool_" | None
```
Per-team instances live in each team's `phases/_profile.py` next to the existing `StackProfile`.

**Moved to shared `v2_review.py`:**
- `_run_llm_review`, `_run_qa_agent`, `_run_security_agent`, `_run_build_verification` — take `config` for the build-label; inject the team's `ReviewIssue`/prompts via parameters.
- `run_documentation_self_review` — the existing thin delegate moves into shared; per-team copies deleted.
- `run_review(config, ...)` and `run_microtask_review(config, ...)` — single shared implementations; all divergent branches read from `config`.

**Kept per-team (no convergence):**
- Backend keeps `run_code_review_phase` / `run_qa_testing_phase` / `run_security_testing_phase` / `run_documentation_review_phase` with per-phase retry counts (`code_review_max_retries`/`qa_max_retries`/`security_max_retries`) and `IN_CODE_REVIEW`/`IN_QA_TESTING`/`IN_SECURITY_TESTING` statuses. These become thin callers of the shared `run_*` helpers.
- Frontend keeps its unified `run_microtask_review` filtering issues by `i.source in {"qa","security"}` with a single `IN_REVIEW` status and single `max_retries`.
- Each team's `run_execution_with_review_gates` becomes a thin orchestrator over the shared skeleton + its own gate-model glue. The shared microtask-loop skeleton (dep-check, progress emission, `while not phase_failed` cycle, documentation self-review, rollback) moves to `shared/phases/execution.py` as `run_gated_execution_impl(*, config, gate_runner, ...)` parameterized by a `GateRunner` strategy each team supplies. **Deferred to a follow-up issue:** the `run_gated_execution_impl` skeleton is out of scope for this PR — only the `run_review` / `run_microtask_review` collapse (the `shared/v2_review.py` body) ships here. The execution-loop skeleton is tracked separately and re-introduced in its own PR.

**Behavior contract:** Identical review outcomes for both teams. The divergent knobs are preserved 1:1 in `ReviewConfig`; no gate model is changed.

### Affected files
- New: `software_engineering_team/shared/v2_review.py`
- Modified: both teams' `phases/review.py`, `phases/_profile.py`, `phases/problem_solving.py` (delegates unchanged), `orchestrator.py`
- Deferred (follow-up issue): `shared/phases/execution.py` (`run_gated_execution_impl`) and both teams' `phases/execution.py` convergence — the execution-loop skeleton collapse does not ship in this PR.
- Tests: `tests/test_v2_review_phase.py`, `tests/test_v2_fe_review_phase.py` — updated to the shared functions; new tests for `ReviewConfig` divergence coverage.

### Risk
Medium. The divergent knobs must be preserved exactly (severity remap, source prefix, ToolAgentPhaseInput fields). Test coverage on both teams' review outcomes is the guardrail.

---

## Refactor #3 — Stop redundant I/O and repo walks (safe wins only)

### Problem
- `write_microtask_output_or_fail` and every batch-fix cycle (execution.py:299/407/529/607) re-write the *entire* `microtask_files` dict via `write_files_and_commit` → `git add -A` (whole-tree stage). O(M·F·R) writes.
- The v2 orchestrators' `_read_repo_code` (backend `orchestrator.py:83-95`, frontend `orchestrator.py:97-120`) calls `read_repo_code_budgeted` fresh on every `run_workflow` (one per task) — N full repo walks for N tasks — even though `coding_team/orchestrator.py:369-423` already maintains an incremental `_RepoContextCache` that the v2 worker discards at `v2_team_worker.py:375`.
- `shared_repo_context/repo_utils.py:140-151` materializes the full `rglob("*")` walk into a `List[Path]` before the budgeted read loop.

### Approach (decision: safe wins only)
Three behavior-preserving changes. `all_files` retention is unchanged (the orchestrator documentation phase feeds `exec_result.files` contents to the review LLM, and deliver re-writes files — both need contents; per decision we do not touch that contract).

**(a) Incremental git commits via `commit_paths`.** ~~`commit_paths(repo_path, paths, message) -> (bool, str)` (already production-used in `shared/phases/setup.py` and both teams' `setup.py`) stages only the named paths. Track the changed-path set per batch-fix cycle and commit only those. The orchestrator's final commit and deliver's commit likewise move to `commit_paths`. `write_files_and_commit` (with `git add -A`) is kept for callers that genuinely write a full set (setup phase), but the execution-loop and deliver call sites move off it.~~ **Deferred to a follow-up issue:** part (a) does not ship in this PR. The whole-tree commits it targets were already eliminated by the prior `shared/` collapse (commit `aaf2f117`), so the incremental-`commit_paths` wiring no longer buys a meaningful win and is deferred. Parts (b) and (c) ship here.

**(b) Port `_RepoContextCache`.** New `software_engineering_team/shared/repo_context_cache.py` lifts the incremental `(mtime_ns, size, rendered_part)` cache from `coding_team/orchestrator.py:369-423` (key by `(st.st_mtime_ns, st.st_size)`, re-render only changed files, wholesale-replace `self._entries` to evict removed files, cap eligible files). The cache is constructed once per job at the coding-team worker seam (`v2_team_worker.py:375`, which currently `del`s the `repo_context` arg) and threaded into the `*DevelopmentAgent`. `_read_repo_code` consults the cache instead of re-walking.

**(c) Stream `read_repo_code_budgeted`.** Replace the materialized `rglob("*")` walk with a lazy `os.walk` that prunes excluded dirs in-place and stops at the char budget — mirroring `_RepoContextCache`'s `_enumerate_context_files`. Same output (same files, same sort order, same budget semantics).

### Behavior contract
Same files written, same commits, same `existing_code` string. Only the amount of I/O and walking changes.

### Affected files
- New: `software_engineering_team/shared/repo_context_cache.py`
- Modified: `shared_repo_context/repo_utils.py`; both v2 `orchestrator.py` (`_read_repo_code`); `coding_team/v2_team_worker.py` (thread the cache instead of discarding).
- Deferred (follow-up issue): part (a) — both v2 `phases/execution.py` write/commit sites and `shared/phases/deliver.py` moving to `commit_paths` do not ship in this PR.
- Tests: streaming-walk test, repo-context-cache test.

### Risk
Low–medium. `_RepoContextCache` already exists and is production-proven; this is wiring. Guard: assert cached `existing_code` equals fresh-walk output for the same on-disk state. (Part (a)'s `commit_paths` wiring is deferred — see above.)

---

## Refactor #4 — Move trace persistence and provider resets off the LLM hot path

### Problem
- `software_engineering_team/shared/trace_store.py:147` — `_trace_observer` calls `write_trace(record)` synchronously, which opens `pg_cursor()` and runs a single-row 16-column `INSERT INTO se_agent_traces` (`trace_store.py:55-78`) on the LLM-call thread. When `SE_TRACE_TO_POSTGRES` is on, **every LLM call does a blocking Postgres round-trip before returning** — contradicting the guidance at `llm_service/telemetry.py:25-28`.
- `llm_service/provider_store.py:636` — `select_active_entry` calls `reset_entry(entry.id)` synchronously on the call path when a limited entry's `reset_at` has elapsed; `reset_entry` (569-604) runs a conditional `UPDATE` + `commit()` + `clear_cache()`, cascading into a full list re-read + re-decrypt on the next call. `mark_exhausted` (537) is the companion sync write.
- `_emit_otel_llm_span` (`telemetry.py:347-419`) is synchronous but must stay so for span context/latency correctness — out of scope per decision.

### Approach (decision: batch traces + shutdown drain; provider reset deferred to a later PR)
**(a) Batched trace flusher.** New `software_engineering_team/shared/trace_flusher.py`:
- Bounded in-memory buffer (`collections.deque`, cap `SE_TRACE_BUFFER_MAX`, default 1000; overflow drops oldest + logs at WARNING — bounded memory, never blocks the caller).
- `_trace_observer` (registered exactly as today via `register_call_observer`) **enqueues** a copied record into the deque instead of INSERTing. No DB I/O on the call path.
- A `BackgroundHeartbeat` (`shared_concurrency/heartbeat.py`, existing primitive) drains the deque on interval `SE_TRACE_FLUSH_INTERVAL_S` (default 2.0, mirrors `SE_COST_FLUSH_INTERVAL_S`) using `pg_cursor()` + `cur.executemany` with the existing 16-column INSERT. Failures swallowed + logged (never raise into the flusher thread).
- `drain()` + `unregister()` called from `_se_shutdown()` (`api/lifecycle.py:35-46`), which runs **before** `close_pool()` (verified at `shared_app/factory.py:130-139`) — so the final flush can still use the pool.
- Column order + positional params preserved exactly from `trace_store.py:55-78`. `write_trace` remains as a sync one-shot for any non-observer caller (e.g. tests, manual tools).

This refactor is SE-scoped only (`software_engineering_team/shared` + `api/lifecycle.py`) — no shared `llm_service` changes, so no cross-team blast radius.

**Deferred to a separate later PR (#4b):** moving `provider_store.reset_entry`/`mark_exhausted` off the LLM call path. That touches the shared `llm_service` used by all teams and is isolated to its own PR with its own issue.

### Behavior contract
- Trace rows: identical contents, eventually-consistent within `SE_TRACE_FLUSH_INTERVAL_S` + drain-at-shutdown. No row loss on clean shutdown.
- Provider failover: unchanged in this PR (reset_entry stays synchronous; deferred to #4b).

### Affected files
- New: `software_engineering_team/shared/trace_flusher.py`.
- Modified: `shared/trace_store.py` (observer enqueues instead of INSERTing; keep `write_trace`); `api/lifecycle.py` (`_se_shutdown` drain/unregister).
- Tests: flusher buffer/overflow/drain test; executemany column-order test; regression that the observer never blocks the LLM call path (zero DB I/O on enqueue).

### Risk
Low. SE-scoped only; no shared `llm_service` changes. Mitigation: `write_trace` kept for non-observer callers; drain runs before `close_pool()`; existing trace tests pass against the batched path.

---

## Refactor #5 — Finish decomposing `orchestrator.py`

### Problem
`software_engineering_team/orchestrator.py` is 2103 lines. Commit d72cec0c moved Discovery out but left ~900 lines of dead/test-only legacy helpers and a ~560-line live build-fix cluster inline.

### Verified dead code (zero production callers)
| Function | Lines | Callers |
|---|---|---|
| `_run_tech_lead_review` | 698-764 | none (not even tests) |
| `_run_code_review` | 766-799 | `test_orchestrator_review_input.py` only |
| `_pop_runnable_task` | 1360-1378 | `test_orchestrator_helpers_coverage.py` only |
| `_maybe_ship_sprint_release` | 1381-~1513 | `test_release_hook.py` only |
| `_log_task_completion_banner` | 420-455 | `test_orchestrator_helpers_coverage.py` only |
| `_log_agent_crash_banner` | 484-510 | `test_orchestrator_helpers_coverage.py`, `test_repair_agent.py` |
| `_apply_repair_fixes` | 513-548 | `test_orchestrator_helpers_coverage.py`, `test_repair_agent.py` |
| `_log_task_breakdown` | 551-596 | `test_orchestrator_helpers_coverage.py` only |

`shared/planning_cache.py` (`get_cached_plan`/`set_cached_plan`/`compute_planning_cache_key`) has zero production callers — referenced only by `tests/test_shared_more.py` and `tests/test_planning_cache_sprint_id.py`.

### Verified live code
- `_run_build_verification` (802-997) — production caller `quality_gate_tools.py:182,184` (imported + called); also `test_software_engineering_orchestrator.py:59`, `test_quality_gate_tools.py` (monkeypatch).
- `_try_build_fix_one_at_a_time` (1000-~1358) — called internally by `_run_build_verification` at orchestrator.py:838/874/925; transitively live.
- `_build_coding_team_plan_input` (668-695) — `run_orchestrator:1858`, `temporal/activities.py:570`.
- `_read_repo_code` (664, alias) — `run_orchestrator:1855`, `temporal/activities.py:566`.
- Call chain: `run_orchestrator` → `run_coding_team_orchestrator` → `swarm_implementation.py:534` → `SECodeEngineProvider.run_build_verification` (`coding_engine_provider.py:65`) → `quality_gate_tools.run_build_verification` (`:170`) → `orchestrator._run_build_verification` (`:184`) → `_try_build_fix_one_at_a_time`.

### Approach (decision: delete dead + delete release hook + fix docs + extract build-fix)
**(a) Delete the 8 dead functions** and their test-only call sites. For each affected test file, keep tests that exercise live code, drop tests that exist solely for deleted functions:
- `test_orchestrator_helpers_coverage.py` — drop `_pop_runnable_task`/`_log_task_completion_banner`/`_log_agent_crash_banner`/`_apply_repair_fixes`/`_log_task_breakdown` cases; keep any live-code cases.
- `test_orchestrator_review_input.py` — exists solely for `_run_code_review`; delete the file.
- `test_release_hook.py` — exists solely for `_maybe_ship_sprint_release`; delete the file.
- `test_repair_agent.py` — keep live portions, drop the `_log_agent_crash_banner`/`_apply_repair_fixes` cases.

**(b) Delete `shared/planning_cache.py`** + `tests/test_planning_cache_sprint_id.py` + the planning-cache cases in `test_shared_more.py`.

**(c) Delete `_maybe_ship_sprint_release`** + `test_release_hook.py`; correct the stale docs:
- `CLAUDE.md` §Architecture "Planning cache: Short-circuits Design phase…" → remove.
- `CLAUDE.md`/`docs/ARCHITECTURE.md` §11 Product Delivery Loop — stop claiming a live release hook; state that the SE-side release hook is not currently wired (a future feature).

**(d) Extract the build-fix cluster** to new `software_engineering_team/build_fix.py` containing `_run_build_verification` + `_try_build_fix_one_at_a_time` (names preserved — `quality_gate_tools.py:170` already defines a *public* `run_build_verification` wrapper that calls the underscore form, so the extracted function keeps its `_`-prefix to avoid a name collision with that wrapper). Update:
- `quality_gate_tools.py:182` import → `from software_engineering_team.build_fix import _run_build_verification`.
- `test_software_engineering_orchestrator.py`, `test_quality_gate_tools.py` monkeypatch targets.
- Any other verified importer (none production beyond `quality_gate_tools`).

After (a)+(d), `orchestrator.py` drops from 2103 → ~1100–1200 lines.

### Behavior contract
No change to live paths. Dead code removal is behavior-preserving by definition. Doc corrections align docs with the verified runtime.

### Affected files
- New: `software_engineering_team/build_fix.py` (exports `_run_build_verification`, `_try_build_fix_one_at_a_time`)
- Modified: `software_engineering_team/orchestrator.py` (shrink); `software_engineering_team/quality_gate_tools.py` (import); `CLAUDE.md`; `docs/ARCHITECTURE.md` §11.
- Deleted: `software_engineering_team/shared/planning_cache.py`; `tests/test_planning_cache_sprint_id.py`; `tests/test_orchestrator_review_input.py`; `tests/test_release_hook.py`.
- Modified tests: `tests/test_orchestrator_helpers_coverage.py`; `tests/test_repair_agent.py`; `tests/test_shared_more.py`.

### Risk
Low for deletions (verified dead). Medium for the build-fix extraction (touches a live production import + tests) — mitigated by keeping the public name stable and re-pointing imports atomically.

---

## Implementation order

Single PR, single tracking issue. Land the work in this commit order within the PR so each stage is independently testable and diffs stay reviewable:

1. **#5** — pure deletions + build-fix extraction; shrinks `orchestrator.py` before the other refactors touch it. Lowest risk, unblocks cleaner diffs.
2. **#4a** — trace batching (SE-scoped); independent of #2/#3 execution code.
3. **#2** — collapse the **review** fork behind `ReviewConfig` (the `shared/v2_review.py` body). The **execution**-fork collapse (`run_gated_execution_impl`) is deferred to a follow-up issue (see Non-goals).
4. **#3** — `_RepoContextCache` port + streaming walk. The changed-path `commit_paths` wiring (part (a)) is deferred to a follow-up issue (see Non-goals).

The PR body uses `Closes #N` against the single tracking issue. Coverage must stay at/above the 90% line floor across the stages that ship. #4b (provider_store reset off-path) is a separate future PR with its own issue.

## Non-goals (explicitly deferred)
- Prompt caching for stable LLM context (#1 from the analysis) — separate effort.
- `all_files` content eviction / paths-only deliver (#3 "also paths-only" option) — deferred per decision.
- OTEL metric recording off-path — deferred per decision.
- **#4b — `provider_store.reset_entry`/`mark_exhausted` off the LLM call path** — split into its own later PR (shared `llm_service` blast radius).
- Converging backend/frontend gate models (#2 full-convergence option) — deferred per decision.
- **#2 execution skeleton — `run_gated_execution_impl` in `shared/phases/execution.py`** — the execution-loop fork collapse is split into a follow-up issue; this PR ships only the `run_review` / `run_microtask_review` collapse in `shared/v2_review.py`.
- **#3 part (a) — incremental `commit_paths` wiring** — the execution-loop and deliver commit sites moving to `commit_paths` is split into a follow-up issue; the whole-tree commits it targeted were already eliminated by the prior `shared/` collapse (commit `aaf2f117`), so it no longer buys a meaningful win. Parts (b) and (c) ship here.
- `TaskGraphService` RLock-over-HTTP-persist, `_pause_lock` over human wait, worktree-cleanup failure sweep, `MAX_DOCUMENTATION_ITERATIONS=100` — flagged in the analysis but out of scope here.