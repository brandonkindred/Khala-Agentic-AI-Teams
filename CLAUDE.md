# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Khala** is a multi-agent orchestration platform that simulates autonomous software development teams and specialized business functions. It currently mounts **20 enabled agent "teams"** (software engineering, blogging, personal assistant, market research, SOC2 compliance, social marketing, branding, agent provisioning, accessibility audit, AI systems, investment, nutrition & meal planning, planning v3, coding team, sales, road trip planning, agentic team provisioning, startup advisor, user agent founder, deepthought) under a single Unified FastAPI app, with an Angular 19 frontend. The authoritative team list lives in `backend/unified_api/config.py` (`TEAM_CONFIGS`).

## Repository Structure

```
backend/
  agents/                    # All agent team implementations
    software_engineering_team/  # Primary team — full dev pipeline; contains
                                # backend_code_v2_team/, frontend_code_v2_team/,
                                # devops_team/, planning_v2_team/, planning_v2_adapter.py,
                                # planning_v3_adapter.py, integration_team/, qa_agent/, etc.
    planning_v3_team/        # Client-facing discovery/PRD team (mounted at /api/planning-v3)
    coding_team/             # SE sub-team: tech lead + stack specialists (logical sub-team)
    blogging/                # Blog content pipeline
    personal_assistant_team/
    market_research_team/
    soc2_compliance_team/
    social_media_marketing_team/
    branding_team/
    agent_provisioning_team/
    accessibility_audit_team/
    ai_systems_team/
    investment_team/         # Advisor/IPS + Strategy Lab (one /api/investment prefix)
    nutrition_meal_planning_team/
    sales_team/
    road_trip_planning_team/
    agentic_team_provisioning/
    startup_advisor/
    user_agent_founder/
    deepthought/
    llm_service/             # Centralized LLM client (Ollama, Claude)
    agent_registry/          # Agent Console catalog: loads per-agent YAML manifests, serves /api/agents
    agent_sandbox/           # Warm per-team Docker sandbox lifecycle for the Agent Console Runner
    agent_console/           # Agent Console Phase 3: Postgres-backed saved inputs, run history, diff, pruner
    shared_agent_invoke/     # One-line-mount invoke shim each team's api/main.py includes
    integrations/            # Shared integration contracts (Google login, Medium, etc.)
    artifact_registry/       # Shared artifact persistence
    event_bus/               # Cross-team event publishing
    shared_temporal/         # Temporal worker/workflow plumbing
    api/                     # Legacy blog API surface (see blogging/ for current pipeline)
  unified_api/               # Single-entry-point FastAPI server (port 8080)
    config.py                # TEAM_CONFIGS, security gateway, Temporal settings
    main.py                  # App with team route mounting + security gateway
  run_unified_api.py         # CLI launcher
  Makefile                   # Primary build/run targets
  requirements.txt           # Top-level Python deps
  pyproject.toml             # Ruff config (line-length 120, Python 3.10 target)
docker/
  docker-compose.yml         # Full stack: Postgres, Temporal, Ollama, Agents, UI
  .env.example               # Template for OLLAMA_API_KEY, LLM settings
user-interface/              # Angular 19 frontend
  src/app/
    components/              # Feature + shared components
    models/                  # TypeScript request/response models
    services/                # API client services
```

## Common Commands

### Backend

```bash
cd backend

make install          # Create venv, install deps
make install-dev      # + pytest, ruff
make lint             # ruff check + format check
make lint-fix         # ruff --fix + format
make test             # pytest (agents + unified_api)
make run              # Start Unified API (0.0.0.0:8080, reload enabled)
make deploy           # Production: 4 workers

# Direct run
python run_unified_api.py
python run_unified_api.py --port 9000 --reload --workers 4
```

### Local dev with Postgres

All migrated teams (blogging, branding, startup_advisor, team_assistant,
user_agent_founder, agentic_team_provisioning, nutrition_meal_planning,
unified_api credentials) now require Postgres for local dev and tests —
no SQLite fallback.

