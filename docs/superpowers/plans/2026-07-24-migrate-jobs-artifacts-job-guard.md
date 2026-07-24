# Migrate jobs/artifacts Routes to Shared Job Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace inline store-available (501) / job-found (404) guards in four blogging API handlers with `Depends(get_job(...))`, matching `interactive.py`.

**Architecture:** `agents.blogging.api.dependencies.get_job` already implements the guard sequence. Each in-scope handler gains a `job: Dict[str, Any] = Depends(get_job(...))` parameter, drops its inline 501/404 block, and uses the injected `job` for post-guard logic. 501 detail strings normalize to the shared default `"Job store not available"`.

**Tech Stack:** Python 3.10+, FastAPI `Depends`, existing blogging API TestClient fixtures, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-07-24-migrate-jobs-artifacts-job-guard-design.md`

**Worktree:** `.worktrees/issue-2095-migrate-jobs-artifacts-job-guard` on branch `refactor/2095-migrate-jobs-artifacts-job-guard`

## Global Constraints

- Scope is exactly four handlers: `get_job_status`, `stream_job_status`, `list_job_artifacts`, `get_job_artifact_content`.
- Do not modify `dependencies.py`, `interactive.py`, or cancel/delete/resume/restart/approve/unapprove/list_jobs.
- Existing route tests must pass unmodified (do not edit test files).
- 501 detail for these routes becomes `"Job store not available"` (normalized; tests assert status code only).
- Coverage floor of 90% on touched files; Design by Contract on any new/changed public functions (handler signatures already documented by FastAPI; keep existing docstrings).
- Never reference GitHub issues in code, comments, or commit messages; PR body may use `Closes #2095`.

## File Structure

| File | Role |
|---|---|
| `backend/agents/blogging/api/dependencies.py` | Unchanged — shared `get_job` / helpers |
| `backend/agents/blogging/api/routers/jobs.py` | Modify `get_job_status` + `stream_job_status` only |
| `backend/agents/blogging/api/routers/artifacts.py` | Modify both artifact handlers |
| `backend/agents/blogging/tests/test_api_temporal_and_501s.py` | Unchanged — verification only |
| Other blogging route tests | Unchanged — verification only |

Reference pattern (already on main) in `backend/agents/blogging/api/routers/interactive.py`:

```python
from agents.blogging.api.dependencies import get_job
from fastapi import Depends

def select_title(
    job_id: str,
    request: SelectTitleRequest,
    _job: Dict[str, Any] = Depends(get_job("submit_title_selection", waiting_for=(...))),
) -> BlogJobStatusResponse:
    ...
```

For read handlers in this plan, name the parameter `job` (not `_job`) and use it.

**Test runner** (from worktree `backend/`, reuse main-repo venv if worktree has none):

```bash
VENV=/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python
export PYTHONPATH="$(pwd)"
"$VENV" -m pytest <args>
```

---

### Task 1: Migrate `get_job_status`

**Files:**
- Modify: `backend/agents/blogging/api/routers/jobs.py` (imports + `get_job_status`)
- Test: `backend/agents/blogging/tests/test_api_temporal_and_501s.py::test_get_job_status_501` (run only; do not edit)

**Interfaces:**
- Consumes: `get_job(*helper_names, store_detail=..., waiting_for=...) -> Callable[[str], Dict[str, Any]]` from `agents.blogging.api.dependencies`
- Produces: `get_job_status(job_id: str, job: Dict[str, Any] = Depends(get_job())) -> BlogJobStatusResponse`

- [ ] **Step 1: Confirm baseline**

Run:

```bash
cd backend
VENV=/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python
PYTHONPATH="$(pwd)" "$VENV" -m pytest \
  agents/blogging/tests/test_api_temporal_and_501s.py::test_get_job_status_501 \
  agents/blogging/tests/test_blogging_api.py \
  agents/blogging/tests/test_api_unit.py \
  -q --tb=short -k "job_status or get_job"
```

Expected: PASS (baseline green before edit).

- [ ] **Step 2: Update imports in `jobs.py`**

Change the top of `backend/agents/blogging/api/routers/jobs.py` from:

```python
from typing import List

from agents.blogging.api.models import (
    BlogJobListItem,
    BlogJobStatusResponse,
    CancelJobResponse,
    DeleteJobResponse,
    FullPipelineRequest,
    StartPipelineResponse,
    _blog_job_dict_to_status_response,
    _format_audience,
)
from fastapi import APIRouter, HTTPException
```

to:

```python
from typing import Any, Dict, List

from agents.blogging.api.dependencies import get_job
from agents.blogging.api.models import (
    BlogJobListItem,
    BlogJobStatusResponse,
    CancelJobResponse,
    DeleteJobResponse,
    FullPipelineRequest,
    StartPipelineResponse,
    _blog_job_dict_to_status_response,
    _format_audience,
)
from fastapi import APIRouter, Depends, HTTPException
```

- [ ] **Step 3: Replace `get_job_status` body**

Replace the entire `get_job_status` function with:

```python
@router.get(
    "/job/{job_id}",
    response_model=BlogJobStatusResponse,
    summary="Get job status",
    description="Poll the status of a running or completed pipeline job.",
)
def get_job_status(
    job_id: str,
    job: Dict[str, Any] = Depends(get_job()),
) -> BlogJobStatusResponse:
    """Get the current status of a pipeline job."""
    return _blog_job_dict_to_status_response(job, job_id)
```

Preconditions for callers: FastAPI injects `job_id` from the path; `get_job()` raises 501/404 before the body runs. Postconditions: returns the status response for the injected job dict.

- [ ] **Step 4: Re-run targeted tests**

Run:

```bash
PYTHONPATH="$(pwd)" "$VENV" -m pytest \
  agents/blogging/tests/test_api_temporal_and_501s.py::test_get_job_status_501 \
  -q --tb=short
```

Expected: PASS (`status_code == 501` when `get_blog_job` is `None`).

Also smoke a happy-path status fetch if covered in `test_blogging_api.py` / `test_api_unit.py`:

```bash
PYTHONPATH="$(pwd)" "$VENV" -m pytest \
  agents/blogging/tests/test_blogging_api.py \
  agents/blogging/tests/test_api_unit.py \
  -q --tb=short -k "status"
```

Expected: PASS (no new failures attributable to this change).

- [ ] **Step 5: Commit**

```bash
git add backend/agents/blogging/api/routers/jobs.py
git commit -m "$(cat <<'EOF'
Migrate get_job_status onto shared get_job dependency.

EOF
)"
```

---

### Task 2: Migrate `stream_job_status`

**Files:**
- Modify: `backend/agents/blogging/api/routers/jobs.py` (`stream_job_status` only; imports already done in Task 1)
- Test: `backend/agents/blogging/tests/test_api_temporal_and_501s.py::test_stream_501` (run only)

**Interfaces:**
- Consumes: same `get_job` as Task 1
- Produces: `stream_job_status(job_id: str, job: Dict[str, Any] = Depends(get_job())) -> StreamingResponse`

- [ ] **Step 1: Confirm baseline for stream 501**

```bash
PYTHONPATH="$(pwd)" "$VENV" -m pytest \
  agents/blogging/tests/test_api_temporal_and_501s.py::test_stream_501 \
  -q --tb=short
```

Expected: PASS.

- [ ] **Step 2: Replace `stream_job_status` signature and drop inline guards**

Replace the function from its decorator through the end of the inline 501/404 block so it becomes:

```python
@router.get(
    "/job/{job_id}/stream",
    summary="Stream job status via SSE",
    description=(
        "Server-Sent Events stream for real-time job updates. "
        "Emits an initial 'snapshot' event with full status, then incremental 'update' events, "
        "and a terminal event ('complete', 'error', or 'cancelled') before closing."
    ),
)
def stream_job_status(
    job_id: str,
    job: Dict[str, Any] = Depends(get_job()),
) -> StreamingResponse:
    """SSE stream for a pipeline job. Falls back gracefully if job is already terminal."""
    from agents.blogging.api import main as _main
    from agents.blogging.shared.job_event_bus import subscribe, unsubscribe

    from shared.sse import sse_job_stream_sync, sse_line

    def _snapshot_event() -> dict:
        current = _main.get_blog_job(job_id) or {}
        resp = _blog_job_dict_to_status_response(current, job_id)
        return {"type": "snapshot", **resp.model_dump(mode="json")}

    # If the job is already terminal, send a snapshot + done and close immediately.
    if job.get("status") in _TERMINAL_STATUSES:

        def _terminal_gen():
            yield sse_line(_snapshot_event())
            yield sse_line({"type": "done"})

        return StreamingResponse(_terminal_gen(), media_type="text/event-stream")

    return StreamingResponse(
        sse_job_stream_sync(
            subscribe=subscribe,
            unsubscribe=unsubscribe,
            job_id=job_id,
            snapshot=_snapshot_event,
            terminal_types=("complete", "error", "cancelled"),
        ),
        media_type="text/event-stream",
    )
```

