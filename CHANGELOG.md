# Changelog

All notable changes to this repository are documented here.

## [Unreleased]

### Added

- **Central job microservice** (`backend/agents/job_service/`): Postgres-backed job records for all teams that used `CentralJobManager`. Set `JOB_SERVICE_URL` (and optional `JOB_SERVICE_API_KEY`) so agents use HTTP; leave unset to keep file storage under `AGENT_CACHE`. Docker Compose adds a `job-service` container and `strands_jobs` database (see `docker/postgres/STRANDS_JOBS_MIGRATION.md` for existing volumes). Heartbeats: default stale threshold **300s** (`JOB_HEARTBEAT_STALE_SECONDS`); agents use `maybe_start_job_heartbeat` when remote. Local: `make run-job-service` from `backend/` (requires `DATABASE_URL`).

### Breaking changes

- **Blogging pipeline:** `BlogReviewAgent` has been removed. The pipeline is **research → planning → draft (author)** with a persisted `ContentPlan` (`content_plan.json` / `content_plan.md`). `POST /research-and-review` (sync and async) runs the same **research + planning** step as the full pipeline, not a separate “review” agent. `POST /full-pipeline` returns `title_choices` and `outline` derived from the approved plan; planning failure returns **422** with `planning_failed` detail. Async jobs expose a **planning** phase and optional planning observability fields on completed jobs; failed planning jobs may include `planning_failure_reason`. Optional env: `BLOG_PLANNING_MODEL`, `BLOG_PLANNING_MAX_ITERATIONS`, `BLOG_PLANNING_MAX_PARSE_RETRIES`.