```bash
# Start Postgres from the full stack compose file (or the tiny subset)
cp docker/.env.example docker/.env              # once, if not done
docker compose -f docker/docker-compose.yml up -d postgres

# Export the vars every shared_postgres caller reads
export POSTGRES_HOST=localhost
export POSTGRES_PORT=5432
export POSTGRES_USER=postgres
export POSTGRES_PASSWORD=postgres
export POSTGRES_DB=postgres

# Now `pytest`, `uvicorn <team>.api.main:app`, etc. all work
```

Pool sizing is controlled by `POSTGRES_POOL_MIN_SIZE` (default 2) and
`POSTGRES_POOL_MAX_SIZE` (default 10); slow-query logging threshold is
`POSTGRES_SLOW_QUERY_MS` (default 100).

### Frontend

```bash
cd user-interface
nvm use               # Node 22 (.nvmrc)
npm ci
npm start             # Dev server at localhost:4200
npm run build         # Production build
npm test              # Vitest (requires Chrome)
npm run test:coverage # 80% line coverage target
```

### Docker (Full Stack)

```bash
cp docker/.env.example docker/.env   # Then set OLLAMA_API_KEY
docker compose -f docker/docker-compose.yml --env-file docker/.env up --build
# Ports: UI=4200, Agents=8888, Temporal UI=8080, Ollama=11434
```

### Docker Volumes

All agent team containers share a single `agents_data` named volume mounted at `/data/agents`. Every service sets `AGENT_CACHE=/data/agents`, so all team artifacts (job state, caches, profiles, workspaces) persist across container restarts. Teams naturally namespace via `{team_name}/` subdirectories under `AGENT_CACHE`. Blogging-specific paths (`BLOGGING_RUN_ARTIFACTS_ROOT`, `BLOGGING_MEDIUM_STATS_ROOT`, `INTEGRATIONS_BROWSER_SESSION_ROOT`) and SE workspaces (`SE_WORKSPACE_DIR`) also point into this volume.

## Architecture

### Execution Model

Each agent team has a **team-lead orchestrator** that coordinates role-separated specialist agents via Pydantic request/response models. There are two runtime modes:

- **Thread mode** (default, local dev): agents run as Python threads
- **Temporal mode** (when `TEMPORAL_ADDRESS` is set): durable workflow execution using Temporal 1.24.2 — state survives server restarts

### Shared infrastructure modules

- **`backend/agents/shared_temporal/`** — Temporal client + per-team worker registry. Teams export `WORKFLOWS`/`ACTIVITIES` from `<team>/temporal/__init__.py`; workers start on import (Pattern A).
- **`backend/agents/shared_postgres/`** — Postgres schema registry. Each team exports a `SCHEMA: TeamSchema` constant from `<team>/postgres/__init__.py` (pure data, no side effects), and the team's FastAPI lifespan calls `register_team_schemas(SCHEMA)` at startup (Pattern B). No-op when `POSTGRES_HOST` is unset. See `backend/agents/shared_postgres/README.md`.

### Software Engineering Team Pipeline (4 phases)

1. **Discovery**: Spec → LLM parsing → Planning (Planning-v2 6-phase workflow via `planning_v2_adapter.py`, or the newer `planning_v3_adapter.py` which delegates to the standalone `planning_v3_team`)
2. **Design**: Tech Lead generates task assignments; Architecture Expert produces architecture docs
3. **Execution** (parallel queues):
   - Prefix queue: git/DevOps setup (sequential)
   - Backend worker: processes backend tasks one at a time
   - Frontend worker: processes frontend tasks one at a time
4. **Integration**: Integration agent → DevOps trigger → security pass → doc update → merge

**Per-task backend pipeline**: Feature branch → planning → code generation → write files → lint → build → code review → acceptance verifier → security review → QA → DbC → Tech Lead review → doc update → merge

