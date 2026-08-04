# DevOps Orchestrator Consolidated Cleanup — Design

**Date:** 2026-07-29  
**Status:** Approved  
**Primary file:** `backend/agents/software_engineering_team/devops_team/orchestrator.py`  
**Tests:** `backend/agents/software_engineering_team/tests/test_devops_team.py` (and related devops unit tests as needed)

## Goal

Close documentation, Design-by-Contract (DbC), and small correctness gaps in the DevOps team lead orchestrator as one coherent pass. Stay inside `orchestrator.py` (+ devops tests). Do not rewrite `quality_gate.py` / `tool_dispatch.py`.

## Scope

### In scope

1. **DbC docstrings** on `__init__`, `_build_legacy_spec`, `_build_subtask_contracts`, and `_run_pipeline` (formal Preconditions / Postconditions / Invariants). Clarify `_report_status` that callback swallowing is owned by `TeamLeadSharedState._report_status`.
2. **Contract enforcement**
   - `_enforce_env_policy`: assert `platform_scope.environments` is iterable and `scope.included` is an iterable of strings before use.
   - Success path after Phase 5: replace `assert completion is not None` with an explicit `RuntimeError` (survives `python -O`).
   - Extract `MAX_LEGACY_TITLE_LENGTH = 120` and use it for legacy title truncation.
3. **Behavior**
   - Negation-aware production intent in `_build_legacy_spec` (token/context check so `non-production`, `not prod`, `no production` stay staging; bare `prod` / `production` map to production).
4. **API cleanup**
   - Remove unused `run_workflow` parameters: `architecture`, `existing_pipeline`, `tech_stack`, `max_iterations`, `devops_review_agent`.
   - Update the legacy-args compatibility test; keyword-probing callers (`v2_team_worker`) continue to work because they only pass accepted kwargs.
5. **Imports**
   - Remove module-level `# noqa: E402` late imports by importing agent / `tool_dispatch` / `debug_patch` dependencies lazily inside `__init__` (and a tiny helper or post-init attribute bind for the existing `_run_execution_tools` / `_debug_patch_once` call shape).

### Out of scope

- Refactors of `phases/quality_gate.py`, `tool_dispatch.py`, or the shared deliver helpers.
- Implementing discarded `run_workflow` parameters.
- Untangling the full DevOps dependency graph beyond lazy imports in the orchestrator.

### Already resolved on main (verify only)

- Dead `tool_gate_map` store: map now lives in `phases/quality_gate.py` and is returned on the Phase 4 result.
- `_phase4_validation_review` docstring already documents `acceptance_trace`.
- `_report_status` docstring’s “swallows callback errors” is accurate because the base mixin logs and swallows; wording will only be clarified to name the owner.

## Design decisions

| Decision | Choice | Rationale |
|---|---|---|
| Change surface | Single surgical PR on orchestrator + tests | Parent finding cluster is local; broader package churn adds risk without closing the cluster |
| Unused kwargs | Remove from signature | Signature was lying; keyword probing means cross-team callers stay safe |
| Late imports | Lazy inside `__init__` / bind helpers | Clears E402 suppressions without a multi-module circular-import untangle |
| Env classifier | Token + preceding-negation | Matches the filed suggested fix; low risk; tested |
| `-O`-safe invariant | `RuntimeError` | Same class of fix used elsewhere for runtime invariants |

## Implementation sketch

1. Add `MAX_LEGACY_TITLE_LENGTH` beside other legacy defaults.
2. Add missing / strengthen existing DbC docstrings.
3. Implement negation-aware env classification helper (private function or inline in `_build_legacy_spec`) and use it.
4. Add precondition asserts in `_enforce_env_policy`.
5. Replace completion `assert` with `RuntimeError`.
6. Narrow `run_workflow` signature; drop the discarded-tuple `_ = (...)`.
7. Move late imports into `__init__`; bind `_run_execution_tools` / `_debug_patch_once` after import so call sites unchanged.
8. Update / extend unit tests:
   - Legacy env: `production`, `non-production`, `not prod`, `no production`, staging default.
   - Env policy precondition (optional: non-string `included` raises AssertionError).
   - Legacy args test no longer passes removed kwargs.
   - Existing devops happy-path / status-hook suites remain green.

## Testing

- Targeted: `pytest` for `test_devops_team.py`, `test_devops_status_hook.py`, and any debug-patch tests that construct `DevOpsTeamLeadAgent`.
- Confirm module imports cleanly (lazy path does not break class attribute aliases used by tests).
- No frontend / docker changes.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Lazy import breaks monkeypatch of `tool_dispatch` / `debug_patch` at module boundary | Keep binding attributes on the class/instance the same way tests already patch (`devops_team.orchestrator` / agent methods) |
| Call sites still pass removed kwargs positionally | Grep + update the one known test; production callers use kwargs / probing |
| Negation heuristic misses edge phrases | Cover listed cases; keep false-positive avoidance for `produce` |

## Success criteria

- Orchestrator methods in scope have explicit DbC docstrings.
- Runtime invariants do not rely on `assert` for the Phase 5 success envelope.
- Legacy classifier negation cases pass.
- No `# noqa: E402` remains in `orchestrator.py`.
- DevOps unit suites above pass.
