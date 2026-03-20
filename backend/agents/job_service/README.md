# Job service (`job_service`)

FastAPI microservice that stores **all agent team job records** in Postgres (`strands_jobs` database by default). Agent processes talk to it over HTTP when `JOB_SERVICE_URL` is set; otherwise they use the file-backed `CentralJobManager` under `AGENT_CACHE`.

## Heartbeats and stale jobs

- Agents should send periodic heartbeats (see `start_periodic_job_heartbeat` / `maybe_start_job_heartbeat` in `shared_job_management.py` and `POST /v1/jobs/{team}/{job_id}/heartbeat`).
- The service marks `pending` / `running` jobs as `failed` when `last_heartbeat_at` is older than `JOB_HEARTBEAT_STALE_SECONDS` (default **300**), except when `waiting_for_answers` is truthy in the payload.

## Environment

| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` or `JOB_SERVICE_DATABASE_URL` | Postgres connection string |
| `JOB_HEARTBEAT_STALE_SECONDS` | Stale threshold (default 300) |
| `JOB_STALE_CHECK_INTERVAL_SECONDS` | Background sweep interval (default 30) |
| `JOB_SERVICE_API_KEY` | If set, require `X-Job-Service-Key` on mutating routes |

## Local run

From `backend/` with venv active:

```bash
export DATABASE_URL=postgresql://strands_jobs:strands_jobs@localhost:5432/strands_jobs
PYTHONPATH=agents python run_job_service.py --reload
```

Or `make run-job-service` from `backend/`.
