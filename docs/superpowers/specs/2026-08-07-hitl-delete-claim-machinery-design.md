# Design: Delete coding-team resume claim/heartbeat/thread machinery

Date: 2026-08-07

## Goal

Remove the bespoke claim/heartbeat/liveness thread machinery used for
coding-team resume once Temporal-native signal paths own pause/resume. After
this change, no production code calls the deleted helpers, and `/resume` /
auto-resume no longer spawn orchestrator threads.

## Context

Parent epic: coding-team HITL Temporal redesign. Dependencies already on
`main`:

- GitHub activities wired into `CodingTeamWorkflow`
- `POST /run/{job_id}/resume` signals when `resume_token` is present
- `_try_auto_resume` can signal Temporal-native jobs

`/run-from-github` already starts `CodingTeamWorkflow` (not `_start_hook_thread`).
Thread-mode claim+spawn remains only as the fallback half of `/resume` and
`_try_auto_resume` for jobs without `resume_token`.

Out of scope: `job_store.claim_resume` / `release_resume_claim` /
`RESUME_CLAIM_TTL_S` (sibling cleanup).

## Decisions

| Topic | Choice |
|---|---|
| Thread-mode `/resume` | Remove; require `resume_token` and signal only; missing token → 400 |
| Thread-mode `/answers` auto-spawn | Remove; without `resume_token`, store answers only (no `_try_auto_resume`) |
| `_try_auto_resume` | Delete (no remaining callers after route changes) |
| `_schedule_resume_recheck` | Delete (issue list; no callers once claim/heartbeat deferral is gone) |
| `_running_job_for_issue` | Keep (job-store active-job scan; still used by `/run-from-github`) |
| `job_store` claim APIs | Leave for sibling issue |
| Tests | Update/remove in this change so CI stays green (necessary for deletion) |

## Behavior

### `POST /run/{job_id}/resume`

1. 404 if job missing; 400 if terminal (unchanged).
2. If `resume_token` present and `status == waiting_for_user`: signal
   `submit_answers` as today; return `"Job resumed."`
3. If `resume_token` present but not waiting: existing non-paused 400.
4. If `resume_token` absent: **400** with a clear detail that only a
   Temporal-native paused job (`resume_token` set) can be resumed this way.
5. No thread liveness, heartbeat, plan recovery, GitHub token, or
   `_claim_and_spawn_resume`.

### `POST /run/{job_id}/answers`

1. Temporal path (`resume_token` on job): unchanged — validate token, append
   answers, signal workflow.
2. Non-Temporal path: `store_submit_answers` only; return status. Do **not**
   call `_try_auto_resume`, do not write “resuming the run” / “call /resume”
   spawn-oriented status_text tied to thread restart.

### Deletions (`orchestration.py` + `coding_team_main` re-exports)

Delete definitions and all re-exports of:

- `_claim_and_spawn_resume`
- `ResumeSpawnResult`
- `_schedule_resume_recheck`
- `_spawn_run_thread`
- `_start_orchestrator_thread`
- `_start_github_resume_thread`
- `_start_hook_thread`
- `_try_auto_resume`

Also remove imports/usages of those symbols from `coding_team_hitl.py` and any
other production modules. Leave docstring-only historical mentions only if they
do not name deleted callables as live APIs; prefer updating comments to avoid
stale references.

### Retention

- `_running_job_for_issue` — unchanged public behavior
- `_recover_resume_plan`, `_resolve_github_job_token`, `run_orchestrator_wired`,
  GitHub-hook run helpers still needed by Temporal activities / other flows —
  delete only if they become unused *and* are in the issue list (they are not)

## Testing

- Delete or rewrite unit tests that exercise claim/spawn/recheck/thread-start /
  `_try_auto_resume` thread paths.
- Keep Temporal `/resume` and `/answers` signal tests.
- Add/adjust: `/resume` without `resume_token` returns 400.
- Add/adjust: `/answers` without `resume_token` stores answers and does not
  signal or spawn.
- Acceptance check: `rg` over production `software_engineering_team/` (exclude
  `tests/`) finds no matches for deleted symbol names.

## Files

| File | Change |
|---|---|
| `api/routes/coding_team_hitl.py` | Temporal-only `/resume`; slim non-Temporal `/answers` |
| `api/orchestration.py` | Delete listed helpers + `_try_auto_resume` |
| `api/coding_team_main.py` | Drop re-exports |
| `tests/test_coding_team_api_hitl.py` (and related) | Remove/rewrite claim/spawn/thread tests; cover new 400 / store-only paths |

## Non-goals

- Deleting `job_store` claim APIs (#3997).
- Early `submit_answers` buffering.
- WorkflowEnvironment pause→signal suite.
- Changing `/run-from-github` Temporal start path.
