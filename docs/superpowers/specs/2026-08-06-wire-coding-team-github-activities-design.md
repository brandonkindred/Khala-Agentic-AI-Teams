# Design: Wire CodingTeamWorkflow to GitHub activities

Date: 2026-08-06

## Goal

For GitHub-issue-driven runs, drive branch prep, the coding-team pipeline
(pause/resume loop), publish, and failure notice as Temporal activities from
`CodingTeamWorkflow`, and start that workflow from `POST /run-from-github`
instead of `_start_hook_thread`.

## Context

GitHub-hook activities already exist and are registered on the coding-team
worker (`coding_team_github_branch_prep`, `coding_team_github_publish`,
`coding_team_github_failure_notice`). They resolve tokens activity-side from
the encrypted job record or `GITHUB_TOKEN` and reject plaintext `token` args.

`CodingTeamWorkflow` today only executes `run_pipeline_activity` in a
pause/resume loop. `POST /run-from-github` still calls `_start_hook_thread` →
`_run_with_github_hooks` on a daemon thread.

## Decisions

| Topic | Choice |
|---|---|
| Orchestration style | Workflow-orchestrated activities (optional `github` block on the start payload) |
| Branch prep timing | Once, before the first pipeline attempt |
| Failure notice | On branch-prep `ok=False` and on pipeline activity exception (`kind="failure"`); not on normal terminal `failed`/`cancelled`/`waiting_for_user` snapshots |
| Route | `/run-from-github` starts `CodingTeamWorkflow` (no Temporal fallback to the hook thread) |
| Token in workflow args | Never — activities resolve activity-side |
| Hook thread deletion | Leave `_run_with_github_hooks` in tree; stop calling it from the route (deletion is sibling cleanup) |

## Architecture

### Start payload

`start_coding_team_workflow(job_id, repo_path, plan_input, github=None)` builds:

```python
{
  "job_id": job_id,
  "repo_path": repo_path,
  "plan_input": plan_input,
  # optional:
  "github": {
    "owner": str,
    "repo": str,
    "issue_number": int,
    "issue_title": str,
    "remote": str,
    "base": str,                      # request.base_branch or repo default_branch
    "integration_branch": str,        # f"khala/issue-{issue_number}"
    "cleanup_checkout_on_success": bool,
  },
}
```

No token field anywhere in the workflow payload.

### `CodingTeamWorkflow.run` (when `github` is present)

1. `execute_activity(github_branch_prep_activity, prep_request)`  
   - Required fields: `job_id`, `repo_path`, `remote`, `default_branch` (= `github["base"]`), `integration_branch`, optional `issue_number`.  
   - If `ok` is false → `github_failure_notice_activity` with `kind="failure"` and the prep error message → return a terminal result (job snapshot or notice return).  
2. Existing pipeline pause/resume loop (unchanged for non-GitHub runs).  
   - On uncaught pipeline activity exception → failure-notice → re-raise so the workflow fails observably.  
3. After a non-`paused` pipeline result:  
   - If job `status` is `failed` / `cancelled` / `waiting_for_user` → return that snapshot (no publish, no failure-notice).  
   - Else → `github_publish_activity` with publish fields from `github` + `job_id`/`repo_path`; return publish result.

When `github` is absent, behavior matches today (pipeline loop only).

### Route

`POST /run-from-github` keeps: token resolve, issue pick/verify, create_job,
`github_context` + `github_token_encrypted` persistence. Then:

- Resolve `base` via existing `GitHubClient.get_repo(...).default_branch` (or
  `request.base_branch`).
- Build `integration_branch = f"khala/issue-{issue.number}"`.
- Call `start_coding_team_workflow(..., github={...})`.
- Do **not** call `_start_hook_thread`.

## Error handling

- Activity `ValueError`s (missing fields, forbidden `token`) propagate as
  activity failures; workflow may fail the run. Prefer constructing well-formed
  requests from the `github` block so these are wiring bugs.
- Failure-notice messages must not include tokens or full request payloads
  (existing activity invariant).
- Pipeline exceptions: notify then re-raise.

## Testing

1. **Unit** (monkeypatched `workflow.execute_activity` in
   `test_coding_team_temporal_workflow.py`):  
   - GitHub happy path call order: branch-prep → pipeline → publish.  
   - Prep `ok=False` → failure-notice, no pipeline.  
   - Pipeline raises → failure-notice.  
   - Non-GitHub request: still a single pipeline call when not paused.
2. **Integration** (`WorkflowEnvironment`, fake activities registered under the
   real Temporal names): branch-prep → pipeline → publish end-to-end
   (acceptance criterion).
3. **Route**: `/run-from-github` invokes `start_coding_team_workflow` and does
   not call `_start_hook_thread` (monkeypatch).

## Scope

### In scope

- Optional `github` block on workflow start + `CodingTeamWorkflow` GitHub path
- `start_coding_team_workflow` signature extension
- `/run-from-github` Temporal dispatch
- Unit + integration + route tests above

### Out of scope

- Resume-trigger route / auto-resume signaling (sibling issues)
- Deleting `_run_with_github_hooks` / claim-heartbeat machinery
- Thread-mode parity extras deferred here: start/prep-note/pause/
  did-not-complete issue comments; checkout sibling busy-check; workflow-side
  “nothing merged” short-circuit before publish (`already_complete` remains
  inside `github_publish_activity`)
- New auth mechanisms

## Risks

- Deferred comments/sibling checks mean early Temporal GitHub runs are quieter
  and less guarded than thread mode until follow-ups land — acceptable for
  this slice given acceptance focuses on prep → pipeline → publish.
- `integration_branch` / `base` must stay consistent with what resume paths
  expect from `github_context`; persist the same values the route already
  stores where applicable.
