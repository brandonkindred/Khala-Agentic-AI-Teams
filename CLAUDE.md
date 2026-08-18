# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository. **This file is a map, not the territory** — a high-level orientation that links out to the detailed docs. For operational depth (full command reference, every env var, per-team internals), follow the links.

## Project Overview

**Khala** is a multi-agent orchestration platform that simulates autonomous software development teams and specialized business functions. It mounts **23 enabled agent "teams"** (software engineering, blogging, personal assistant, market research, SOC2 compliance, social marketing, branding, agent provisioning, accessibility audit, AI systems, investment, planning, coding team, sales, road trip planning, agentic team provisioning, startup advisor, user agent founder, deepthought, job matching, user profile, product delivery, agent studio) under a single Unified FastAPI app, with an Angular 19 frontend. The authoritative team list is `TEAM_CONFIGS` in `backend/unified_api/config.py`.

## Repository Structure

One directory per agent team under `backend/agents/`, with platform infra as a
sibling package under `backend/shared/`. Load-bearing entries:

```
backend/
  shared/                   # Platform infra (postgres, temporal, agent_invoke, …) →
                             # import shared.<name> (see Architecture below)
  agents/
    software_engineering_team/  # Primary team — full dev pipeline; contains the backend/frontend
                                # code-v2, devops, coding-team execution engine (Tech Lead +
                                # Task Graph, routed at /api/coding-team), planning, integration,
                                # and QA sub-teams
    planning_team/           # Client-facing discovery/PRD team (/api/planning)
    product_delivery/        # Persistent Product Delivery Loop (/api/product-delivery)
    llm_service/             # Centralized LLM client (Ollama, Claude, RunPod)
    agent_platform/          # In-process platform: registry, console, sandbox, Studio authoring
    agent_cognition/         # Memory, rules, and tools substrate for generated agents
    agent_team_studio/       # Infra + domain apps: provisioning, agentic compose, personas
    artifact_registry/       # Shared artifact persistence
    event_bus/               # Cross-team event publishing
    api/                     # Legacy blog API surface (see blogging/ for current pipeline)
  unified_api/               # Single-entry-point FastAPI server; config.py = TEAM_CONFIGS +
                             # security gateway + Temporal settings
  run_unified_api.py         # CLI launcher
  Makefile                   # Primary build/run targets
docker/                      # Full-stack compose: Postgres, Temporal, Ollama, Agents, UI
user-interface/              # Angular 19 frontend (src/app: components/, models/, services/)
```

## Architecture

Concise summary below; full detail with Mermaid diagrams lives in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