**Planning cache**: Short-circuits Design phase when spec, architecture, and project_overview are unchanged.

### Sub-Team Variants

All three live **inside** `backend/agents/software_engineering_team/`:

- **Backend-Code-V2** (`software_engineering_team/backend_code_v2_team/`): 3-layer (Backend Tech Lead → Backend Dev Agent + tool agents for linting, build, code review, security, QA, DbC, git ops)
- **Frontend-Code-V2** (`software_engineering_team/frontend_code_v2_team/`): 3-layer (Frontend Tech Lead → Frontend Dev Agent + tool agents)
- **DevOps Team** (`software_engineering_team/devops_team/`): 5-phase (Intake → Change Design → Write Artifacts → Validation → Completion)
- **Planning V2** (`software_engineering_team/planning_v2_team/`): legacy 6-phase planning, still supported via `planning_v2_adapter.py`
- **Coding Team** (`backend/agents/coding_team/`): standalone module mounted at `/api/coding-team` and used by SE as a logical sub-team (`parent_team_key="software_engineering"`)
- **Planning V3** (`backend/agents/planning_v3_team/`): standalone client-facing discovery/PRD team mounted at `/api/planning-v3`; SE invokes it through `planning_v3_adapter.py`

### Unified API Routing

All teams mount under `/api/{team-slug}`. Team configs are defined in `backend/unified_api/config.py`. The security gateway (`SECURITY_GATEWAY_ENABLED=true` by default) sits in front of all routes.

### Agent Console & Agent Registry

The **Agent Console** (UI at `/agent-console`, replaces the old `/agent-provisioning`) is the single entry point for discovering, inspecting, and (in later phases) running every specialist agent in the system. It has three tabs:

- **Catalog** — browsable/searchable card grid of every agent, with a drawer showing full anatomy metadata.
- **Runner** — placeholder; isolated agent invocation ships in Phase 2.
- **Provisioning & Environments** — embeds the existing `AgentProvisioningDashboardComponent` unchanged.

The catalog is backed by `backend/agents/agent_registry/`, which loads declarative per-agent YAML manifests from `backend/agents/<team_dir>/agent_console/manifests/*.yaml` and exposes them via `/api/agents` (router lives in `backend/unified_api/routes/agents.py`). Manifests describe each agent's id, team, summary, I/O schema refs, invoke metadata, and sandbox provisioning hints. See `backend/agents/agent_registry/README.md` for the authoring guide.

**Phase 2 — Runner + sandboxes (shipped):** the Runner tab invokes a single specialist agent in a warm per-team Docker sandbox. The lifecycle service lives in `backend/agents/agent_sandbox/` and drives `docker/sandbox.compose.yml` (dedicated `sandbox-postgres`, isolated `khala-sandbox` network, ports 8200–8220). Each team's `api/main.py` mounts `shared_agent_invoke.mount_invoke_shim(app, team_key="...")` which exposes `POST /_agents/{id}/invoke`; the unified API proxies via `POST /api/agents/{id}/invoke`. Idle sandboxes are reaped after `SANDBOX_IDLE_TEARDOWN_MINUTES` (default 15). Golden sample inputs are generated from `inputs.schema_ref` via `python3 -m agent_registry.scripts.generate_sample_skeletons`. Four teams are wired in Phase 2: blogging, software_engineering, planning_v3, branding. Agents with the `requires-live-integration` tag (e.g. `blogging.publication`) are catalogued but not runnable in sandboxes — the Runner's Run button is disabled with an explainer.

