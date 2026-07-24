# Migrate `jobs.py` / `artifacts.py` Status & Artifact Routes to Shared Job Guard

**Status:** Approved 2026-07-24  
**Date:** 2026-07-24  
**Type:** Structural refactor (behavior-preserving except normalized 501 detail)  
**Issue:** #2095  
**Branch / worktree:** `refactor/2095-migrate-jobs-artifacts-job-guard` / `.worktrees/issue-2095-migrate-jobs-artifacts-job-guard`

## Problem

The store-available (501) → job-found (404) guard is duplicated in
`backend/agents/blogging/api/routers/jobs.py` (`get_job_status`,
`stream_job_status`) and `backend/agents/blogging/api/routers/artifacts.py`
(`list_job_artifacts`, `get_job_artifact_content`). The shared FastAPI
dependency `get_job` in `agents.blogging.api.dependencies` already encodes
that sequence and is used by `interactive.py`. These four handlers should
delegate to it the same way.

## Goals

1. Replace the inline 501/404 blocks in the four handlers with
   `Depends(get_job(...))`, matching `interactive.py`.
2. Keep status codes and 404 detail strings unchanged.
3. Leave existing route tests unmodified; they must continue to pass.
4. Hold the 90% coverage floor on touched files.

## Non-goals

- Migrating cancel / delete / resume / restart / approve / unapprove /
  list_jobs (follow-up; outside the cited line ranges).
- Changing `dependencies.py` or its tests.
- `interactive.py` (tracked separately).
- Preserving per-route custom 501 detail strings (normalized by design).

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Approach | `Depends(get_job(...))` on each handler | Matches `interactive.py`; no new abstraction |
| Scope | Four handlers only (status, stream, list artifacts, get artifact) | Matches issue line citations; keeps complexity low |
| 501 detail | Always default `"Job store not available"` | Normalize API surface; route tests assert status only |
| Injected job | Use the `Depends`-injected `job` for post-guard logic | Avoids a redundant second lookup |
| Get-artifact helpers | `get_job("read_artifact")` | Preserves today's dual-helper 501 gate |

## Design

### Imports

Both routers add:

```python
from typing import Any, Dict  # jobs.py already has List; add as needed

from agents.blogging.api.dependencies import get_job
from fastapi import Depends  # alongside existing FastAPI imports
```

`HTTPException` remains where handlers still raise non-guard errors
(stream has none beyond the guard; artifacts keep work_dir / unknown /
missing-file 404s).

### `jobs.get_job_status`

```python
def get_job_status(
    job_id: str,
    job: Dict[str, Any] = Depends(get_job()),
) -> BlogJobStatusResponse:
    return _blog_job_dict_to_status_response(job, job_id)
```

Drop the late `from agents.blogging.api import main` and the inline
501/404 block. No extra helper names; default store detail.

### `jobs.stream_job_status`

```python
def stream_job_status(
    job_id: str,
    job: Dict[str, Any] = Depends(get_job()),
) -> StreamingResponse:
    from agents.blogging.api import main as _main
    # ... subscribe / sse imports unchanged ...

    def _snapshot_event() -> dict:
        current = _main.get_blog_job(job_id) or {}
        ...
    if job.get("status") in _TERMINAL_STATUSES:
        ...
```

The injected `job` replaces the pre-stream lookup used for the terminal
short-circuit. Snapshot re-fetch via `_main.get_blog_job` stays as today.

### `artifacts.list_job_artifacts`

```python
def list_job_artifacts(
    job_id: str,
    job: Dict[str, Any] = Depends(get_job()),
) -> ArtifactListResponse:
    from agents.blogging.api import main as _main
    work_dir = job.get("work_dir")
    ...
```

Post-guard artifact-directory logic unchanged.

### `artifacts.get_job_artifact_content`

```python
def get_job_artifact_content(
    job_id: str,
    artifact_name: str,
    download: bool = Query(...),
    job: Dict[str, Any] = Depends(get_job("read_artifact")),
) -> ArtifactContentResponse | Response:
    from agents.blogging.api import main as _main
    work_dir = job.get("work_dir")
    ...
```

`read_artifact` is passed as an extra helper name so a missing reader
still yields 501. Detail string becomes the shared default (was
`"Job store or artifact reader not available"`).

### Behavior contract

| Case | Status | Detail |
|---|---|---|
| Required helper(s) missing | 501 | `"Job store not available"` |
| Job missing | 404 | `f"Job {job_id} not found"` |
| No `work_dir` / unknown artifact / missing file | 404 | Existing in-handler messages |

## Testing

- Existing `jobs.py` / `artifacts.py` route tests (including
  `test_api_temporal_and_501s.py` status/stream/artifact 501 cases) must
  pass unmodified — they assert status codes, not detail strings.
- No new unit tests required unless coverage on a touched file falls
  below 90%; if so, add a narrow route-level assertion rather than
  changing dependency tests.
- Verify with the blogging API test selection covering status, stream,
  artifacts, and 501 paths; run `ruff` on touched files.

## Success criteria

1. All four handlers obtain the job via `Depends(get_job(...))`; inline
   store/job-found guards are gone.
2. Status codes unchanged; 501 detail normalized to the shared default.
3. Existing route tests pass unmodified.
4. Coverage floor of 90% holds for touched files.

## Risk

Low. Mechanical Depends swap with an established pattern. Residual risk
is forgetting `read_artifact` on the content route (would change when
501 fires) — caught by `test_get_artifact_501`.