- **Execution model** — each team has a team-lead orchestrator coordinating role-separated specialist agents via Pydantic request/response models. Two runtime modes: **thread mode** (default, local dev; agents run as Python threads) and **Temporal mode** (durable workflows when `TEMPORAL_ADDRESS` is set — state survives server restarts).
- **Shared infra patterns** — Temporal client + per-team worker registry (`backend/shared/temporal/`, **Pattern A**: teams export `WORKFLOWS`/`ACTIVITIES` from `<team>/temporal/__init__.py`, workers start on import) and a Postgres schema registry ([`shared.postgres/README.md`](backend/shared/postgres/README.md), **Pattern B**: teams export a `SCHEMA` constant, the FastAPI lifespan calls `register_team_schemas`; no-op when `POSTGRES_HOST` is unset).
- **Software Engineering team** — 4-phase pipeline (Discovery → Design → parallel Execution → Integration) with a per-task backend pipeline (feature branch → plan → codegen → lint/build → code review → security → QA → DbC → Tech Lead review → merge; lint/build is a single CI-owned gate, not re-run inside code review). Sub-team variants (backend/frontend code-v2, devops, coding_team) live inside `software_engineering_team/`; standalone Planning lives in `planning_team/`. Deep dive: [`software_engineering_team/README.md`](backend/agents/software_engineering_team/README.md).
- **Unified API routing** — all teams mount under `/api/{team-slug}`; configs in `backend/unified_api/config.py`; the security gateway (`SECURITY_GATEWAY_ENABLED=true` by default) fronts all routes. Lifespan worker/route registration (Postgres schemas, proxy catch-alls, assistant mounts, sandbox/Studio/console/cognition workers, and the import-time `include_router` list) is catalogued in [`docs/UNIFIED_API_LIFESPAN.md`](docs/UNIFIED_API_LIFESPAN.md). Platform workers that share process-local singletons boot from that lifespan, not Pattern A import-time start.
- **Agent Console & Registry** — single entry point (UI `/agent-console`) for discovering, inspecting, and running specialist agents. Catalog: [`agent_platform/registry/README.md`](backend/agents/agent_platform/registry/README.md); ephemeral sandbox runner: [`agent_platform/sandbox/README.md`](backend/agents/agent_platform/sandbox/README.md); runs/inputs/diff data layer: [`agent_platform/console/README.md`](backend/agents/agent_platform/console/README.md); conversational single-agent authoring (Agent Studio) and its `AgentDefinition` view-model ↔ `AgentManifest` SoT field mapping: [`agent_platform/studio/README.md`](backend/agents/agent_platform/studio/README.md). Generated and Studio manifests are built through one construction API: [`shared.manifests/README.md`](backend/shared/manifests/README.md).
- **Product Delivery Loop** — `product_delivery/` (`/api/product-delivery`) wraps the SE pipeline in a persistent backlog → groom → sprint plan → run → release → feedback loop. Sequence diagram and contracts: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) §11.
- **LLM resolution (behavior-defining)** — `llm_service/` is a unified client for Ollama and Claude (default model `claude-opus-4-8`). The **Postgres-backed ordered provider list is the SOLE source of LLM resolution** (`/llm-config` UI → `llm_provider_configs` table); each entry carries its own provider/model/base URL and its own Fernet-encrypted API key. `get_client` selects the most-preferred entry that isn't usage-limited; a `FailoverLLMClient` fails over to the next entry on a 429. When the list is empty (or `POSTGRES_HOST` unset) and the provider isn't `dummy`, `get_client` raises `LLMNotConfiguredError` — there is **no legacy single-provider env fallback**. The only override is `LLM_PROVIDER=dummy` (the no-LLM test/dev harness). `LLM_MODEL`/`LLM_BASE_URL` supply blank-entry defaults only. Tuning vars: [`docs/ENV_VARS.md`](docs/ENV_VARS.md).

## Build & Run

Essential entry points below; full command reference in `backend/Makefile`, [`docker/README.md`](docker/README.md), and [`user-interface/README.md`](user-interface/README.md).

```bash
# Backend (from backend/)
make install-dev      # venv + deps + pytest, ruff
make lint / lint-fix  # ruff check+format
make test             # pytest (agents + unified_api)
make run              # Unified API on 0.0.0.0:8080 (reload)

# Frontend (from user-interface/; Node 22 via `nvm use`)
npm ci && npm start   # dev server at localhost:4200
npm test              # Vitest (requires Chrome)

# Full stack (needs docker/.env with OLLAMA_API_KEY)
docker compose -f docker/docker-compose.yml --env-file docker/.env up --build
```

**Local dev with Postgres:** migrated teams require Postgres (no SQLite fallback), and every `JobServiceClient` requires `JOB_SERVICE_URL` (except under `pytest`, where `backend/conftest.py` spins the job service up in-process). Bring up `postgres` + `job-service` from the compose file and export the `POSTGRES_*` / `JOB_SERVICE_URL` vars; step-by-step in [`docs/ENV_VARS.md`](docs/ENV_VARS.md) and [`docker/README.md`](docker/README.md).

**Docker volumes:** all agent containers share one `agents_data` volume mounted at `/data/agents` (`AGENT_CACHE`); teams namespace artifacts under `{team_name}/`, so job state, caches, and workspaces persist across restarts.

## Code Style

- **Python**: Ruff, line-length 120, Python 3.10 target. Pre-commit hooks enforce this. Ignored rules: E501, N802/N806, B904, SIM108. Per-file ignores exist for tests and `agent_implementations/`.
- **TypeScript**: Angular style; SCSS for styling.

## Project Rules

