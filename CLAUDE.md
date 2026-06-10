# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Khala** is a multi-agent orchestration platform that simulates autonomous software development teams and specialized business functions. It currently mounts **21 enabled agent "teams"** (software engineering, blogging, personal assistant, market research, SOC2 compliance, social marketing, branding, agent provisioning, accessibility audit, AI systems, investment, nutrition & meal planning, planning v3, coding team, sales, road trip planning, agentic team provisioning, startup advisor, user agent founder, deepthought, job matching) under a single Unified FastAPI app, with an Angular 19 frontend. The authoritative team list lives in `backend/unified_api/config.py` (`TEAM_CONFIGS`).

## Repository Structure

One directory per agent team under `backend/agents/` — the authoritative team list is `TEAM_CONFIGS` in `backend/unified_api/config.py`. Load-bearing entries:

```
backend/
  agents/
    software_engineering_team/  # Primary team — full dev pipeline; contains the backend/frontend
                                # code-v2, devops, planning-v2, integration, and QA sub-teams
    coding_team/             # Standalone /api/coding-team; SE uses it as a logical sub-team
    planning_v3_team/        # Client-facing discovery/PRD team (/api/planning-v3)
    product_delivery/        # Persistent Product Delivery Loop (/api/product-delivery)
    llm_service/             # Centralized LLM client (Ollama, Claude)
    agent_registry/          # Agent Console catalog: per-agent YAML manifests → /api/agents
    agent_console/           # Agent Console data layer: runs, saved inputs, diff
    shared_postgres/         # \
    shared_temporal/         #  > Shared infra (see Architecture below)
    shared_agent_invoke/     # /
    integrations/            # Shared integration contracts (Google login, Medium, etc.)
    artifact_registry/       # Shared artifact persistence
    event_bus/               # Cross-team event publishing
    api/                     # Legacy blog API surface (see blogging/ for current pipeline)
  unified_api/               # Single-entry-point FastAPI server (port 8080);
                             # config.py = TEAM_CONFIGS + security gateway + Temporal settings
  run_unified_api.py         # CLI launcher
  Makefile                   # Primary build/run targets
  pyproject.toml             # Ruff config (line-length 120, Python 3.10 target)
docker/                      # Full-stack compose: Postgres, Temporal, Ollama, Agents, UI
user-interface/              # Angular 19 frontend (src/app: components/, models/, services/)
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

Migrated teams (blogging, branding, startup_advisor, team_assistant, user_agent_founder, agentic_team_provisioning, nutrition_meal_planning, unified_api credentials) require Postgres for local dev and tests — no SQLite fallback. Every team's `JobServiceClient` requires `JOB_SERVICE_URL` (no file-backed fallback), except under `pytest`, where `backend/conftest.py` spins up the job service in-process.

```bash
cp docker/.env.example docker/.env              # once, if not done
docker compose -f docker/docker-compose.yml up -d postgres job-service

export POSTGRES_HOST=localhost
export POSTGRES_PORT=5432
export POSTGRES_USER=postgres
export POSTGRES_PASSWORD=postgres
export POSTGRES_DB=postgres
export JOB_SERVICE_URL=http://localhost:8085
```

Pool sizing and slow-query logging knobs (`POSTGRES_POOL_MIN_SIZE`/`POSTGRES_POOL_MAX_SIZE`, `POSTGRES_SLOW_QUERY_MS`): see `docs/ENV_VARS.md`.

### Frontend

```bash
cd user-interface
nvm use               # Node 22 (.nvmrc)
npm ci
npm start             # Dev server at localhost:4200
npm run build         # Production build
npm test              # Vitest (requires Chrome)
npm run test:coverage # 90% line coverage target
```

### Docker (Full Stack)

```bash
cp docker/.env.example docker/.env   # Then set OLLAMA_API_KEY
docker compose -f docker/docker-compose.yml --env-file docker/.env up --build
# Ports: UI=4200, Agents=8888, Temporal UI=8080, Ollama=11434
```

### Docker Volumes

All agent containers share one `agents_data` named volume mounted at `/data/agents` (`AGENT_CACHE`); teams namespace artifacts under `{team_name}/`, so job state, caches, profiles, and workspaces persist across restarts. Blogging paths and SE workspaces (`SE_WORKSPACE_DIR`) also point into this volume.

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

The **Agent Console** (UI at `/agent-console`; the old `/agent-provisioning` route redirects there) is the single entry point for discovering, inspecting, and running every specialist agent. Three parts, each with its own docs:

- **Catalog** — `backend/agents/agent_registry/` serves per-agent YAML manifests at `/api/agents`. Manifest schema and routes: `backend/agents/agent_registry/README.md`.
- **Runner** — each invocation runs in an ephemeral docker compose sandbox, proxied via `POST /api/agents/{id}/invoke`. Stack contents and lifecycle: `backend/agents/agent_provisioning_team/sandbox/README.md`.
- **Runs, saved inputs, diff** — `backend/agents/agent_console/` is the Postgres-backed data layer. Tables, routes, and env vars: `backend/agents/agent_console/README.md`.

### Product Delivery Loop

The `product_delivery` team (`backend/agents/product_delivery/`, mounted at `/api/product-delivery`) wraps the SE 4-phase pipeline in a persistent loop: backlog → groom → sprint plan → SE run → release hook → feedback → next groom. Sequence diagram, runtime contracts, and known limitations (including which SE path fires the release hook): `docs/ARCHITECTURE.md` §11 ("Product Delivery Loop").

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

### Project Rules

- **Never reference GitHub issues in code, comments, or docs.** Do not mention issue numbers (e.g. `#NNN`, `Issue #NNN`), issue URLs, or "see issue X" anywhere in source code, comments, docstrings, commit messages, changelogs, or documentation. Describe the change on its own terms — what it does and why — without pointing at an external tracker. This rule applies to *new* writing; existing references in this file and historical docs are grandfathered until the surrounding section is rewritten.
- **Always use `Closes #N` notation in pull requests.** Every PR must reference the associated GitHub issue in its body using GitHub's auto-close keywords (`Closes #N`, `Fixes #N`, or `Resolves #N`) so merging the PR automatically closes the linked issue. This is the *only* place issue numbers belong — PR bodies — and it is required, not optional. If a change has no associated issue, open one first.
- **Design by Contract (DbC) is mandatory for all code and comments.** Every function, method, and module must make its contract explicit:
  - **Preconditions** — what callers must guarantee about inputs (types, ranges, invariants, required state). Enforce with `assert` or explicit validation that raises on violation at boundaries.
  - **Postconditions** — what the function guarantees about its return value and observable side effects when preconditions hold.
  - **Invariants** — properties of a class/module that hold before and after every public operation.
  - Document the contract in the docstring under explicit `Preconditions:`, `Postconditions:`, and (where relevant) `Invariants:` sections. Comments that are not part of a contract should still respect the existing "only write a comment when the WHY is non-obvious" rule.
  - Violations are bugs in the *caller* (precondition) or *callee* (postcondition/invariant) — never silently coerce, never `try`/`except` around a contract failure to hide it.

