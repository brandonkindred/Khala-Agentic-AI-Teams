# Strands Full Stack (Postgres, Temporal, Ollama, Agents)

This directory defines a **Docker Compose stack** that runs:

- **PostgreSQL 16** – shared database with `temporal` and `strands` databases (created at first run)
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

   Agent output and project data are stored in the **`agents_workspace`** Docker volume (mounted at `/workspace` in the agents container). This data persists when containers are stopped or recreated. Postgres data is stored in the **`postgres_data`** volume. To remove all persisted data, use `docker compose down -v` (the `-v` flag removes named volumes).

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
   | **Angular UI** | http://localhost:4201       (proxies /api to agents) |
   | Agents API     | http://localhost:8888       (direct) |
   | Temporal UI    | http://localhost:8080       |
   | Postgres       | localhost:5432 (user `postgres` / `temporal` / `strands`) |
   | Ollama (local) | http://localhost:11434      |

   Use the **Angular UI at 4201** so API requests go through the same origin and nginx proxies them to the backend. If you run only the API container and use the UI with `ng serve`, point the dev API base to `http://localhost:8888` in `user-interface/src/environments/environment.ts`.

## Required environment variables

- **OLLAMA_API_KEY** – Create at [ollama.com/settings/keys](https://ollama.com/settings/keys). Required for Ollama Cloud (Option A). Passed into the agents container so the LLM client can call `https://ollama.com` with `Authorization: Bearer <key>`.

Optional (defaults in compose / `docker/.env.example`; copy to `docker/.env` and set as needed):

- **SW_LLM_BASE_URL** – default is `https://ollama.com` (Ollama Cloud). Set to `http://ollama:11434` to use the local Ollama container instead.
- **SW_LLM_MODEL** – default `qwen3.5:397b-cloud`
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
| `postgres_data`   | PostgreSQL     | Database files (Temporal + app DBs). |
| `agents_workspace`| strands-agents | Agent workspace at `/workspace` (repos, generated code, artifacts). |

Data in these volumes survives `docker compose down` and container restarts. To wipe persisted data, run `docker compose down -v`.

## Port summary

| Port  | Service        |
|-------|----------------|
| 5432  | PostgreSQL     |
| 7233  | Temporal gRPC  |
| 8080  | Temporal UI    |
| 4201  | Angular UI (proxies /api to agents) |
| 8888  | Agents API (direct) |
| 11434 | Ollama (optional) |

Agents direct ports (when needed): 18000–18005 map to APIs 8000–8005.

## Agents and Postgres

When running in this stack, the **strands-agents** service uses the **stack’s Postgres** (database `strands`, user `strands`) via **POSTGRES_HOST=postgres**. The container does not start its own PostgreSQL. The init script in `docker/postgres/init/` creates the `strands` database and user on first run.

## Verification

After starting the stack:

1. **Compose up** – `docker compose -f docker/docker-compose.yml --env-file docker/.env up -d --build` should bring up all services without errors.
2. **Temporal UI** – Open http://localhost:8080 and confirm the Temporal Web UI loads.
3. **Agents** – `curl http://localhost:8888/health` should return `{"status":"ok"}` (agents use stack Postgres and Ollama Cloud when configured).
4. **Logs API** – With `ENABLE_LOG_API=1` in `.env`, `curl "http://localhost:8888/api/software-engineering/logs?service=sw_api&lines=100"` should return 200 and log content. With `ENABLE_LOG_API` unset, the same URL should return 404.

## Security

- Do not commit `.env` with real secrets. Use `.env.example` as a template only.
- For production, do not expose Temporal or Postgres to the public internet; keep them on internal networks.
- Leave **ENABLE_LOG_API** unset or 0 in production so the logs endpoint is disabled.