- **Never reference GitHub issues in code, comments, or docs.** Do not mention issue numbers (e.g. `#NNN`, `Issue #NNN`), issue URLs, or "see issue X" anywhere in source code, comments, docstrings, commit messages, changelogs, or documentation. Describe the change on its own terms — what it does and why — without pointing at an external tracker. This rule applies to *new* writing; existing references in this file and historical docs are grandfathered until the surrounding section is rewritten.
- **Always use `Closes #N` notation in pull requests.** Every PR must reference the associated GitHub issue in its body using GitHub's auto-close keywords (`Closes #N`, `Fixes #N`, or `Resolves #N`) so merging the PR automatically closes the linked issue. This is the *only* place issue numbers belong — PR bodies — and it is required, not optional. If a change has no associated issue, open one first.
- **Design by Contract (DbC) is mandatory for all code and comments.** Every function, method, and module must make its contract explicit:
  - **Preconditions** — what callers must guarantee about inputs (types, ranges, invariants, required state). Enforce with `assert` or explicit validation that raises on violation at boundaries.
  - **Postconditions** — what the function guarantees about its return value and observable side effects when preconditions hold.
  - **Invariants** — properties of a class/module that hold before and after every public operation.
  - Document the contract in the docstring under explicit `Preconditions:`, `Postconditions:`, and (where relevant) `Invariants:` sections. Comments that are not part of a contract should still respect the existing "only write a comment when the WHY is non-obvious" rule.
  - Violations are bugs in the *caller* (precondition) or *callee* (postcondition/invariant) — never silently coerce, never `try`/`except` around a contract failure to hide it.

## Testing

- **Coverage requirement: tests must cover at least 90% of code (line coverage) on both backend and frontend.** This is a hard floor for new and modified code; CI enforces it. If a file or branch cannot reach 90%, document the reason explicitly in the PR and add a targeted `# pragma: no cover` (Python) or `/* istanbul ignore next */` (TypeScript) with a one-line justification — do not lower the global threshold.
- **Backend**: `pytest` with `pytest-cov` (per-team suites). **Frontend**: Vitest + Angular testing utilities, 90% line-coverage target for `src/app`.
- **CI**: GitHub Actions — ruff lint must pass first, then parallel coverage-gated test jobs, then a docker smoke test.

## Key Environment Variables

Core, behavior-changing vars only. The complete reference (every var, defaults, backoff math, fallback semantics, edge cases) is [`docs/ENV_VARS.md`](docs/ENV_VARS.md); team-specific tuning knobs live in the owning team's README (agent cognition, SE observability, blogging, social marketing, etc.). All numeric vars parse defensively: garbage → documented default, out-of-range → clamped to the documented floor/ceiling.

| Variable | Purpose |
|---|---|
| `LLM_PROVIDER` | `dummy` selects the no-LLM test/dev harness (the only load-bearing value); otherwise the Postgres provider list is the sole source. |
| `LLM_BASE_URL` / `LLM_MODEL` | Default base URL / model for a provider-list entry whose field is blank (they no longer configure a live provider on their own). |
| `POSTGRES_HOST` (+ `_PORT`/`_USER`/`_PASSWORD`/`_DB`) | Required for migrated teams; enables Postgres-backed stores via `shared.postgres`; no SQLite fallback. |
| `JOB_SERVICE_URL` | Central job service; required by every team's `JobServiceClient`. |
| `TEMPORAL_ADDRESS` (+ `_NAMESPACE`/`_TASK_QUEUE`) | Enables Temporal mode when set. |
| `AGENT_CACHE` / `SE_WORKSPACE_DIR` | Shared cache root (Docker: `/data/agents`, namespaced per team) / root for SE per-job workspaces. |
| `SECURITY_GATEWAY_ENABLED` | Security gateway toggle (default: true). |
| `UNIFIED_API_PORT` / `UNIFIED_API_HOST` | Bind address/port for the Unified API (default `0.0.0.0:8080`). |
| `GITHUB_TOKEN` | Token for the coding team's `run-from-github` flow (Issues/PRs/Contents read-write + Metadata read). |

## Reference Docs

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — detailed architecture with Mermaid diagrams (including the Product Delivery Loop and Unified API lifespan)
- [`docs/UNIFIED_API_LIFESPAN.md`](docs/UNIFIED_API_LIFESPAN.md) — unified-API lifespan worker/route registration catalog
- [`docs/ENV_VARS.md`](docs/ENV_VARS.md) — complete environment-variable reference (defaults, backoff math, edge cases)
- [`backend/agents/software_engineering_team/README.md`](backend/agents/software_engineering_team/README.md) — SE team deep dive
- [`backend/agents/agent_team_studio/agent_provisioning_team/AGENT_ANATOMY.md`](backend/agents/agent_team_studio/agent_provisioning_team/AGENT_ANATOMY.md) — required structure for AI agents (Input/Output, Tools, Memory, Prompts, Guardrails, Subagents)
- [`docker/README.md`](docker/README.md) — full-stack setup, ports, env vars, security
- [`user-interface/README.md`](user-interface/README.md) — UI setup and API configuration