Keep snapshot re-fetch via `_main.get_blog_job` unchanged. Use injected `job` only for the terminal-status check.

- [ ] **Step 3: Re-run stream tests**

```bash
PYTHONPATH="$(pwd)" "$VENV" -m pytest \
  agents/blogging/tests/test_api_temporal_and_501s.py::test_stream_501 \
  agents/blogging/tests/test_api_unit.py \
  agents/blogging/tests/test_blogging_api.py \
  -q --tb=short -k "stream"
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add backend/agents/blogging/api/routers/jobs.py
git commit -m "$(cat <<'EOF'
Migrate stream_job_status onto shared get_job dependency.

EOF
)"
```

---

### Task 3: Migrate `list_job_artifacts`

**Files:**
- Modify: `backend/agents/blogging/api/routers/artifacts.py` (imports + `list_job_artifacts`)
- Test: `backend/agents/blogging/tests/test_api_temporal_and_501s.py::test_list_artifacts_501` (run only)

**Interfaces:**
- Consumes: `get_job()`
- Produces: `list_job_artifacts(job_id: str, job: Dict[str, Any] = Depends(get_job())) -> ArtifactListResponse`

- [ ] **Step 1: Confirm baseline**

```bash
PYTHONPATH="$(pwd)" "$VENV" -m pytest \
  agents/blogging/tests/test_api_temporal_and_501s.py::test_list_artifacts_501 \
  -q --tb=short
```

Expected: PASS.

- [ ] **Step 2: Update imports in `artifacts.py`**

Change:

```python
from __future__ import annotations

import json as json_module
from pathlib import Path

from agents.blogging.api.models import (
    ArtifactContentResponse,
    ArtifactListResponse,
    ArtifactMeta,
)
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
```

to:

```python
from __future__ import annotations

import json as json_module
from pathlib import Path
from typing import Any, Dict

from agents.blogging.api.dependencies import get_job
from agents.blogging.api.models import (
    ArtifactContentResponse,
    ArtifactListResponse,
    ArtifactMeta,
)
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
```

- [ ] **Step 3: Replace `list_job_artifacts`**

```python
@router.get(
    "/job/{job_id}/artifacts",
    response_model=ArtifactListResponse,
    summary="List job artifacts",
    description="List artifact filenames that exist for a pipeline job. Returns 404 if the job is missing or has no work_dir.",
)
def list_job_artifacts(
    job_id: str,
    job: Dict[str, Any] = Depends(get_job()),
) -> ArtifactListResponse:
    """List existing artifact names for a job."""
    from agents.blogging.api import main as _main

    work_dir = job.get("work_dir")
    if not work_dir:
        raise HTTPException(status_code=404, detail="Job has no artifact directory")
    work_path = Path(work_dir)
    existing_names = [name for name in _main.ARTIFACT_NAMES if (work_path / name).exists()]
    meta_list = []
    for name in existing_names:
        producer = _main.ARTIFACT_PRODUCER.get(name, {}) if _main.ARTIFACT_PRODUCER else {}
        meta_list.append(
            ArtifactMeta(
                name=name,
                producer_phase=producer.get("producer_phase"),
                producer_agent=producer.get("producer_agent"),
            )
        )
    return ArtifactListResponse(artifacts=meta_list)
```

Keep the `work_dir` / listing logic identical; only the store/job-found guard moves to `Depends`.

- [ ] **Step 4: Re-run list-artifact tests**