## Key Environment Variables

Core vars only. The complete reference — every var, defaults, backoff math, fallback semantics, edge cases — lives in [`docs/ENV_VARS.md`](docs/ENV_VARS.md); team-specific tuning knobs (Strategy Lab, agent cognition, blogging, social marketing, etc.) are also covered in the owning team's README. All numeric vars parse defensively: garbage → documented default, out-of-range → clamped to the documented floor/ceiling unless noted.

| Variable | Purpose |
|---|---|
| `OLLAMA_API_KEY` | Required for Ollama Cloud API |
| `LLM_PROVIDER` / `LLM_BASE_URL` / `LLM_MODEL` | LLM provider selection, server URL, model name |
| `POSTGRES_HOST` (+ `_PORT`/`_USER`/`_PASSWORD`/`_DB`) | Required for migrated teams; enables Postgres-backed stores via `shared_postgres`; no SQLite fallback |
| `JOB_SERVICE_URL` | Central job service; required by every team's `JobServiceClient` |
| `TEMPORAL_ADDRESS` (+ `TEMPORAL_NAMESPACE`/`TEMPORAL_TASK_QUEUE`) | Enables Temporal mode when set |
| `AGENT_CACHE` | Shared cache root for all teams (Docker: `/data/agents`); each team namespaces under `{team_name}/` |
| `SE_WORKSPACE_DIR` | Root for software-engineering team per-job workspaces |
| `SECURITY_GATEWAY_ENABLED` | Security gateway toggle (default: true) |
| `UNIFIED_API_PORT` / `UNIFIED_API_HOST` | Bind address/port for the Unified API (default `0.0.0.0:8080`) |
| `GITHUB_TOKEN` | Token for the coding team's `run-from-github` flow (Issues/PRs/Contents read-write + Metadata read) |

**Blogging pipeline:** `research → planning (ContentPlan) → writer → gates`. See `backend/agents/blogging/README.md`.

**Google browser login (shared):** `GET/PUT/DELETE /api/integrations/google-browser-login` stores one Fernet-encrypted Google credential (Postgres only — never SQLite) for any Playwright integration that signs in with Google. Reuse `unified_api/google_browser_login_credentials.py` for new "Sign in with Google" integrations. Medium uses this flow; details in `backend/agents/blogging/README.md`.

## Testing

- **Coverage requirement: tests must cover at least 90% of code (line coverage) on both backend and frontend.** This is a hard floor for new and modified code; CI enforces it. If a file or branch cannot reach 90%, document the reason explicitly in the PR and add a targeted `# pragma: no cover` (Python) or `/* istanbul ignore next */` (TypeScript) with a one-line justification — do not lower the global threshold.
- **Backend**: `pytest` with `pytest-cov` — CI runs per-team test suites (SE, blogging, market research, SOC2, social marketing, investment, planning v3, sales, deepthought, etc.) and fails the build below 90% line coverage.
- **Frontend**: Vitest + Angular testing utilities; **90% line coverage target** for `src/app`.
- **CI**: GitHub Actions — ruff lint must pass first, then parallel test jobs (coverage-gated at 90%), then docker smoke test.

## Reference Docs

- `docs/ENV_VARS.md` — complete environment-variable reference (every var, defaults, backoff math, edge cases)
- `backend/agents/agent_provisioning_team/AGENT_ANATOMY.md` — Required structure for AI agents (Input/Output, Tools, Memory, Prompts, Security Guardrails, Subagents); diagrams in `design_assets/`
- `ARCHITECTURE.md` — detailed architecture with Mermaid diagrams (12 sections, including the Product Delivery Loop)
- `backend/agents/software_engineering_team/README.md` — 31KB SE team deep dive
- `docker/README.md` — Full-stack setup, ports, env vars, security
- `user-interface/README.md` — UI setup and API configuration
