# Job Matching Team

Scans open roles on the web against a **job-seeker profile** and returns a
ranked, explainable "best to apply" list. Mounted by the unified API at
`/api/job-matching`.

## Pipeline

```
profile (+ per-request overrides)
   └─> QueryBuilderAgent   → targeted web search queries
        └─> JobScannerAgent → Ollama web_search + page fetch + LLM extraction
             → normalized, de-duplicated JobPostings
              └─> JobRankerAgent → weighted rubric score + apply/maybe/skip
                   → JobMatchResponse (sorted best-first), persisted to Postgres
```

- **QueryBuilderAgent** turns the profile into a small set of high-signal
  search queries (LLM, with a deterministic template fallback so the pipeline
  runs offline in tests).
- **JobScannerAgent** runs each query through Ollama's `web_search` API, fetches
  the listing pages, and LLM-extracts structured postings. Postings are
  de-duplicated by a stable `(company, title, location)` fingerprint and can be
  filtered against fingerprints already ranked in prior runs (`exclude_seen`).
- **JobRankerAgent** scores each posting across six weighted dimensions
  (`title_fit`, `seniority_fit`, `location_fit`, `comp_fit`, `company_fit`,
  `skills_fit`) using the profile's `weights`, then applies deterministic hard
  exclusions (excluded companies, salary floor, deal-breakers → forced `skip`).

## Profile

The standing criteria are the **career section of the central user profile**
(`user_profiles.profile_json["career"]`, managed by the `user_profile` module
and edited via `PUT /profile`). Saving records a `career` artifact association
so the profile surfaces on the User Profile page. Resolution order:

1. `$JOB_SEEKER_PROFILE_PATH` — explicit operator pin, always honored first
2. Career section of the user profile (Postgres; skipped when unavailable or
   malformed — corruption is logged at ERROR and repaired by re-saving)
3. `$AGENT_CACHE/job_seeker_profile.yaml`
4. the bundled `profile/job_seeker_profile.example.yaml` (with a WARN log)

Set `JOB_SEEKER_PROFILE_STRICT=true` to raise when the pinned env path is
missing (and to disable the example fallback). Each scan request may override
any profile field via `profile_overrides`.

## HTTP API

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness |
| GET | `/profile` | Resolved job-seeker profile (career section of the user profile wins) |
| PUT | `/profile` | Save the profile as the career section of the central user profile |
| POST | `/scan` | Start an async scan → `{job_id}` |
| GET | `/scan/status/{job_id}` | Poll; `result` holds the `JobMatchResponse` |
| GET | `/scan/jobs` | List scan jobs |
| POST | `/scan/jobs/{job_id}/cancel` | Cancel a pending/running scan |
| DELETE | `/scan/jobs/{job_id}` | Delete a scan-job record |
| GET | `/runs` | Persisted run summaries |
| GET | `/runs/{run_id}` | A run plus its ranked jobs |
| GET | `/listings?status=&limit=` | Aggregated listings: latest ranked snapshot per fingerprint + user state |
| PATCH | `/listings/{fingerprint}` | Set a listing's status (`new`/`favorite`/`not_interested`/`poor_fit`/`archived`) |

## Persistence

Postgres tables `job_matching_runs`, `job_matching_ranked_jobs`, and
`job_matching_listing_states` (user triage state keyed by posting fingerprint;
see `postgres/__init__.py`), registered via `shared.app.create_team_app`'s
`postgres_schema=JOB_MATCHING_SCHEMA`. This team also depends on the central
`user_profile` schema (`user_profiles`/`user_profile_associations`) for
career-profile reads/writes, declared via `create_team_app`'s
`extra_postgres_schemas=[USER_PROFILE_SCHEMA]` rather than registered
separately from an `on_startup` hook — both schemas land together on
`app.state.postgres_schemas`, which the team-service wrapper registers
*before* starting this team's Temporal worker, so a queued
`job_matching_prepare_scan` activity can never read `user_profiles` before it
exists on a fresh database. Requires `POSTGRES_HOST`; there is no SQLite
fallback.

## Execution

`POST /scan` runs asynchronously and the caller polls `GET /scan/status/{job_id}`.
Two interchangeable runtimes drive the same job-store state machine
(`pending → running → completed/failed`), so callers see identical behavior
either way:

- **Thread mode** (default): the scan runs on a daemon thread in-process.
- **Temporal mode** (when `TEMPORAL_ADDRESS` is set): the scan is dispatched to a
  durable `JobMatchingWorkflow` (shared.temporal Pattern A). The workflow is
  visible in the Temporal UI and an in-flight scan survives a worker/process
  restart. The worker boots via `temporal/worker.py`
  (`TEAM_TEMPORAL_WORKER_MODULE`/`_FUNC` in docker-compose, run by the
  team_service entrypoint), with the API lifespan (`_start_temporal_worker_backstop`
  in `api/main.py`) as a backstop when the app is served standalone (`uvicorn …:app`).
  The team is also registered in `shared.temporal.teams_registry` for any future
  in-process `start_all_team_workers` host.

## Configuration

| Variable | Purpose |
|---|---|
| `OLLAMA_API_KEY` | Required for live web search (Ollama `web_search` API). |
| `JOB_SEEKER_PROFILE_PATH` | Override path to the profile YAML. |
| `JOB_SEEKER_PROFILE_STRICT` | When true, missing/invalid profile raises instead of falling back. |
| `JOB_MATCHING_SERVICE_URL` | Upstream URL the unified API proxies to. |
| `OLLAMA_WEB_SEARCH_BASE_URL` | Optional override for the web-search endpoint. |
| `TEMPORAL_ADDRESS` | When set, scans run on a durable Temporal workflow instead of a daemon thread. |

## Agent Studio

Two manifests under `agent_console/manifests/`:

- `job_matching.ranker` — pure-LLM scoring, sandbox-runnable.
- `job_matching.scanner` — requires live web access, tagged
  `requires-live-integration` (catalogued but not sandbox-runnable).

## Tests

```bash
cd backend
pytest agents/job_matching_team -q            # offline unit suite
pytest agents/job_matching_team -m integration  # Postgres store round-trip
```

Unit tests mock the LLM, web search, and fetch, so the suite runs fully offline.
