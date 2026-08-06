# Design: Resolve GitHub token activity-side (HITL redesign)

Date: 2026-08-06

## Goal

Ensure no decrypted GitHub token appears in Temporal workflow history for the
coding-team GitHub-hook activities. Tokens are resolved inside each activity
from the encrypted job-record value or `GITHUB_TOKEN`, never passed as a
plain-text activity/workflow argument.

## Context

`coding_team_github_activities.py` already wraps branch prep, publish, and
failure-notice as Temporal activities. Each still accepts a plain-text
`token` field in the activity request dict. Docstrings explicitly defer
activity-side resolution to this work.

Thread mode already persists `github_token_encrypted` on the job at
`POST /run-from-github` and resolves via `_resolve_github_job_token` on
resume (decrypt, else `GITHUB_TOKEN`). That helper soft-returns `None` and
classifies plain vs GitHub jobs for resume — activities need a fail-closed
variant of the same decrypt/env preference, not the resume soft-fail API.

Workflow wiring that *calls* these activities (`CodingTeamWorkflow`) is a
separate follow-up; this change only hardens the activity contract so that
wiring cannot pass a raw token even by mistake.

## Decisions

| Topic | Choice |
|---|---|
| Resolution location | Shared helper in `coding_team_github_activities.py` |
| Plaintext `token` in request | Reject with `ValueError` (fail closed) |
| Missing / unresolvable token | Reject with `ValueError` for all three activities |
| Preference order | `decrypt_token(github_token_encrypted)` then `GITHUB_TOKEN` env |
| Reuse `_resolve_github_job_token` | No — keep resume soft-fail separate; mirror decrypt/env only |
| Sandbox safety | Lazy-import job store / crypto inside the helper body |
| Error messages | Name field names / reasons only; never echo payload, ciphertext, or secrets |

## Architecture

### Helper: `_require_activity_github_token(request) -> str`

Preconditions:

- `request` is a dict (the activity request payload).

Postconditions:

1. If `"token" in request` → raise `ValueError` stating the field is forbidden
   (do not include the value).
2. If `job_id` is missing/falsy → raise `ValueError` naming `job_id`.
3. Load the job via the coding-team job store; if missing → raise `ValueError`
   naming `job_id`.
4. Resolve
   `decrypt_token(job.get("github_token_encrypted")) or os.environ.get("GITHUB_TOKEN")`.
5. If empty → raise `ValueError` that no usable GitHub token is available
   (no secret material in the message).
6. Return the plaintext token for in-activity use only (never returned in the
   activity result dict).

### Activity contract changes

| Activity | Required fields change |
|---|---|
| `github_branch_prep_activity` | Add `job_id`; remove any acceptance of `token` |
| `github_publish_activity` | Drop `token` from `_PUBLISH_REQUIRED_FIELDS` |
| `github_failure_notice_activity` | Drop `token` from `_FAILURE_NOTICE_REQUIRED_FIELDS` |

Each activity calls `_require_activity_github_token(request)` once after
structural required-field checks (or as part of them for `job_id`), then
passes the returned string into existing `_prepare_issue_branch` /
`GitHubClient` / `_publish_merged_work` / `_record_*` call sites unchanged.

Update module and activity docstrings: remove “token is plain-text for now /
activity-side resolution deferred” language; document activity-side
resolution and the forbidden `token` field.

## Error handling

- All validation failures are `ValueError` before any GitHub/git side effect
  when feasible (token reject and missing `job_id` before store I/O; missing
  job / missing token after load).
- Exception messages must not include `repr(request)`, ciphertext, or
  plaintext tokens (same invariant already used when naming missing fields).

## Testing

Update the three existing activity test modules:

1. Happy path: seed job with `github_token_encrypted` (and/or `GITHUB_TOKEN`);
   request includes `job_id` and omits `token`; assert the fake client / auth
   env receives the resolved secret.
2. Reject plaintext `token`: request that includes `"token"` → `ValueError`;
   message must not contain the secret.
3. Unresolvable token: no encrypted field, env cleared → `ValueError`.
4. Missing `job_id`: still covered by required-field validation (branch_prep
   newly requires it).
5. Preference order: encrypted job value wins over env when both are set.
6. Acceptance: required-field tuples / lists no longer include `"token"`.

Optional thin unit tests for the helper alone are fine if it keeps activity
tests smaller; not required if the three modules already cover the matrix.

## Scope

### In scope

- Helper + activity signature/docstring updates in
  `temporal/coding_team_github_activities.py`
- Test updates for the three GitHub-hook activity test files

### Out of scope

- Wiring activities into `CodingTeamWorkflow` (separate follow-up)
- Deleting thread-mode claim/heartbeat / `_run_with_github_hooks`
- New auth mechanisms (GitHub App, etc.)
- Changing `_resolve_github_job_token` resume behavior

## Risks

- Branch prep previously allowed optional no-auth; fail-closed means Temporal
  GitHub-hook runs always need a resolvable token. Acceptable: those runs are
  always GitHub-issue jobs created with encrypted token or env fallback.
- Callers of the activities in tests must be updated to seed jobs instead of
  passing `token=`; no production workflow callers exist yet.