```bash
PYTHONPATH="$(pwd)" "$VENV" -m pytest \
  agents/blogging/tests/test_api_temporal_and_501s.py::test_list_artifacts_501 \
  agents/blogging/tests/test_api_unit.py \
  agents/blogging/tests/test_blogging_api.py \
  agents/blogging/tests/test_api_extra.py \
  -q --tb=short -k "artifact"
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/agents/blogging/api/routers/artifacts.py
git commit -m "$(cat <<'EOF'
Migrate list_job_artifacts onto shared get_job dependency.

EOF
)"
```

---

### Task 4: Migrate `get_job_artifact_content`

**Files:**
- Modify: `backend/agents/blogging/api/routers/artifacts.py` (`get_job_artifact_content` only)
- Test: `backend/agents/blogging/tests/test_api_temporal_and_501s.py::test_get_artifact_501` (run only)

**Interfaces:**
- Consumes: `get_job("read_artifact")` — checks `get_blog_job` and `read_artifact`
- Produces: handler with `job: Dict[str, Any] = Depends(get_job("read_artifact"))`

- [ ] **Step 1: Confirm baseline**

```bash
PYTHONPATH="$(pwd)" "$VENV" -m pytest \
  agents/blogging/tests/test_api_temporal_and_501s.py::test_get_artifact_501 \
  -q --tb=short
```

Expected: PASS. Note: this test sets only `read_artifact` to `None`; `get_job("read_artifact")` must still 501 (it checks all named helpers plus `get_blog_job`).

- [ ] **Step 2: Replace `get_job_artifact_content`**

```python
@router.get(
    "/job/{job_id}/artifacts/{artifact_name}",
    summary="Get job artifact content or download",
    description="Return the content of a single artifact (JSON body), or with ?download=true return as attachment. Path traversal is blocked; artifact_name must be in the allowed list.",
    response_model=None,
)
def get_job_artifact_content(
    job_id: str,
    artifact_name: str,
    download: bool = Query(
        False, description="If true, return content as attachment with Content-Disposition"
    ),
    job: Dict[str, Any] = Depends(get_job("read_artifact")),
) -> ArtifactContentResponse | Response:
    """Return content of one artifact for a job, or as download attachment."""
    from agents.blogging.api import main as _main

    work_dir = job.get("work_dir")
    if not work_dir:
        raise HTTPException(status_code=404, detail="Job has no artifact directory")
    if artifact_name not in _main.ARTIFACT_NAMES:
        raise HTTPException(status_code=404, detail=f"Unknown artifact: {artifact_name!r}")
    parse_json = artifact_name.endswith(".json")
    content = _main.read_artifact(work_dir, artifact_name, default=None, parse_json=parse_json)
    if content is None:
        raise HTTPException(status_code=404, detail=f"Artifact {artifact_name!r} not found")

    if download:
        if isinstance(content, (dict, list)):
            raw = json_module.dumps(content, indent=2)
            media_type = "application/json"
        else:
            raw = content if isinstance(content, str) else str(content)
            if artifact_name.endswith(".json"):
                media_type = "application/json"
            elif artifact_name.endswith(".yaml") or artifact_name.endswith(".yml"):
                media_type = "text/yaml"
            else:
                media_type = "text/plain; charset=utf-8"
        return Response(
            content=raw.encode("utf-8"),
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{artifact_name}"'},
        )
    return ArtifactContentResponse(name=artifact_name, content=content)
```

Do **not** pass `store_detail=...`; accept the default `"Job store not available"` (replaces the old `"Job store or artifact reader not available"` string).

- [ ] **Step 3: Re-run artifact content tests**

```bash
PYTHONPATH="$(pwd)" "$VENV" -m pytest \
  agents/blogging/tests/test_api_temporal_and_501s.py::test_get_artifact_501 \
  agents/blogging/tests/test_api_unit.py \
  agents/blogging/tests/test_blogging_api.py \
  agents/blogging/tests/test_api_extra.py \
  -q --tb=short -k "artifact"
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add backend/agents/blogging/api/routers/artifacts.py
git commit -m "$(cat <<'EOF'
Migrate get_job_artifact_content onto shared get_job dependency.

EOF
)"
```

---

### Task 5: Final verification

**Files:**
- Verify only (no further edits unless lint/coverage fails)

