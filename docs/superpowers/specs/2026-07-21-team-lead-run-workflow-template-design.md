# Team-lead `run_workflow` template

**Date:** 2026-07-21  
**Status:** Approved for implementation planning

## Goal

Consolidate the near-identical `BackendCodeV2TeamLead.run_workflow` and
`FrontendCodeV2TeamLead.run_workflow` bodies into one shared helper on
`BaseTeamLead`, leaving each team lead as a thin wrapper that supplies its
module-level `run_setup`, development-agent class, and result class.

## Motivation

The two `run_workflow` implementations differ only in class/result names.
`BaseTeamLead`'s docstring already flagged this convergence as a separate,
test-guarded change. Carrying two copies invites accidental drift (already
observed once on setup `status_text` wiring).

## Decisions (locked)

| Decision | Choice |
|---|---|
| Shape | Shared helper `_run_setup_and_delegate` on `BaseTeamLead`; thin subclass `run_workflow` wrappers |
| Parameterization | Call-site args: `result_cls`, `run_setup_fn`, `development_agent_cls` (late-bound module globals) |
| Setup progress 2/3 `status_text` | Keep backend strings; apply to both teams |
| `_update_job` exception handling | DEBUG log (already consistent on both teams); no silent `pass` |
| Monkeypatch surface | Preserved: wrappers pass `run_setup` / `*DevelopmentAgent` looked up in each team's orchestrator module |
| Out of scope | `copy_development_result_fields` semantics; devops/coding_team migration |

### Status text (canonical)

| Progress | `status_text` |
|---|---|
| 2 | `"Setting up repository and development environment"` |
| 3 | `"Repository setup complete"` |
| 5 | `"Linting and testing verified; ready for development"` (unchanged) |

## Architecture

```
BackendCodeV2TeamLead.run_workflow ──┐
                                     ├──► BaseTeamLead._run_setup_and_delegate(...)
FrontendCodeV2TeamLead.run_workflow ─┘         │
                                               ├─ result_cls(task_id=...)
                                               ├─ _update_job (DEBUG on failure)
                                               ├─ setup phase (progress 2/3 + status_text)
                                               ├─ run_setup_fn(...)
                                               ├─ lint/test gates
                                               ├─ progress 5
                                               ├─ development_agent_cls(self.llm).run_workflow(...)
                                               └─ copy_development_result_fields → return
```

Subclasses remain responsible only for:

1. Passing their team-specific `result_cls`, `run_setup_fn`, and `development_agent_cls`
2. Forwarding the public `run_workflow` kwargs unchanged

Why thin wrappers instead of class attributes: tests monkeypatch
`orchestrator.run_setup` and `orchestrator.*DevelopmentAgent` as module-level
attributes. Passing those names from the subclass method body keeps late
binding; class attributes bound at import time would break those patches.

## Error handling

Unchanged relative to today's backend path (which becomes the shared path):

- Setup exception → set `failure_reason`, ERROR log, return early
- Missing linting or testing config → WARNING log, set `failure_reason`, return early
- `job_updater` exception inside `_update_job` → DEBUG log, continue

## Testing

- Existing `test_team_lead_propagates_development_handoff_fields` (backend + frontend)
  must keep passing without changing their monkeypatch surface.
- New unit coverage on `_run_setup_and_delegate` in `test_team_lead_base.py`:
  - happy path (setup OK → agent called → fields copied)
  - setup exception → early failure
  - linting / testing not configured → early failure
  - `_update_job` exception → DEBUG log, workflow continues
  - progress 2/3 kwargs include the canonical `status_text` strings
- No changes to `copy_development_result_fields` tests or field list.
- Gate: `test_team_lead_base.py`, `test_backend_code_v2_team.py`,
  `test_frontend_code_v2_team.py`, then `make test` / `make lint` from
  `backend/`; 90% coverage floor on touched files.

## Out of scope

- Any change to `copy_development_result_fields`'s field list or semantics
- Migrating `devops_team` / `coding_team` onto this template
- Renaming public `run_workflow` APIs on either team lead

## Implementation notes

- Update `BaseTeamLead` module/class docstrings: remove the "run_workflow stays
  per-team" deferral; document the thin-wrapper + late-bound globals pattern.
- Frontend setup progress 2/3 gains `status_text` as the intentional drift fix.
- DbC: document Preconditions/Postconditions on `_run_setup_and_delegate`.
