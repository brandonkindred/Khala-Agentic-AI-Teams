# Road Trip Planning Team

Multi-agent road trip planner: traveler profiling, route optimization, activities, logistics, and itinerary composition. Unified API prefix: `/api/road-trip-planning`.

## Pipeline

`POST /plan` runs five specialist agents in sequence and returns a `job_id`; poll `GET /jobs/{job_id}` for the `TripItinerary` result. The neutral per-step functions live in `pipeline.py` and are shared by both runtime modes:

```
profile_travelers → plan_route → recommend_activities → plan_logistics → compose_itinerary
```

- **Thread mode** (default, `TEMPORAL_ADDRESS` unset): `run_pipeline` chains the five steps in a daemon thread.
- **Temporal mode** (`TEMPORAL_ADDRESS` set): the durable `RoadTripWorkflow` (`temporal/workflows.py`) drives each step as its own `@activity.defn` (`temporal/activities.py`) — `begin` (RUNNING) → the five specialist steps → `persist` (COMPLETED), with `mark_failed` on error. Because each specialist agent is a distinct activity, a worker restart re-runs only the unfinished step, and each step carries its own timeout, retry policy, and Temporal span. Progress is exposed via the workflow's `progress` query. Task queue: `road_trip_planning-queue`.

Job-store status bookkeeping (RUNNING → COMPLETED/FAILED) is owned by the begin/persist/mark-failed activities in Temporal mode, and by `run_plan_core`/`run_plan_background` in thread mode.

## Khala platform

This package is part of the [Khala](../../../README.md) monorepo (Unified API, Angular UI, and full team index).
