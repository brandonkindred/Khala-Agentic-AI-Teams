# Design: Correct `planning_audit` Test Docstring Path

**Branch / worktree:** `docs/2139-planning-audit-docstring` / `.worktrees/issue-2139-planning-audit-docstring`  
**Scope decision:** Approach C — leave `writer.py` unchanged (path already correct); fix only the misleading test-module docstring.

## Goal

Close the documentation triage after verifying that `planning_team.postgres.writer` correctly cites `software_engineering_team.shared.planning_audit`, and align the related test module header with that same import path so readers are not pointed at a non-existent platform `shared.planning_audit`.

## Verification (already done)

| Check | Result |
|-------|--------|
| Module file | `backend/agents/software_engineering_team/shared/planning_audit.py` exists |
| Platform `backend/shared/` | No `planning_audit` export |
| Live imports / patches | Use `software_engineering_team.shared.planning_audit` / `from software_engineering_team.shared import planning_audit` |
| `writer.py` module docstring | Already cites `software_engineering_team.shared.planning_audit` — correct |

## Non-Goals

- No behavioral changes.
- No module moves or import rewrites.
- No edits to `backend/agents/planning_team/postgres/writer.py`.
- No new tests; docs-only change.

## File Touch

| File | Change |
|------|--------|
| `backend/agents/software_engineering_team/tests/test_planning_audit.py` | Module docstring: `(shared.planning_audit)` → `(software_engineering_team.shared.planning_audit)` |

## Exact Edit

Replace:

```python
"""Unit tests for the SE planning_runs audit helper (shared.planning_audit)."""
```

with:

```python
"""Unit tests for the SE planning_runs audit helper (software_engineering_team.shared.planning_audit)."""
```

## Verification

Docs-only; confirm the single-line edit and that `writer.py` remains untouched. Optional: `rg planning_audit` still shows the SE-team path as the sole real module location.

## Acceptance Mapping

| Criterion | How met |
|-----------|---------|
| Issue path verified | Verification table above |
| Docstring accurate if path moved | Path did not move; `writer.py` left as-is |
| Related misleading docstring fixed | Test module header uses full import path |
| Linked triage issue closed | PR body uses GitHub auto-close keyword for the associated issue |
