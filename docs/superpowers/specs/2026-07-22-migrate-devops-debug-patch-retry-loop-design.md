# Migrate devops_team debug-patch loop to BaseTeamLead retry extension

**Date:** 2026-07-22  
**Status:** Approved for implementation planning  

## Goal

Rewrite `DevOpsTeamLeadAgent`'s Phase 4.6 debug-patch retry loop to use
`BaseTeamLead._run_bounded_retry_loop`, preserving the 3-iteration bound and the
existing `infra_debug_agent` → `infra_patch_agent` → re-validate sequence.

## Motivation

`BaseTeamLead._run_bounded_retry_loop` already exists as a shared bounded
retry/patch-loop helper. The devops orchestrator still uses a bespoke inline
`for` loop with the same semantics (soft-abort via break, success when
execution failures clear). Migrating onto the helper removes the one-off
control flow without changing pipeline behavior.

## Decisions (locked)

| Decision | Choice |
|---|---|
| Invocation style | Unbound `BaseTeamLead._run_bounded_retry_loop(self, …)` — same pattern as `_run_gated_phases` (DevOps stays on `TeamLeadSharedState`) |
| Attempt body | Private instance method `_debug_patch_once` |
| Cross-iteration state | Mutable `_DebugPatchState` dataclass mutated in place |
| `aggregated_artifacts` | Separate mutable dict updated in place on successful patches (shared with rest of pipeline) |
| Soft abort | `_debug_patch_once` returns `None` (debug/patch exception, not fixable, empty patches) |
| Success check | `is_success=lambda s: not s.exec_failures` |
| Empty initial failures | Skip calling the helper entirely (no Phase 4.6 work) |
| Inheritance change | Out of scope — do not make `DevOpsTeamLeadAgent` subclass `BaseTeamLead` |

## Architecture

### Files touched

| Path | Change |
|---|---|
| `backend/agents/software_engineering_team/devops_team/orchestrator.py` | Add `_DebugPatchState` + `_debug_patch_once`; replace Phase 4.6 inline loop with unbound helper call |
| `backend/agents/software_engineering_team/tests/test_devops_*.py` | Only if coverage on `orchestrator.py` falls below 90% |

### Files not touched

- `infra_debug_agent` / `infra_patch_agent` internals
- `shared/team_lead_base.py` (`_run_bounded_retry_loop` already present)
- Phase 1–3 / broader Phase 4 template migrations

### Call site (after Phase 4.5)

```python
MAX_INFRA_FIX_ITERATIONS = 3
exec_failures = [er for er in exec_results if not er.get("success", True)]
state = _DebugPatchState(
    exec_results=exec_results,
    exec_failures=exec_failures,
    exec_gate_map=exec_gate_map,
    exec_findings=exec_findings,
)
if state.exec_failures:
    BaseTeamLead._run_bounded_retry_loop(
        self,
        max_iterations=MAX_INFRA_FIX_ITERATIONS,
        attempt=lambda i: self._debug_patch_once(
            i,
            state=state,
            aggregated_artifacts=aggregated_artifacts,
            repo_path=repo_path,
            repo_str=repo_str,
            write_changes=write_changes,
            subdir=subdir,
            max_iterations=MAX_INFRA_FIX_ITERATIONS,
        ),
        is_success=lambda s: not s.exec_failures,
    )
tool_gate_map.update(state.exec_gate_map)
# downstream review/validation continues unchanged
```

### State bag

```python
@dataclass
class _DebugPatchState:
    exec_results: List[Dict[str, Any]]
    exec_failures: List[Dict[str, Any]]
    exec_gate_map: Dict[str, str]
    exec_findings: List[str]
```

### `_debug_patch_once` contract

```python
def _debug_patch_once(
    self,
    fix_iter: int,
    *,
    state: _DebugPatchState,
    aggregated_artifacts: Dict[str, str],
    repo_path: Path,
    repo_str: str,
    write_changes: bool,
    subdir: Optional[str],
    max_iterations: int,
) -> Optional[_DebugPatchState]:
```

Preconditions:
- `fix_iter` is a 0-based iteration index supplied by the helper
- `state.exec_failures` is non-empty when the helper invokes this method
- `max_iterations >= 1`

Postconditions:
- Soft abort → log (same messages as today) and return `None`
- Otherwise → update `aggregated_artifacts` and `state` from the patch + re-exec,
  then return `state` (caller uses `is_success` on `state.exec_failures`)

Body is a straight lift of the current loop body: status report → debug agent →
patch agent → optional write → `_run_execution_tools` → refresh gate map / findings.

## Error handling

Unchanged soft-fail semantics. `_debug_patch_once` catches debug/patch exceptions,
logs warnings, and returns `None` so the helper aborts. Non-fixable debug output
and empty patch sets also return `None`. Exceptions outside those soft-fail
`try` blocks propagate unchanged (helper does not catch).

## Testing

- `test_devops_team.py` and `test_devops_debug_patch.py` pass unchanged.
- Add focused `_debug_patch_once` unit coverage only if `orchestrator.py` drops
  below the 90% floor.
- Verification: `make test` and `make lint` from `backend/`.

## Out of scope

- Any change to `infra_debug_agent` / `infra_patch_agent` logic
- Subclassing `BaseTeamLead` from `DevOpsTeamLeadAgent`
- Phase 1–3 / Phase 4 sibling migrations under the parent epic
- Changing `_run_bounded_retry_loop` itself
