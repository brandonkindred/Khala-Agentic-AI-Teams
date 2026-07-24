# Finish devops_team migration onto BaseTeamLead shared hooks

**Date:** 2026-07-24  
**Status:** Approved for implementation planning  
**Type:** Structural refactor (behavior-preserving)  
**Branch / worktree:** `refactor/2015-devops-baseteamlead-closeout` / `.worktrees/issue-2015-devops-baseteamlead-closeout`

## Goal

Close out the devops_team → BaseTeamLead shared-hooks migration by folding Phase 5 (completion package + deliver/merge) into the outer `_run_gated_phases` sequence and routing merge failure through `build_team_failure_result`. Keep `DevOpsTeamLeadAgent` on `TeamLeadSharedState` with unbound helper aliases — the same stance as the Phase 1–4 and retry migrations. Preserve pipeline behavior exactly.

## Motivation

Phases 1–4, status reporting, and the debug-patch retry loop already consume BaseTeamLead helpers. Phase 5 remains a post-sequencer inline block that hand-builds a merge-failure `DevOpsTeamResult`. The prior Phase 4 migration design explicitly deferred Phase 5 to this closeout. Finishing the outer sequencer and shared failure envelope completes the migration without changing devops features.

## Decisions (locked)

| Decision | Choice |
|---|---|
| Inheritance | Stay on `TeamLeadSharedState` + unbound `BaseTeamLead` helpers (do **not** subclass `BaseTeamLead`; constructor still requires code-v2 briefing knobs that DevOps does not use) |
| Acceptance “derives from BaseTeamLead” | Satisfied via end-to-end API consumption, not class inheritance — note the deviation in the PR body |
| Phase 5 shape | Extract `_phase5_completion_deliver` and append it to the outer `_run_gated_phases([...])` list |
| Success path | Phase 5 returns `None` after filling a nonlocal `completion`; thin `DevOpsTeamResult(success=True, …)` remains after the sequencer (because `_run_gated_phases` treats any non-`None` as failure) |
| Merge failure | `build_team_failure_result(DevOpsTeamResult, reason, completion_package=…)` with the same blocked-package shape as today |
| `copy_development_result_fields` | Not used — those fields are code-v2-only; DevOps uses the shared failure-envelope helper instead |
| Invocation style | Unbound `BaseTeamLead._run_gated_phases(self, …)` (unchanged from Phase 1–4) |
| Behavior | Side-effect order and result payloads unchanged |

## Architecture

### Files touched

| Path | Change |
|---|---|
| `backend/agents/software_engineering_team/devops_team/orchestrator.py` | Extract `_phase5_completion_deliver`; add it to the outer gated-phase list; route merge failure through `build_team_failure_result`; keep thin success return after sequencer |

### Files not touched

- `shared/team_lead_base.py` (helpers already landed)
- Phase 1–4 callables / gates / retry body
- Making `DevOpsTeamLeadAgent` subclass `BaseTeamLead`
- Other early-return sites still using hand-built `DevOpsTeamResult` (out of scope for this closeout)

### Target shape

```python
completion = None  # filled by _phase5_completion_deliver via nonlocal

early_exit = BaseTeamLead._run_gated_phases(
    self,
    [
        _phase1_intake_clarify,
        _phase2_parallel_design,
        _phase3_branch_write,
        _phase4_validation_review,
        _phase5_completion_deliver,
    ],
)
if early_exit is not None:
    return early_exit

assert completion is not None  # phase 5 success path always assigns it
return DevOpsTeamResult(success=True, iterations=1, completion_package=completion)
```

### Inside `_phase5_completion_deliver`

1. `_report_status("phase5", …)` (already on the shared status hook).
2. Run `doc_runbook_agent`; mutate `completion` (acceptance trace, release readiness) — same domain logic as today.
3. When `write_changes and aggregated_artifacts`: call `deliver_inline_merge`; on `not deliver_result.merged`, return `build_team_failure_result(...)` with the existing blocked `DevOpsCompletionPackage` fields; otherwise assign successful `git_ops`.
4. When not writing / no artifacts: leave neutral `GitOperationsMetadata()`.
5. Assign `completion.git_operations`, handoff, `status="completed"`, `quality_gates` into the nonlocal `completion`.
6. Return `None` so the outer thin success envelope runs.

### Data flow / error handling

- Closures already populated by Phases 1–4 (`quality_gates`, `aggregated_artifacts`, phase-2 agent results, `repo_path`, `write_changes`, `task_spec`) feed Phase 5 unchanged.
- Merge failure: Phase 5 returns the failure envelope → outer sequencer returns it → no success envelope.
- Success: Phase 5 returns `None` → thin success `DevOpsTeamResult` after sequencer.
- Exceptions from agents/tools/git helpers propagate unchanged (no new try/except).

### Behavioral preservation (must hold)

- Side-effect order before any early return is unchanged.
- Merge-failure package (blocked status, empty commit/merge hashes, notes, quality_gates, files_changed) is structurally identical to today.
- Success package (trace, release readiness, git ops, handoff, completed status) unchanged.
- `write_changes=False` skips git and keeps the neutral default `GitOperationsMetadata`.

## Testing

- Existing `test_devops_team.py` + `test_devops_debug_patch.py` pass without modification (especially delivery merge-failure and completion-package integration cases).
- No new unit tests required beyond that regression suite.
- Orchestrator coverage ≥ 90% for the touched file.
- From `backend/`: targeted pytest above; `make lint` / ruff on the touched path.

## Out of scope

- Subclassing `BaseTeamLead` from devops (constructor mismatch; prior migrations locked SharedState).
- Changing `_run_gated_phases` / `_run_phase_gates` / `_run_bounded_retry_loop` contracts.
- Migrating other hand-built early-return `DevOpsTeamResult` sites in Phases 1–4 to `build_team_failure_result`.
- coding_team migration (separate track).
- Any new devops feature work.
