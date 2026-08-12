# Khala Full Stack (Postgres, Temporal, Ollama, Agents)

This directory defines a **Docker Compose stack** that runs:

- **PostgreSQL 18** – shared database with `temporal` and `khala` databases (created at first run). The data volume is `postgres_data_v18`; the name is suffixed with the major version so each bump starts from an empty volume declaratively, without `docker compose down -v`. Orphaned previous-version volumes can be cleaned up with `docker volume prune`.
- **Neo4j** – required knowledge-graph store for agents (Graphiti). The unified API does **not** open a Graphiti client or run background graph sync unless you set `NEO4J_BOLT_URL=bolt://neo4j:7687` (extra memory/CPU for that process).
- **Temporal** – workflow engine (Postgres-backed, no Elasticsearch)
- **Temporal UI** – Web UI for workflows
- **Ollama** (optional) – local Ollama server if you override LLM to use it
- **Khala** – all agent APIs; **default LLM is Ollama Cloud** (https://ollama.com) when running from Docker

## Quick start

1. **Copy env and set your Ollama Cloud API key**

   ```bash
   cp docker/.env.example docker/.env
   # Edit docker/.env and set OLLAMA_API_KEY (from https://ollama.com/settings/keys)
   ```

2. **Start the stack** (from repo root)

   Agent output and project data are stored in the **`agents_workspace`** Docker volume (mounted at `/workspace` in the agents container). This data persists when containers are stopped or recreated. Postgres data is stored in the **`postgres_data_v18`** volume (the suffix tracks the Postgres major version; bumping Postgres renames the volume so the next `up` starts from an empty data dir). To remove all persisted data, use `docker compose down -v` (the `-v` flag removes named volumes).

   Use `--env-file docker/.env` so variables from `docker/.env` (e.g. `OLLAMA_API_KEY`) are passed into the containers.

   ```bash
   docker compose -f docker/docker-compose.yml --env-file docker/.env up --build
   ```

   Compose creates the `khala-stack` bridge network (subnet `172.28.0.0/24`) automatically on first `up`; nothing else needs to run beforehand.

3. **Access**

   | Service        | URL                         |
   |----------------|-----------------------------|
   | **Angular UI** | http://localhost:4201       (proxies /api to agents; nested routes e.g. SE Planning/Coding Team, Investment Advisor/Strategy Lab, Agentic roster) |
   | Agents API     | http://localhost:8888       (direct) |
   | Temporal UI    | http://localhost:8080       |
   | Prometheus     | http://localhost:9090       (scrape targets: `/targets`; metric browser: `/graph`) |
   | Grafana        | http://localhost:3000       (login `admin`/`admin` by default; Khala folder holds the FastAPI overview dashboard) |
   | Postgres       | localhost:5432 (user `postgres` / `temporal` / `khala`) |
   | Neo4j Browser  | http://localhost:7474   (Bolt `localhost:7687`; agent knowledge graph — not used by the unified API unless `NEO4J_BOLT_URL` is set) |
   | Ollama (local) | http://localhost:11434      |

   Use the **Angular UI at 4201** so API requests go through the same origin and nginx proxies them to the backend. If you run only the API container and use the UI with `ng serve`, point the dev API base to `http://localhost:8888` in `user-interface/src/environments/environment.ts`.

## Required environment variables

- **OLLAMA_API_KEY** – Create at [ollama.com/settings/keys](https://ollama.com/settings/keys). Required for Ollama Cloud (Option A). Passed into the agents container so the LLM client can call `https://ollama.com` with `Authorization: Bearer <key>`.
- **POSTGRES_USER**, **POSTGRES_PASSWORD** – credentials for the default Postgres superuser; init scripts create `temporal` and `khala` DBs and users from these. `docker-compose.yml` has no fallback default for either — `docker compose up`/`config` fails fast with an `is required` error if they're unset, so they must be set explicitly (e.g. via `docker/.env`).

Optional (defaults in compose / `docker/.env.example`; copy to `docker/.env` and set as needed):

- **LLM_BASE_URL** – default is `https://ollama.com` (Ollama Cloud). Set to `http://ollama:11434` to use the local Ollama container instead.
- **LLM_MODEL** – default `deepseek-v4-pro:cloud`
- **POSTGRES_DB** – default `postgres`; the database name for the default Postgres superuser.
- **NEO4J_BOLT_URL** – unset on `khala` by default (no Graphiti sync in the reverse proxy). Set to `bolt://neo4j:7687` to opt that process into graph sync; see `docs/ENV_VARS.md`. **NEO4J_PASSWORD** (and related Neo4j vars) configure the always-on Neo4j container for agents.

Personal Assistant credential encryption uses a key generated at **Docker image build time** (stored in the image), so credentials persist across container restarts without setting any env var.

## Viewing server logs (testing)

When **ENABLE_LOG_API=1** in the agents service, you can fetch recent supervisor logs over HTTP:

```bash
# Enable in .env: ENABLE_LOG_API=1, then restart the stack.

# Last 100 lines of Software Engineering API log
curl "http://localhost:8888/api/software-engineering/logs?service=sw_api&lines=100"

# Include stderr logs
curl "http://localhost:8888/api/software-engineering/logs?service=sw_api&lines=200&stderr=1"

# All API logs (no postgres/dockerd)
curl "http://localhost:8888/api/software-engineering/logs?service=all&lines=500"
```

Query params:

- **service** – `sw_api`, `blogging_api`, `market_research_api`, etc., or `all`
- **lines** – number of lines (default 500, max 10000)
- **stderr** – set to `1` to include `*_err.log` files

When **ENABLE_LOG_API** is not set or is 0, the endpoint returns **404** so it is not exposed in production.

## Data persistence

| Volume            | Service        | Purpose |
|-------------------|----------------|---------|
| `postgres_data_v18` | PostgreSQL   | Database files (Temporal + app DBs). Suffix tracks the Postgres major version — renaming the volume on each major bump gives a fresh data dir declaratively. |
| `agents_workspace`| khala | Agent workspace at `/workspace` (repos, generated code, artifacts). |
| `prometheus_data` | Prometheus    | Prometheus TSDB (metric samples). Retention window controlled by `PROMETHEUS_RETENTION` (default `15d`). |
| `grafana_data`    | Grafana       | Grafana state (users, saved dashboards, datasource cache). |

Data in these volumes survives `docker compose down` and container restarts. To wipe persisted data, run `docker compose down -v`.

## Port summary

| Port  | Service        |
|-------|----------------|
| 5432  | PostgreSQL     |
| 7233  | Temporal gRPC  |
| 8080  | Temporal UI    |
| 3000  | Grafana        |
| 9090  | Prometheus     |
| 4201  | Angular UI (proxies /api to agents) |
| 8888  | Agents API (direct) |
| 8108  | Agentic Team Provisioning API (direct; also proxied at `/api/agentic-team-provisioning` on 8888) |
| 11434 | Ollama (optional) |

Agents direct ports (when needed): 18000–18005 map to APIs 8000–8005.

The Unified API (`khala` on 8888) only registers each team’s `/api/...` route when the matching `*_SERVICE_URL` is set (see `docker-compose.yml`). **Agentic Team Provisioning** requires `AGENTIC_TEAM_PROVISIONING_SERVICE_URL` pointing at the `agentic-team-provisioning-service` container (included in the full stack).

## Resource limits (khala)

The **khala** service is configured for 8 vCPUs and 1G memory (`deploy.resources` plus legacy `cpus` / `mem_limit` / `mem_reservation`). 1G/512M reservation reflects a measured single-worker RSS floor of ~140 MiB post the workers=1 / Pattern-A-boot / Strands-import-edge fixes (see the comment above the `khala` service in `docker-compose.yml`); raise it if `docker stats` shows sustained pressure closer to the limit in a real deployment. After changing these in `docker-compose.yml`, recreate the container so limits apply:

```bash
docker compose -f docker/docker-compose.yml down khala
docker compose -f docker/docker-compose.yml --env-file docker/.env up -d khala
```

On **macOS** with Docker Desktop, container memory is capped by the VM's memory limit (Docker Desktop → Settings → Resources). If 1G is not applied, raise the VM limit and restart Docker.

## Unified API image (slim dependencies)

The **khala** (Unified API) image is mostly a reverse proxy: of the teams
configured in `TEAM_CONFIGS` (`backend/unified_api/config.py`), all but a
handful (`in_process=True` — `user_profile`, `product_delivery`,
`agent_studio`, plus platform modules `agent_console`, `agent_registry`,
`agent_cognition`, `team_assistant`) are proxied over HTTP via
`unified_api/team_proxy.py` to their own per-team container and never
imported by this process. `backend/Dockerfile` reflects that: it installs
`backend/requirements-unified-api.txt`, an audited minimal dependency set for
the proxy process's own cold path, instead of the full
`backend/agents/requirements.txt` every per-team container installs.

| Image | Requirements file | Scope |
|---|---|---|
| `khala` (Unified API) | `backend/requirements-unified-api.txt` | Slim — only what the proxy process itself imports at startup or during an always-mounted in-process route. |
| `team_service`, `blogging_service`, `job_service`, `agent_sandbox_image` (and per-team Dockerfiles) | `backend/agents/requirements.txt` (or a per-team file) | Full — every team's agent/tool code actually runs here, including heavy per-team packages (e.g. `strands-agents` across most teams, `pandas`/`numpy`/`pyarrow` for `investment_team`). |

See [`docs/UNIFIED_API_DEPENDENCY_AUDIT.md`](../docs/UNIFIED_API_DEPENDENCY_AUDIT.md) for the full package-by-package audit behind this split.

## Team-assistant kill-switch and lazy mount

Team-assistant conversational sub-apps (`<team-prefix>/assistant`) are never
all mounted eagerly at `khala` startup. Each one is registered as a
lightweight mount spec and only actually constructed and mounted on that
team's first matching request — a cold-request cost paid once per team, only
for teams that receive assistant traffic. Set
`UNIFIED_API_TEAM_ASSISTANTS_ENABLED=false` (`docker/.env.example`) to skip
registration entirely: zero assistant sub-apps are ever mounted, regardless
of traffic, while team proxy routes and health checks stay unaffected. See
[`docs/ENV_VARS.md`](../docs/ENV_VARS.md#unified_api_team_assistants_enabled)
for full detail.

## Agents and Postgres

When running in this stack, the **khala** service uses the **stack’s Postgres** (database `khala`, user `khala`) via **POSTGRES_HOST=postgres**. The container does not start its own PostgreSQL. The init script in `docker/postgres/init/` creates the `khala` database and user on first run.

### Per-sandbox compose stacks (Agent Console)

Agent Console sandboxes run as **per-sandbox docker compose projects** — each agent invocation gets its own isolated stack containing the agent container, **plus its own Postgres, Temporal, Prometheus, and Grafana**. Nothing in those stacks joins this `khala-stack` compose network, so the agent runs as if it were in a live environment but cannot touch the long-lived services here. (Permission tiers were removed in #456: every sandbox is provisioned with full access on its own services.)

- The compose template lives at `backend/agent_sandbox_image/sandbox-stack.yml`. The provisioner (`backend/agents/agent_team_studio/agent_provisioning_team/sandbox/provisioner.py`) renders it into `${AGENT_CACHE}/agent_provisioning/sandboxes/stacks/<project>/` and runs `docker compose -p <project> -f <rendered> up -d`.
- The Postgres password is freshly generated per sandbox; Postgres / Temporal / Prometheus / Grafana speak to one another over the project's private bridge. Only the agent's `8090/tcp` is published to the host on a loopback-bound ephemeral port so the unified API can proxy invokes.
- Idle sandboxes are torn down with `docker compose down -v` (named volumes go too) after `AGENT_PROVISIONING_SANDBOX_IDLE_MINUTES` (default 5) — no run inherits state from a previous one.

The legacy `POSTGRES_PASSWORD_SANDBOX_<TEAM>` env vars and the `docker/postgres/init/04-create-sandbox-team-roles.sh` init script are vestigial under this model — they only mattered when sandboxes shared this stack's Postgres. They're harmless if left in place, but new deployments don't need them.

Resource cost: each sandbox stack is ~1.5 GB resident and ~30 s cold start. With a 16 GB host, expect roughly 6–8 concurrent sandboxes before swapping; the idle reaper keeps the steady state small.

### Sandbox secrets

Sandbox containers **never** receive `OLLAMA_API_KEY`, `ANTHROPIC_API_KEY`, or the freshly generated `POSTGRES_PASSWORD` via `docker run -e` flags — so they don't appear in `docker inspect` and aren't visible via `docker exec <sandbox> env`.

The provisioner writes each sandbox's secrets to a 0400 `KEY=VALUE` file under `${AGENT_CACHE}/agent_provisioning/sandboxes/stacks/<project>/agent.env` on the host and bind-mounts it read-only at `/run/secrets/sandbox-env`. The in-sandbox entrypoint reads the file into `os.environ` and unlinks the in-sandbox view; the host file (and the rest of the per-project directory) is removed when the stack is torn down.

Verify after a run:

```bash
sandbox=$(docker ps --format '{{.Names}}' | grep '^khala-sbx-.*-agent$' | head -1)
docker exec "$sandbox" env | grep -E 'OLLAMA|POSTGRES_PASSWORD|ANTHROPIC'
# Expected: (no output)
docker inspect "$sandbox" | jq '.[0].Config.Env' | grep -E 'OLLAMA|POSTGRES_PASSWORD|ANTHROPIC'
# Expected: (no match)
```

## Observability (Prometheus + Grafana)

The stack ships with a Prometheus server and Grafana instance pre-wired.

- **Prometheus** at http://localhost:9090 scrapes `/metrics` on the unified API (`khala:8080`), the job service (`job-service:8085`), and every team microservice on its own port. Config file: `docker/prometheus/prometheus.yml`. View scrape health at http://localhost:9090/targets — every target should report `UP` once containers are healthy.
- **Grafana** at http://localhost:3000 (default `admin`/`admin`, override via `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD` in `.env`). The Prometheus datasource is provisioned automatically from `docker/grafana/provisioning/datasources/prometheus.yml`. A starter **Khala FastAPI Overview** dashboard (request rate, p95 latency, 5xx rate, scrape health) is provisioned under the "Khala" folder.
- **Retention** is controlled by `PROMETHEUS_RETENTION` (default `15d`). Data persists in the `prometheus_data` and `grafana_data` named volumes.
- **Grafana admin password caveat**: `GRAFANA_ADMIN_PASSWORD` is only read on first boot (when `grafana_data` is empty). Changing it later has no effect — reset via the Grafana UI, or remove the volume with `docker volume rm docker_grafana_data` to re-seed from env vars.

Metrics are produced by `prometheus-fastapi-instrumentator` which is installed into the unified API (`backend/unified_api/main.py`), the job service (`backend/job_service/main.py`), the blogging service (`backend/blogging_service/entrypoint.py`), and the generic team entrypoint (`backend/team_service/entrypoint.py`). That means every team container automatically exposes `/metrics` without any per-team code changes. Dropping additional dashboard JSON files into `docker/grafana/provisioning/dashboards/` picks them up automatically every 30 seconds.

Add a new team? Edit `docker/prometheus/prometheus.yml` and append a new target entry to the `team-services` job with the service's DNS name and port, then add a matching `extra_hosts` entry to the `prometheus` service in `docker-compose.yml`.

## Tracing (Tempo)

Grafana Tempo (`docker/tempo/tempo.yaml`) is the trace backend, capped at 1G
memory (`mem_limit`/`deploy.resources.limits.memory` on the `tempo` service in
`docker-compose.yml`). Only opted-in containers (`se-service`,
`investment-service`, `branding-service`) export OTLP traces here
(`OTEL_EXPORTER_OTLP_ENDPOINT=http://tempo:4318`); other team services keep
spans in-process. Grafana queries Tempo via the pre-provisioned "Tempo"
datasource (`docker/grafana/provisioning/datasources/tempo.yml`, uid
`khala-tempo`).

Tempo is a Go binary, and the Go garbage collector doesn't know about cgroup
memory limits — by default (`GOGC=100`) the heap can roughly double before a
collection runs, which produces RSS spikes that can blow past the 1G cap even
when average usage is fine. The `tempo` service sets `GOMEMLIMIT=800MiB` (a
soft heap target the runtime actively collects toward, kept below the 1G cap
to leave headroom for non-Go-heap memory like mmap'd WAL segments) and
`GOGC=50` (collects more eagerly than the Go default) so peak RSS stays
smoothed under the cgroup cap instead of sawtoothing past it.

Query-path memory is bounded by three cooperating caps in `tempo.yaml`:

- **`querier.max_concurrent_queries`** (5, Tempo default 20) — limits concurrently-executing
  query jobs; each one holds decoded trace blocks in memory.
- **`query_frontend.max_outstanding_per_tenant`** (100, Tempo default 2000) — limits
  in-flight query requests queued at the frontend before it starts returning
  HTTP 429 instead of buffering more work.
- **`query_frontend.search.concurrent_jobs`** (200, Tempo default 1000) — limits
  concurrently-running sharded search sub-jobs, each of which holds decoded
  block data while it runs.

**Tradeoff**: these caps are set well below Tempo's defaults to protect the
1G cgroup. Under heavy parallel dashboard use (many Grafana panels refreshing
at once), requests queue and search jobs serialize more than they would with
Tempo's out-of-the-box defaults — trading some query latency (and occasional
HTTP 429s) for a bounded, predictable memory footprint. Raise these values
(and likely the 1G limit) if sustained query queuing/429s show up in real
usage. See the concurrent-load validation script in the PR description for
the querier + query_frontend sub-issue for how these values were checked
under load.

## Load Testing (k6)

`docker/k6/load_test_unified_api.js` is a [Grafana k6](https://k6.io) script that fans out
concurrent GET requests across a rotation of unified-api's *proxied* teams, hitting each team's
forwarded `/health` endpoint through `khala:8080`. It's the repeatable, concurrency-controlled
alternative to the one-off curl loop in the Verification section below — use it whenever you need
sustained load rather than a quick sanity ping (e.g. to validate memory/right-sizing changes under
realistic traffic). k6 was chosen over a hand-rolled script because it already reports
throughput/latency out of the box and pairs naturally with the Prometheus + Grafana stack above;
see `system_design/adr/ADR-011-grafana-k6-load-testing.md` for the full decision record.

The k6 service is opt-in via the `load-test` Compose profile, so it never starts on a plain
`docker compose up`:

```bash
docker compose -f docker/docker-compose.yml --env-file docker/.env up -d --build
docker compose -f docker/docker-compose.yml --profile load-test run --rm k6
```

Concurrency (virtual users) and duration default to 10 VUs / 30s and are configurable via env vars
or k6's native CLI flags:

```bash
# Env vars (picked up by the compose service definition)
K6_VUS=20 K6_DURATION=60s docker compose -f docker/docker-compose.yml --profile load-test run --rm k6

# Native k6 flags (override the script's `options`, or run k6 directly against the stack
# without Docker if you have k6 installed locally)
docker compose -f docker/docker-compose.yml --profile load-test run --rm k6 run --vus 20 --duration 60s /scripts/load_test_unified_api.js
k6 run docker/k6/load_test_unified_api.js -e BASE_URL=http://localhost:8888 --vus 20 --duration 60s
```

`K6_TEAMS` (comma-separated team URL segments, default `blogging,personal-assistant,market-research,soc2-compliance,social-marketing,branding`)
picks which proxied teams to target — pick from any team listed in the Port summary above except
`user-profile`, `product-delivery`, and `agent-studio` (in-process, never proxied, so hitting them
wouldn't exercise the proxy path this harness is for).

k6 prints throughput and latency automatically at the end of every run — no extra flags needed:

```
     http_req_duration..............: avg=12.4ms min=3.1ms med=9.8ms max=118ms p(90)=22ms p(95)=31ms
     http_req_failed.................: 0.00%  ✓ 0        ✗ 5412
     http_reqs.......................: 5412   180.4/s
```

## Memory / RSS Measurement

`docker/scripts/measure_unified_api_rss.sh` samples unified-api's process RSS across four
operating states — idle, DB-pool-warm, Temporal-client-active, and peak-concurrency-burst — to
build a reproducible memory profile for right-sizing the `khala` service's resource limits (see
the `deploy.resources`/`mem_limit` block on the `khala` service above). It reads
`process_resident_memory_bytes{job="unified-api"}` from Prometheus (already scraped, per the
Observability section above — no new endpoint or `docker stats` shell-out needed) using the same
`curl .../api/v1/query | jq` pattern as the Prometheus-targets verification step below.

**Methodology**

- **Warm-up period**: 30s of zero driven traffic before each `idle`/`temporal-active` sample batch
  (`WARMUP_SECONDS`), so transient startup/GC-adjacent noise settles before sampling.
- **Sampling interval**: 5s between samples, 5 samples per state by default
  (`SAMPLE_INTERVAL_SECONDS`/`SAMPLE_COUNT`); the script reports the median and max per state.
- **`idle`**: sampled immediately after `/health` responds and the warm-up period elapses. In the
  standard compose config this baseline already includes the Postgres pool at its min size (2
  connections, opened eagerly at startup) and the Temporal client connected (also automatic at
  startup when `TEMPORAL_ADDRESS` is set) — there's no code path that defers either past process
  readiness, so `idle` is "freshly booted, standard config, no request traffic," not "nothing
  initialized yet."
- **`db-pool-warm`**: fires 12 concurrent requests (`DB_WARM_CONCURRENCY`, above the pool's default
  10-connection max) at `/api/product-delivery/products` (`DB_WARM_PATH`) to force the Postgres
  pool to grow beyond min size, waits 5s to settle (`DB_WARM_SETTLE_SECONDS`, comfortably inside
  psycopg_pool's ~300s default idle-reclaim window), then samples — isolating the incremental RSS
  cost of a fully-grown pool. Targets that route rather than `/health`: `/health`'s live-DB-probe
  branch runs through a fixed 2-worker executor (`_get_probe_executor` in
  `backend/unified_api/main.py`), so concurrent `/health` traffic can never grow the pool past that
  cap, no matter how many requests are in flight — `/api/product-delivery/products` is a plain
  synchronous route that hits Postgres directly per request through Starlette's much larger default
  thread pool. Aborts with an error instead of sampling if fewer than half the warm-up requests
  succeeded, since that would produce a misleading "pool-warm" measurement against a pool that
  never actually warmed.
- **`temporal-active`**: sampled identically to `idle`, because the Temporal client isn't
  toggleable at runtime — it's a boot-time decision. To isolate its incremental cost, run the
  script twice across two container boots: once with `UNIFIED_API_AGENT_STUDIO_TEMPORAL_WORKER=false`
  and `UNIFIED_API_SANDBOX_TEMPORAL_WORKER=false` set on the `khala` service (Temporal-disabled
  baseline — run `idle` against this boot), then again with the default config (Temporal enabled —
  run `temporal-active`), and diff the two summaries.
- **`peak-burst`**: launches the [k6 harness](#load-testing-k6) at `VUS=50 DURATION=60s`
  (`PEAK_VUS`/`PEAK_DURATION` — "max configured concurrency" per the harness's own tunables) and
  samples RSS every 15s (`PEAK_SAMPLE_INTERVAL_SECONDS`) for the burst's duration, reporting the
  max observed value as the peak. The interval defaults to 15s — matching
  `docker/prometheus/prometheus.yml`'s global `scrape_interval` — rather than something shorter,
  since sampling faster than Prometheus actually scrapes just re-reads the same cached value and
  silently produces fewer independent observations than it looks like. Keep this at or above your
  stack's configured `scrape_interval` if you change it.

**Running it**

```bash
docker compose -f docker/docker-compose.yml --env-file docker/.env up -d --build
./docker/scripts/measure_unified_api_rss.sh idle
./docker/scripts/measure_unified_api_rss.sh db-pool-warm
./docker/scripts/measure_unified_api_rss.sh temporal-active
./docker/scripts/measure_unified_api_rss.sh peak-burst
```

All four subcommands append to the same CSV (default `rss_measurements.csv` in the current
directory, override with `OUTPUT_CSV`) with columns `timestamp,state,sample_index,rss_bytes`, and
the `state` column always matches the subcommand name exactly (`idle`, `db-pool-warm`,
`temporal-active`, `peak-burst`). Each invocation prints a median/max-in-MiB summary as it runs.
Since the default filename has no timestamp, move or rename it (or set `OUTPUT_CSV` explicitly)
between unrelated measurement sessions so they don't mix in one file. Attach or link the CSV,
along with the printed summaries, wherever you're recording the measurement results for
reproducibility.

**Tests**: `docker/scripts/tests/test_measure_unified_api_rss.sh` covers the median/max math (odd
and even sample counts, including Prometheus-style scientific-notation values), state-label
consistency, `sample_index` sequencing, usage/bad-argument handling, failing loudly rather than
reporting success when a state collects zero usable samples, and — via local mock HTTP servers
standing in for `/health` and Prometheus, no live stack required — an end-to-end run of the `idle`
and `db-pool-warm` subcommands. Run it with `bash docker/scripts/tests/test_measure_unified_api_rss.sh`.

## Verification

After starting the stack:

1. **Compose up** – `docker compose -f docker/docker-compose.yml --env-file docker/.env up -d --build` should bring up all services without errors.
2. **Temporal UI** – Open http://localhost:8080 and confirm the Temporal Web UI loads.
3. **Agents** – `curl http://localhost:8888/health` should return HTTP 200 with a body like `{"status": "healthy", "version": "1.0.0", "teams": [...]}` (agents use stack Postgres and Ollama Cloud when configured). `status` is `"degraded"` rather than `"healthy"` if any enabled proxy team is missing its `*_SERVICE_URL`, or `"unhealthy"` if an in-process team's Postgres schema registration failed — check the per-team `teams[]` entries to see which one.
4. **Logs API** – With `ENABLE_LOG_API=1` in `.env`, `curl "http://localhost:8888/api/software-engineering/logs?service=sw_api&lines=100"` should return 200 and log content. With `ENABLE_LOG_API` unset, the same URL should return 404.
5. **Metrics endpoints** – `curl -sf http://localhost:8888/metrics | head` and the same on `:8585` (job service) and `:8090`–`:8110` (team services) should return Prometheus text-format output (`# HELP ...`).
6. **Prometheus targets** – Open http://localhost:9090/targets; all rows should be green (`UP`). Or run `curl -s 'http://localhost:9090/api/v1/query?query=up' | jq '.data.result[] | {service:.metric.service, up:.value[1]}'`.
7. **Grafana datasource** – `curl -sf -u admin:admin http://localhost:3000/api/datasources | jq` should list one `Prometheus` datasource. Then open http://localhost:3000 → Dashboards → Khala → **Khala FastAPI Overview** and confirm the panels render live data after generating some traffic (e.g. `for i in {1..20}; do curl -sf http://localhost:8888/health > /dev/null; done`, or for a repeatable, concurrency-controlled load run see the "Load Testing (k6)" section above).
8. **Tempo tracing** – `curl -sf http://localhost:3200/ready` should return `ready`. Generate a few traces by calling an opted-in service a few times through `khala:8888` (e.g. an `se-service` route), wait ~10s for `ingester.trace_idle_period` to flush, then `curl -s 'http://localhost:3200/api/search?tags=' | jq '.traces | length'` should return a non-zero count. Pick a `traceID` from that response and confirm `curl -s http://localhost:3200/api/traces/<traceID> | jq '.batches | length'` returns non-zero. Finally, in Grafana open **Explore** → **Tempo** datasource and confirm the same trace is browsable via search and via trace-by-ID lookup.

## Security

- Do not commit `.env` with real secrets. Use `.env.example` as a template only.
- For production, do not expose Temporal or Postgres to the public internet; keep them on internal networks.
- Leave **ENABLE_LOG_API** unset or 0 in production so the logs endpoint is disabled.

## Khala platform

This package is part of the [Khala](../README.md) monorepo (Unified API, Angular UI, and full team index).
