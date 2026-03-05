---
name: KIN-5 centralized job management layer
overview: Define and implement a shared, local-cache-backed job management subsystem used by agent teams for consistent job lifecycle tracking, action/outcome updates, completion/failure signaling, and stale-job health checks.
todos:
  - id: spec-1
    content: Define canonical job model and lifecycle states shared across teams
    status: completed
  - id: spec-2
    content: Define API for job creation, updates, events, listing, and stale health checks
    status: completed
  - id: impl-1
    content: Build shared CentralJobManager module with file-backed storage and thread-safety
    status: completed
  - id: impl-2
    content: Integrate software engineering team job store with centralized manager backend
    status: completed
  - id: impl-3
    content: Integrate social marketing API in-memory job tracking with centralized manager
    status: completed
  - id: impl-4
    content: Add stale monitor that marks stale pending/running jobs as failed unless waiting
    status: completed
  - id: test-1
    content: Add and update tests to validate centralized job management behavior
    status: in_progress
isProject: false
---

# Spec

## Problem
Job management was fragmented by team-specific formats and behavior. This caused inconsistent status models, uneven information capture, and no shared stale-job supervision policy.

## Goals
1. Centralize job lifecycle handling behind a common subsystem.
2. Keep storage local-cache-backed (file system) for restart survivability.
3. Standardize job status transitions and action/outcome tracking.
4. Allow teams to signal completion and failure consistently.
5. Periodically detect stale pending/running jobs and mark them failed unless explicitly waiting.

## Non-goals (v1)
- Cross-process distributed locks.
- External databases/queues.
- UI redesign.

## Canonical data model (v1)
Each job stores:
- `job_id`, `team`, `job_type`, `status`
- `created_at`, `updated_at`, `last_heartbeat_at`
- `events[]` (action/outcome timeline)
- team-specific extension fields (metadata, progress, current stage/task, result/error, etc.)

## Lifecycle states
- Active: `pending`, `running`
- Terminal: `completed`, `failed`, `cancelled`
- Team extensions are allowed but must retain compatibility with active/terminal semantics.

## Shared operations
- `create_job(...)`
- `update_job(...)` (with heartbeat timestamp updates)
- `append_event(...)` for action/outcome reporting
- `get_job(...)`, `list_jobs(...)`
- `mark_stale_active_jobs_failed(...)` with waiting-field exemption
- optional daemon monitor `start_stale_job_monitor(...)`

## Stale health policy
A periodic monitor checks active jobs and marks them failed when:
- status is `pending` or `running`
- `waiting_for_answers` is not true
- `now - last_heartbeat_at > stale_after_seconds`

Failure reason is recorded in `error`.

# Implementation Plan
1. Introduce shared module `agents/shared_job_management.py` implementing `CentralJobManager` and monitor helper.
2. Point software engineering `job_store` create/get/list/update to centralized manager while preserving existing API surface.
3. Add stale-mark helper in software engineering `job_store` and start one monitor in API startup path (`run_team`).
4. Migrate social marketing API from in-memory dict jobs to centralized manager storage.
5. Update tests that depended on in-memory internals; add tests for manager stale detection and event storage.

# Execution Notes
- Central manager stores jobs at `.agent_cache/<team>/jobs/<job_id>.json`.
- Existing per-team fields are preserved by forwarding arbitrary update/create fields.
- Social marketing revision flow now stores `request_payload` as JSON-friendly dict and reconstructs pydantic model on revise.
