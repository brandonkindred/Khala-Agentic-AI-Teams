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

The standing criteria live in a YAML file resolved in this order:

1. `$JOB_SEEKER_PROFILE_PATH`
2. `$AGENT_CACHE/job_seeker_profile.yaml`
3. the bundled `profile/job_seeker_profile.example.yaml` (with a WARN log)

Set `JOB_SEEKER_PROFILE_STRICT=true` to raise instead of falling back. Each
scan request may override any profile field via `profile_overrides`.

## HTTP API

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness |
| GET | `/profile` | Resolved job-seeker profile |
| POST | `/scan` | Start an async scan → `{job_id}` |
| GET | `/scan/status/{job_id}` | Poll; `result` holds the `JobMatchResponse` |
| GET | `/scan/jobs` | List scan jobs |
| POST | `/scan/jobs/{job_id}/cancel` | Cancel a pending/running scan |
| DELETE | `/scan/jobs/{job_id}` | Delete a scan-job record |
| GET | `/runs` | Persisted run summaries |
| GET | `/runs/{run_id}` | A run plus its ranked jobs |

## Persistence

Postgres tables `job_matching_runs` and `job_matching_ranked_jobs` (see
`postgres/__init__.py`), registered from the FastAPI lifespan via
`shared_postgres.register_team_schemas`. Requires `POSTGRES_HOST`; there is no
SQLite fallback.

## Configuration

| Variable | Purpose |
|---|---|
| `OLLAMA_API_KEY` | Required for live web search (Ollama `web_search` API). |
| `JOB_SEEKER_PROFILE_PATH` | Override path to the profile YAML. |
| `JOB_SEEKER_PROFILE_STRICT` | When true, missing/invalid profile raises instead of falling back. |
| `JOB_MATCHING_SERVICE_URL` | Upstream URL the unified API proxies to. |
| `OLLAMA_WEB_SEARCH_BASE_URL` | Optional override for the web-search endpoint. |

## Agent Console

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