**Phase 3 — Runs, saved inputs, diff, form editor (shipped):** `backend/agents/agent_console/` is the Postgres-backed data layer (via `shared_postgres`) for two tables — `agent_console_saved_inputs` (user-curated payloads) and `agent_console_runs` (one row per invocation, best-effort persisted from the invoke proxy). New routes: `GET/POST/PUT/DELETE /api/agents/{id}/saved-inputs`, `GET/DELETE /api/agents/runs/{id}`, `GET /api/agents/{id}/runs`, `POST /api/agents/diff`. Every row is tagged with an `author` handle derived from the shared `AuthorProfile` so we can migrate to real auth without re-keying data. A background pruner started from the unified API lifespan trims runs to the newest `AGENT_CONSOLE_RUNS_RETENTION` (default 200) per agent every `AGENT_CONSOLE_PRUNE_INTERVAL_S` (default 3600s). The Runner UI gains a Form/JSON editor toggle (tiered renderer with JSON fallback for unions/deep nesting), a saved-inputs picker group, a history panel with compare/delete actions, and an "editing as JSON" chip where the form bails out. Diff endpoint returns a unified-diff string of pretty-printed, sorted-key JSON; UI colour-codes lines client-side with no additional library.

The old `/agent-provisioning` route redirects to `/agent-console` for backward compatibility.

### LLM Integration

`backend/agents/llm_service/` provides a unified client that supports:
- **Ollama** (local inference or Cloud API via `OLLAMA_API_KEY`) — including thinking mode
- **Claude** (via httpx direct calls)

Environment variables for LLM: `LLM_PROVIDER`, `LLM_BASE_URL`, `LLM_MODEL`

## Code Style

- **Python**: Ruff, line-length 120, Python 3.10 target. Pre-commit hooks enforce this.
- Ignored rules: E501, N802/N806, B904, SIM108
- Known first-party modules: `shared`, `backend_agent`, `frontend_team`, `devops_agent`, `qa_agent`
- Per-file ignores exist for tests and `agent_implementations/`
- **TypeScript**: Angular style; SCSS for styling

## Key Environment Variables

| Variable | Purpose |
|---|---|
| `OLLAMA_API_KEY` | Required for Ollama Cloud API |
| `LLM_PROVIDER` | LLM provider selection |
| `LLM_BASE_URL` | LLM server URL |
| `LLM_MODEL` | Model name |
| `TEMPORAL_ADDRESS` | Enables Temporal mode when set |
| `TEMPORAL_NAMESPACE` | Temporal namespace |
| `TEMPORAL_TASK_QUEUE` | Temporal task queue name |
| `SECURITY_GATEWAY_ENABLED` | Security gateway toggle (default: true) |
| `ENABLE_LOG_API` | Exposes HTTP log endpoint |
| `BLOGGING_RUN_ARTIFACTS_ROOT` | Optional root for pipeline run artifacts (default: `{tempdir}/blogging_runs`; Docker sets `/data/blogging/runs`) |
| `BLOGGING_MEDIUM_STATS_ROOT` | Optional base dir for Medium stats job `work_dir` (default: `{AGENT_CACHE}/blogging_team/medium_stats_runs`) |
| `MEDIUM_GOOGLE_REDIRECT_URI` | Optional; fixed OAuth redirect for Medium’s Google identity link (`…/api/integrations/medium/oauth/google/callback`) when the API is behind a proxy |
| `BLOG_PLANNING_MAX_ITERATIONS` | Blog planning refine loop cap (default 5) |
| `BLOG_PLANNING_MAX_PARSE_RETRIES` | JSON parse/repair attempts per planning LLM call (default 3) |
| `BLOG_PLANNING_MODEL` | Optional Ollama model name for **planning only** (same base URL as `LLM_*`) |
| `INTEGRATIONS_BROWSER_SESSION_ROOT` | Root for Playwright `storage_state` files used by browser-based integrations (Medium, etc.); Docker maps to the shared `agents_data` volume |
| `SE_WORKSPACE_DIR` | Root for software-engineering team per-job workspaces |
| `AGENT_CACHE` | Shared cache root for all teams (Docker: `/data/agents`); each team namespaces under `{team_name}/` |
| `UNIFIED_API_PORT` / `UNIFIED_API_HOST` | Bind address/port for the Unified API (default `0.0.0.0:8080`) |
| `POSTGRES_HOST` (and `POSTGRES_PORT`/`USER`/`PASSWORD`/`DB`) | Required for migrated teams (blogging, branding, team_assistant, startup_advisor, user_agent_founder, agentic_team_provisioning, unified_api credentials). Enables Postgres-backed stores via `shared_postgres`; no SQLite fallback |
| `ARCHITECT_MODEL_SPECIALIST` / `ARCHITECT_MODEL_ORCHESTRATOR` | Per-role model overrides for the AI Systems team |
| `ALPHA_VANTAGE_API_KEY` / `FRED_API_KEY` | Market data providers used by the Investment Strategy Lab |
| `STRATEGY_LAB_MARKET_DATA_*` | Strategy Lab market-data cache/timeout/provider tuning |
| `AUTHOR_PROFILE_PATH` | Path to user/author profile YAML injected into blogging prompts. Falls back to `$AGENT_CACHE/author_profile.yaml`, then to the bundled example. See `backend/agents/blogging/author_profile/`. |
| `AUTHOR_PROFILE_STRICT` | When `true`, missing/invalid profile raises instead of falling back to the bundled example. Recommended for production. |
| `SOCIAL_MARKETING_WINNING_POSTS_TOP_K` | Max exemplars retrieved from the social marketing Winning Posts Bank per concept run (default `5`). |
| `SOCIAL_MARKETING_WINNING_POSTS_RERANK_ENABLED` | Enable LLM rerank stage in the Winning Posts Bank retrieval (default `true`; set to `false` to disable). |
| `SOCIAL_MARKETING_WINNING_POSTS_INGEST_THRESHOLD` | Engagement-score cutoff (0..1) above which performance observations are auto-promoted into the Winning Posts Bank (default `0.7`). |

