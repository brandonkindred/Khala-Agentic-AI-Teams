# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Strands Agents** is a multi-agent orchestration platform that simulates autonomous software development teams and specialized business functions. It consists of 13+ agent "teams" (software engineering, blogging, market research, SOC2 compliance, etc.), each exposed as a FastAPI service, unified under a single Unified API with an Angular 19 frontend.

## Repository Structure

```
backend/
  agents/                    # All agent team implementations
    software_engineering_team/  # Primary team — full dev pipeline
    blogging/                # Blog content pipeline
    llm_service/             # Centralized LLM client (Ollama, Claude)
    integrations/            # Shared integration contracts
    api/                     # Legacy blog API surface (see blogging/ for current pipeline)
  unified_api/               # Single-entry-point FastAPI server (port 8080)
    config.py                # Team routing + Temporal settings
    main.py                  # App with team route mounting
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

### Software Engineering Team Pipeline (4 phases)

1. **Discovery**: Spec → LLM parsing → Planning-v2 (6-phase workflow) → planning_v2_adapter
2. **Design**: Tech Lead generates task assignments; Architecture Expert produces architecture docs
3. **Execution** (parallel queues):
   - Prefix queue: git/DevOps setup (sequential)
   - Backend worker: processes backend tasks one at a time
   - Frontend worker: processes frontend tasks one at a time
4. **Integration**: Integration agent → DevOps trigger → security pass → doc update → merge

**Per-task backend pipeline**: Feature branch → planning → code generation → write files → lint → build → code review → acceptance verifier → security review → QA → DbC → Tech Lead review → doc update → merge

**Planning cache**: Short-circuits Design phase when spec, architecture, and project_overview are unchanged.

### Sub-Team Variants

- **Backend-Code-V2** (`backend_code_v2_team/`): 3-layer (Backend Tech Lead → Backend Dev Agent + 7 tool agents)
- **Frontend-Code-V2** (`frontend_code_v2_team/`): 3-layer (Frontend Tech Lead → Frontend Dev Agent + 12 tool agents)
- **DevOps Team**: 5-phase (Intake → Change Design → Write Artifacts → Validation → Completion)

### Unified API Routing

All teams mount under `/api/{team-slug}`. Team configs are defined in `backend/unified_api/config.py`. The security gateway (`SECURITY_GATEWAY_ENABLED=true` by default) sits in front of all routes.

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

**Blogging pipeline:** `research → planning (ContentPlan) → writer → gates`; `POST /research-and-review` runs research + the same planning step. See `backend/agents/blogging/README.md` and repo `CHANGELOG.md`.

**Google browser login (shared):** **`GET/PUT/DELETE /api/integrations/google-browser-login`** stores one Fernet-encrypted Gmail/Google email+password for **any** integration that signs in with Google via Playwright in **Postgres only** (`encrypted_integration_credentials` when `POSTGRES_HOST` is set, e.g. Docker). **Not available** without Postgres (credentials are never stored in SQLite). Code: `unified_api/google_browser_login_credentials.py` — reuse for new integrations when the site uses “Sign in with Google”.

**Medium.com integration:** **Medium statistics** need **`storage_state`** on disk (`INTEGRATIONS_BROWSER_SESSION_ROOT`). With provider **Google**, Playwright uses the **shared** credentials above; **`POST /api/integrations/medium/session/browser-login`** captures the session; the stats resolver **auto-logs in** if the session file is missing. Optional **Google OAuth client** in the UI is only for `GET /api/integrations/medium/oauth/google/connect`.

## Testing

- **Backend**: `pytest` — CI runs per-team test suites (SE, blogging, market research, SOC2, social marketing)
- **Frontend**: Vitest + Angular testing utilities; 80% line coverage target for `src/app`
- **CI**: GitHub Actions — ruff lint must pass first, then parallel test jobs, then docker smoke test

## Reference Docs

- `backend/agents/agent_provisioning_team/AGENT_ANATOMY.md` — Required structure for AI agents (Input/Output, Tools, Memory, Prompts, Security Guardrails, Subagents); diagrams in `design_assets/`
- `ARCHITECTURE.md` — 26KB detailed architecture with Mermaid diagrams (11 sections)
- `backend/agents/software_engineering_team/README.md` — 31KB SE team deep dive
- `docker/README.md` — Full-stack setup, ports, env vars, security
- `user-interface/README.md` — UI setup and API configuration
