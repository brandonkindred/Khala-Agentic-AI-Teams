# Extract Documentation-Phase Block into BaseV2DevelopmentAgent

**Status:** Approved 2026-07-21  
**Date:** 2026-07-21  
**Type:** Structural refactor (behavior-preserving)  
**Tracks:** Extract documentation-phase block under the team-lead orchestration unify epic

## Problem

`BackendDevelopmentAgent.run_workflow` and `FrontendDevelopmentAgent.run_workflow` each contain a near-byte-identical documentation-phase block (~30 lines). The only real difference is the job `status_text`:

| Team | `status_text` |
|------|----------------|
| Backend | `"Generating documentation and API specs"` |
| Frontend | `"Generating documentation and API docs..."` |

This duplication is part of the 373-line backend/frontend orchestrator diff tracked under the team-lead orchestration unify epic.

## Goals

1. Define the documentation-phase block once on `BaseV2DevelopmentAgent` in `shared/v2_orchestrator.py`.
2. Have both teams' `run_workflow` call the shared helper with their existing status string.
3. Preserve runtime behavior (phase order, job updates, file merge, exception swallowing) and keep existing team tests passing unchanged.

## Non-goals

- Extracting post-execution bookkeeping or deliver-phase + logging (sibling extractions).
- Introducing `_TEAM_LABEL` (deferred to the deliver-phase extraction — these status strings are not derivable from `"Backend"`/`"Frontend"` alone).
- Changing `run_documentation_phase` itself or any documentation tool-agent behavior.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Status parameterization | `status_text: str` kwarg | Matches `_build_progress_callback(review_label=...)`; preserves exact live strings |
| Phase callable | Inject `run_documentation_phase` | Late import in each `run_workflow` keeps monkeypatches on `phases.documentation` working |
| Mutation style | Mutate `result` + `current_files` in place; return `current_files` | Callers already own both; return keeps the updated dict available for deliver |
| `_TEAM_LABEL` | Out of scope | Belongs to the deliver-phase extraction |

## Design

### `BaseV2DevelopmentAgent._run_documentation_phase`

Static helper (same style as `_run_preflight` / `_run_planning_and_branch_setup`) that:

1. Logs `"Next step -> Starting Phase: Documentation"`.
2. Sets `result.current_phase = Phase.DOCUMENTATION` (shared `v2_models.Phase`).
3. Calls `update_job(current_phase="documentation", progress=80, status_text=status_text)`.
4. Invokes the injected `run_documentation_phase(...)` with the same kwargs used today.
5. On success: sets `result.documentation_result`; if `doc_result.files` is truthy, updates `current_files` and `result.final_files`; logs completion summary.
6. On exception: logs the existing warning (`Documentation phase failed: ... Continuing to Deliver phase`) and does not raise.

### Call sites

Each orchestrator replaces its inline block with:

```python
from .phases.documentation import run_documentation_phase

current_files = self._run_documentation_phase(
    task_id=task_id,
    task=task,
    repo_path=repo_path,
    llm=self.llm,
    exec_result=exec_result,
    planning_result=planning_result,
    tool_agents=tool_agents,
    result=result,
    current_files=current_files,
    run_documentation_phase=run_documentation_phase,
    update_job=_update_job,
    logger=logger,
    status_text="<team-specific string unchanged>",
)
```

## Testing

- Add `TestRunDocumentationPhase` in `test_v2_orchestrator_helpers.py` covering: success with files, success without files, and exception swallow + warning log.
- Existing `test_backend_code_v2_team.py` / `test_frontend_code_v2_team.py` / helper tests must pass unchanged (they patch `phases.documentation.run_documentation_phase`).

## Success criteria

- Shared helper exists; both orchestrators call it; no duplicated documentation-phase block remains.
- Status text remains team-specific and byte-identical to today.
- `make test` and `make lint` pass from `backend/`; 90% coverage floor holds for touched files.

## Risk

Low. Mechanical extraction with injected callables; failure mode is breaking monkeypatch seams — mitigated by injecting `run_documentation_phase` from the late import at each call site.