**Blogging pipeline:** `research → planning (ContentPlan) → writer → gates`; `POST /research-and-review` runs research + the same planning step. See `backend/agents/blogging/README.md` and repo `CHANGELOG.md`.

**Google browser login (shared):** **`GET/PUT/DELETE /api/integrations/google-browser-login`** stores one Fernet-encrypted Gmail/Google email+password for **any** integration that signs in with Google via Playwright in **Postgres only** (`encrypted_integration_credentials` when `POSTGRES_HOST` is set, e.g. Docker). **Not available** without Postgres (credentials are never stored in SQLite). Code: `unified_api/google_browser_login_credentials.py` — reuse for new integrations when the site uses “Sign in with Google”.

**Medium.com integration:** **Medium statistics** need **`storage_state`** on disk (`INTEGRATIONS_BROWSER_SESSION_ROOT`). With provider **Google**, Playwright uses the **shared** credentials above; **`POST /api/integrations/medium/session/browser-login`** captures the session; the stats resolver **auto-logs in** if the session file is missing. Optional **Google OAuth client** in the UI is only for `GET /api/integrations/medium/oauth/google/connect`.

## Testing

- **Backend**: `pytest` — CI runs per-team test suites (SE, blogging, market research, SOC2, social marketing, investment, planning v3, sales, deepthought, etc.)
- **Frontend**: Vitest + Angular testing utilities; 80% line coverage target for `src/app`
- **CI**: GitHub Actions — ruff lint must pass first, then parallel test jobs, then docker smoke test

## Reference Docs

- `backend/agents/agent_provisioning_team/AGENT_ANATOMY.md` — Required structure for AI agents (Input/Output, Tools, Memory, Prompts, Security Guardrails, Subagents); diagrams in `design_assets/`
- `ARCHITECTURE.md` — 26KB detailed architecture with Mermaid diagrams (11 sections)
- `backend/agents/software_engineering_team/README.md` — 31KB SE team deep dive
- `docker/README.md` — Full-stack setup, ports, env vars, security
- `user-interface/README.md` — UI setup and API configuration
