# Design: Align `_report_status` “Never Raises” Wording

**Issue:** #2268  
**Branch / worktree:** `docs/2268-report-status-docstring`  
**Date:** 2026-07-24

## Problem

`DevOpsTeamLeadAgent._report_status` in
`backend/agents/software_engineering_team/devops_team/orchestrator.py`
documents Postconditions ending with “Never raises into the caller”, but the
method begins with `assert isinstance(phase, str) and phase, ...`, which raises
`AssertionError` when the precondition is violated. The docstring and the
implementation contradict each other.

The same wording/assert pattern exists on
`TeamLeadSharedState._report_status` in
`backend/agents/software_engineering_team/shared/team_lead_base.py`. Scope
includes both call sites so the contract stays consistent.

Sibling `_log_pipeline_status` in the same orchestrator already uses the
correct DbC phrasing: “Never raises when preconditions hold.”

## Goal

Make both `_report_status` Postconditions match that sibling wording so
“never raises” is understood only when preconditions hold. Keep the asserts;
they are intentional Design-by-Contract enforcement (covered by
`test_report_status_rejects_empty_phase`).

## Non-goals

- No production logic changes.
- No assert removal or softening.
- No new tests (documentation-only change).
- No edits to `_log_pipeline_status` (already correct).

## Design

### Files touched

1. `backend/agents/software_engineering_team/devops_team/orchestrator.py` —
   `DevOpsTeamLeadAgent._report_status` docstring only.
2. `backend/agents/software_engineering_team/shared/team_lead_base.py` —
   `TeamLeadSharedState._report_status` docstring only.

### Docstring update

In both Postconditions blocks, replace:

```text
Never raises into the caller.
```

with:

```text
Never raises when preconditions hold.
```

Leave Preconditions and the rest of each Postconditions block unchanged.

## Testing

No automated test for docstring text. After the edit, re-read both methods and
confirm:

1. The new phrase appears in each Postconditions block.
2. Method bodies (including asserts) are unchanged.
3. Existing focused status-hook / reject-empty-phase tests still pass.

## Success criteria

1. Both `_report_status` docstrings say “Never raises when preconditions hold.”
2. Neither still says “Never raises into the caller.”
3. Asserts and runtime behavior unchanged.
4. No other files modified for the fix itself (spec/plan docs excluded).
