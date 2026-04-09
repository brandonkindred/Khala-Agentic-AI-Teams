# Strands Full Stack (Postgres, Temporal, Ollama, Agents)

This directory defines a **Docker Compose stack** that runs:

- **PostgreSQL 18** – shared database with `temporal` and `strands` databases (created at first run). The data volume is `postgres_data_v18`; the name is suffixed with the major version so each bump starts from an empty volume declaratively, without `docker compose down -v`. Orphaned previous-version volumes can be cleaned up with `docker volume prune`.
- **Temporal** – workflow engine (Postgres-backed, no Elasticsearch)
- **Temporal UI** – Web UI for workflows
- **Ollama** (optional) – local Ollama server if you override LLM to use it
- **Strands Agents** – all agent APIs; **default LLM is Ollama Cloud** (https://ollama.com) when running from Docker

## Quick start

1. **Copy env and set your Ollama Cloud API key**

   ```bash
   cp docker/.env.example docker/.env
   # Edit docker/.env and set OLLAMA_API_KEY (from https://ollama.com/settings/keys)
   ```

2. **Start the stack** (from repo root)

   Agent output and project data are stored in the **`agents_workspace`** Docker volume (mounted at `/workspace` in the agents container). This data persists when containers are stopped or recreated. Postgres data is stored in the **`postgres_data_v18`** volume (the suffix tracks the Postgres major version; bumping Postgres renames the volume so the next `up` starts from an empty data dir). To remove all persisted data, use `docker compose down -v` (the `-v` flag removes named volumes).

   Use `--env-file docker/.env` so variables from `docker/.env` (e.g. `OLLAMA_API_KEY`) are passed into the containers.

   **Docker:** One-time, ensure the network exists: `./docker/ensure-network.sh` (uses `docker network create` if you have Docker).
   ```bash
   ./docker/ensure-network.sh
   docker compose -f docker/docker-compose.yml --env-file docker/.env up --build
   ```

   **Podman:** Use `podman compose` as a drop-in (requires a running Podman machine; on Windows, install WSL2 then `podman machine init` and `podman machine start`). **One-time:** create the external network so Compose doesn't try to manage it (avoids repeated "IPAM option driver has changed" errors):
   ```bash
   ./docker/ensure-network.sh
   podman compose -f docker/docker-compose.yml --env-file docker/.env up --build
   ```

3. **Access**

   | Service        | URL                         |
   |----------------|-----------------------------|
   | **Angular UI** | http://localhost:4201       (proxies /api to agents; nested routes e.g. SE Planning/Coding Team, Investment Advisor/Strategy Lab, Agentic roster) |
   | Agents API     | http://localhost:8888       (direct) |
   | Temporal UI    | http://localhost:8080       |
   | Prometheus     | http://localhost:9090       (scrape targets: `/targets`; metric browser: `/graph`) |
   | Grafana        | http://localhost:3000       (login `admin`/`admin` by default; Strands folder holds the FastAPI overview dashboard) |
   | Postgres       | localhost:5432 (user `postgres` / `temporal` / `strands`) |
   | Ollama (local) | http://localhost:11434      |

   Use the **Angular UI at 4201** so API requests go through the same origin and nginx proxies them to the backend. If you run only the API container and use the UI with `ng serve`, point the dev API base to `http://localhost:8888` in `user-interface/src/environments/environment.ts`.

## Required environment variables

- **OLLAMA_API_KEY** – Create at [ollama.com/settings/keys](https://ollama.com/settings/keys). Required for Ollama Cloud (Option A). Passed into the agents container so the LLM client can call `https://ollama.com` with `Authorization: Bearer <key>`.

Optional (defaults in compose / `docker/.env.example`; copy to `docker/.env` and set as needed):

- **LLM_BASE_URL** – default is `https://ollama.com` (Ollama Cloud). Set to `http://ollama:11434` to use the local Ollama container instead.
- **LLM_MODEL** – default `qwen3.5:397b-cloud`
- **POSTGRES_USER**, **POSTGRES_PASSWORD**, **POSTGRES_DB** – used for the default Postgres superuser; init scripts create `temporal` and `strands` DBs and users.

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
| `agents_workspace`| strands-agents | Agent workspace at `/workspace` (repos, generated code, artifacts). |
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

The Unified API (`strands-agents` on 8888) only registers each team’s `/api/...` route when the matching `*_SERVICE_URL` is set (see `docker-compose.yml`). **Agentic Team Provisioning** requires `AGENTIC_TEAM_PROVISIONING_SERVICE_URL` pointing at the `agentic-team-provisioning-service` container (included in the full stack).

## Resource limits (strands-agents / Podman)

The **strands-agents** service is configured for 8 vCPUs and 2G memory (`deploy.resources` plus legacy `cpus` / `mem_limit`). After changing these in `docker-compose.yml`, recreate the container so limits apply:

```bash
podman compose -f docker/docker-compose.yml down strands-agents
podman compose -f docker/docker-compose.yml --env-file docker/.env up -d strands-agents
```

On **macOS** with Podman Machine, container memory is capped by the machine’s memory. If 2G is not applied, increase it and recreate the machine: `podman machine stop; podman machine set --memory 8192; podman machine start` (then recreate the container).

## Agents and Postgres

When running in this stack, the **strands-agents** service uses the **stack’s Postgres** (database `strands`, user `strands`) via **POSTGRES_HOST=postgres**. The container does not start its own PostgreSQL. The init script in `docker/postgres/init/` creates the `strands` database and user on first run.

## Observability (Prometheus + Grafana)

The stack ships with a Prometheus server and Grafana instance pre-wired.

- **Prometheus** at http://localhost:9090 scrapes `/metrics` on the unified API (`strands-agents:8080`), the job service (`job-service:8085`), and every team microservice on its own port. Config file: `docker/prometheus/prometheus.yml`. View scrape health at http://localhost:9090/targets — every target should report `UP` once containers are healthy.
- **Grafana** at http://localhost:3000 (default `admin`/`admin`, override via `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD` in `.env`). The Prometheus datasource is provisioned automatically from `docker/grafana/provisioning/datasources/prometheus.yml`. A starter **Strands FastAPI Overview** dashboard (request rate, p95 latency, 5xx rate, scrape health) is provisioned under the "Strands" folder.
- **Retention** is controlled by `PROMETHEUS_RETENTION` (default `15d`). Data persists in the `prometheus_data` and `grafana_data` named volumes.
- **Grafana admin password caveat**: `GRAFANA_ADMIN_PASSWORD` is only read on first boot (when `grafana_data` is empty). Changing it later has no effect — reset via the Grafana UI, or remove the volume with `docker volume rm docker_grafana_data` to re-seed from env vars.

Metrics are produced by `prometheus-fastapi-instrumentator` which is installed into the unified API (`backend/unified_api/main.py`), the job service (`backend/job_service/main.py`), the blogging service (`backend/blogging_service/entrypoint.py`), and the generic team entrypoint (`backend/team_service/entrypoint.py`). That means every team container automatically exposes `/metrics` without any per-team code changes. Dropping additional dashboard JSON files into `docker/grafana/provisioning/dashboards/` picks them up automatically every 30 seconds.

Add a new team? Edit `docker/prometheus/prometheus.yml` and append a new target entry to the `team-services` job with the service's DNS name and port, then add a matching `extra_hosts` entry to the `prometheus` service in `docker-compose.yml`.

## Verification

After starting the stack:

1. **Compose up** – `docker compose -f docker/docker-compose.yml --env-file docker/.env up -d --build` should bring up all services without errors.
2. **Temporal UI** – Open http://localhost:8080 and confirm the Temporal Web UI loads.
3. **Agents** – `curl http://localhost:8888/health` should return `{"status":"ok"}` (agents use stack Postgres and Ollama Cloud when configured).
4. **Logs API** – With `ENABLE_LOG_API=1` in `.env`, `curl "http://localhost:8888/api/software-engineering/logs?service=sw_api&lines=100"` should return 200 and log content. With `ENABLE_LOG_API` unset, the same URL should return 404.
5. **Metrics endpoints** – `curl -sf http://localhost:8888/metrics | head` and the same on `:8585` (job service) and `:8090`–`:8110` (team services) should return Prometheus text-format output (`# HELP ...`).
6. **Prometheus targets** – Open http://localhost:9090/targets; all rows should be green (`UP`). Or run `curl -s 'http://localhost:9090/api/v1/query?query=up' | jq '.data.result[] | {service:.metric.service, up:.value[1]}'`.
7. **Grafana datasource** – `curl -sf -u admin:admin http://localhost:3000/api/datasources | jq` should list one `Prometheus` datasource. Then open http://localhost:3000 → Dashboards → Strands → **Strands FastAPI Overview** and confirm the panels render live data after generating some traffic (e.g. `for i in {1..20}; do curl -sf http://localhost:8888/health > /dev/null; done`).

## Security

- Do not commit `.env` with real secrets. Use `.env.example` as a template only.
- For production, do not expose Temporal or Postgres to the public internet; keep them on internal networks.
- Leave **ENABLE_LOG_API** unset or 0 in production so the logs endpoint is disabled.

## Strands platform

This package is part of the [Strands Agents](../README.md) monorepo (Unified API, Angular UI, and full team index).
