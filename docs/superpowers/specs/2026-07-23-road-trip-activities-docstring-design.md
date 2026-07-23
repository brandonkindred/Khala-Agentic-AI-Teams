# Design: Road Trip Temporal Activities Module Docstring Polish

**Branch / worktree:** `docs/2141-road-trip-activities-docstring`  
**Scope decision:** Approach B — rephrase the module invariant to name the team `job_store` facade (approved).

## Goal

Clarify that road-trip Temporal activities persist job status through
`road_trip_planning_team.shared.job_store` under the `road_trip_planning_team`
slug, matching branding’s activities-docstring style and the code’s actual call
sites (`update_job` / `get_job`), so maintainers are not misled into thinking
activities talk to `JobServiceClient` directly.

## Non-Goals

- No behavioral changes to activities, `job_store`, or callers.
- No new tests (docstring-only change).
- No edits outside `backend/agents/road_trip_planning_team/temporal/activities.py`.

## File Touch

| File | Change |
|------|--------|
| `backend/agents/road_trip_planning_team/temporal/activities.py` | Replace the module docstring’s Invariant paragraph only |

## Docstring Change

**Before:**

```text
Invariant: job-store status is written to the durable ``JobServiceClient`` store
under the ``road_trip_planning_team`` slug (the same slug the API's ``create_job``
used), so a completed run survives a worker/process restart.
```

**After:**

```text
Invariant: job-store status is written via
``road_trip_planning_team.shared.job_store`` under the
``road_trip_planning_team`` slug — the same slug the API's
``create_job`` used — so a completed run survives a worker/process restart.
```

All other module-docstring content (activity list, sync/import hygiene, JSON
payload note) stays unchanged.

## Verification

```bash
cd backend && LLM_PROVIDER=dummy make lint
```

Docstring-only; full test suite is optional. Confirm the invariant text matches
`job_store._client()` (`JobServiceClient(team="road_trip_planning_team")`) and
API `create_job` usage.

## Acceptance Mapping

| Criterion | How met |
|-----------|---------|
| Name the facade activities actually call | Invariant cites `road_trip_planning_team.shared.job_store` |
| Keep slug / restart-survival meaning | Same slug and survival clause retained |
| Style parity with branding activities docstring | Facade-first wording, em-dash slug clause |
| Lint passes under dummy LLM | Verification command |