**Interfaces:**
- Consumes: all four migrated handlers from Tasks 1–4
- Produces: green lint + targeted blogging suite; coverage ≥ 90% on touched files if measured

- [ ] **Step 1: Confirm no remaining inline guards on in-scope handlers**

```bash
rg -n "get_blog_job is None|Job status not available|Job store or artifact reader" \
  backend/agents/blogging/api/routers/jobs.py \
  backend/agents/blogging/api/routers/artifacts.py
```

Expected: no matches inside `get_job_status`, `stream_job_status`, `list_job_artifacts`, or `get_job_artifact_content`. Matches elsewhere in `jobs.py` (cancel/delete/etc.) are expected and must remain.

Also confirm Depends usage:

```bash
rg -n "Depends\(get_job" \
  backend/agents/blogging/api/routers/jobs.py \
  backend/agents/blogging/api/routers/artifacts.py
```

Expected: four call sites — two in `jobs.py` (`get_job()`), one `get_job()` and one `get_job("read_artifact")` in `artifacts.py`.

- [ ] **Step 2: Run full relevant blogging API slice**

```bash
cd backend
PYTHONPATH="$(pwd)" "$VENV" -m pytest \
  agents/blogging/tests/test_api_dependencies.py \
  agents/blogging/tests/test_api_temporal_and_501s.py \
  agents/blogging/tests/test_api_unit.py \
  agents/blogging/tests/test_api_extra.py \
  agents/blogging/tests/test_blogging_api.py \
  -q --tb=short
```

Expected: all PASS.

- [ ] **Step 3: Lint touched files**

```bash
cd backend
"$VENV" -m ruff check agents/blogging/api/routers/jobs.py agents/blogging/api/routers/artifacts.py
"$VENV" -m ruff format --check agents/blogging/api/routers/jobs.py agents/blogging/api/routers/artifacts.py
```

Expected: clean. If format fails, run `ruff format` on those two files and amend only if the commit that introduced the format issue is HEAD and unpushed; otherwise make a new commit:

```bash
git add backend/agents/blogging/api/routers/jobs.py backend/agents/blogging/api/routers/artifacts.py
git commit -m "$(cat <<'EOF'
Format jobs and artifacts routers after job-guard migration.

EOF
)"
```

- [ ] **Step 4: Coverage check on touched files (if below floor, add assertion — only then)**

```bash
PYTHONPATH="$(pwd)" "$VENV" -m pytest \
  agents/blogging/tests/test_api_temporal_and_501s.py \
  agents/blogging/tests/test_api_unit.py \
  agents/blogging/tests/test_api_extra.py \
  agents/blogging/tests/test_blogging_api.py \
  --cov=agents.blogging.api.routers.jobs \
  --cov=agents.blogging.api.routers.artifacts \
  --cov-report=term-missing \
  -q --tb=line
```

Expected: line coverage ≥ 90% for both modules. If a module dips below 90%, do **not** edit existing tests to force green via weaker asserts; add a new narrow test only if a real uncovered branch was introduced by this migration (unlikely for a Depends swap). Prefer documenting with `# pragma: no cover` only for race-only guards already present elsewhere — not for new code.

- [ ] **Step 5: Final commit only if Step 3/4 produced fixes; otherwise stop**

No empty commit. If everything is already committed and green, this task ends with verification only.

---

## Spec coverage (self-review)

| Spec requirement | Task |
|---|---|
| Migrate `get_job_status` via `Depends(get_job())` | Task 1 |
| Migrate `stream_job_status`; keep snapshot re-fetch | Task 2 |
| Migrate `list_job_artifacts` via `Depends(get_job())` | Task 3 |
| Migrate `get_job_artifact_content` via `get_job("read_artifact")` | Task 4 |
| Normalize 501 detail to default | Tasks 1–4 (no `store_detail=`) |
| Leave other `jobs.py` handlers / `dependencies.py` / tests untouched | Global Constraints + Task 5 rg check |
| Existing route tests pass unmodified | Steps run tests without editing them |
| 90% coverage floor | Task 5 Step 4 |

Placeholder scan: none. Type consistency: `job: Dict[str, Any] = Depends(get_job(...))` throughout; artifact content uses `get_job("read_artifact")`.
