# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Khala** is a multi-agent orchestration platform that simulates autonomous software development teams and specialized business functions. It currently mounts **21 enabled agent "teams"** (software engineering, blogging, personal assistant, market research, SOC2 compliance, social marketing, branding, agent provisioning, accessibility audit, AI systems, investment, nutrition & meal planning, planning v3, coding team, sales, road trip planning, agentic team provisioning, startup advisor, user agent founder, deepthought, job matching) under a single Unified FastAPI app, with an Angular 19 frontend. The authoritative team list lives in `backend/unified_api/config.py` (`TEAM_CONFIGS`).

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
    job_matching_team/       # Scans open roles vs a job-seeker profile; ranks best-to-apply
    llm_service/             # Centralized LLM client (Ollama, Claude)
    agent_registry/          # Agent Console catalog: loads per-agent YAML manifests, serves /api/agents
    agent_console/           # Agent Console data layer: Postgres-backed saved inputs, run history, diff, pruner
    product_delivery/        # Persistent Product Delivery Loop, in-process module mounted by
                             # unified_api at /api/product-delivery. Backlog tables
                             # (products → initiatives → epics → stories → tasks + acceptance criteria
                             # + feedback_items), ProductOwnerAgent (WSJF/RICE); sprints + releases
                             # tables, sprint_planner_agent, _load_requirements_from_sprint in the SE
                             # orchestrator; release_manager_agent ships sprint completion →
                             # plan/releases/<version>.md + product_delivery_releases row, auto-promotes
                             # Integration-phase failures into sprint-tagged feedback_items (queryable
                             # via GET /feedback; operator triage feeds the next groom — POST /groom
                             # does not consume feedback automatically); POST/GET /releases routes;
                             # SE Integration-phase hook (legacy SE path only — default
                             # use_coding_team=True path currently skips the hook).
    shared_agent_invoke/     # Invoke shim mounted inside the sandbox image; exposes POST /_agents/{id}/invoke
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
# Start Postgres + the central job service (every team's JobServiceClient
# requires JOB_SERVICE_URL — there is no longer a file-backed fallback).
cp docker/.env.example docker/.env              # once, if not done
docker compose -f docker/docker-compose.yml up -d postgres job-service

# Export the vars every shared_postgres caller reads
export POSTGRES_HOST=localhost
export POSTGRES_PORT=5432
export POSTGRES_USER=postgres
export POSTGRES_PASSWORD=postgres
export POSTGRES_DB=postgres

# Required by every team for job tracking
export JOB_SERVICE_URL=http://localhost:8085

# Now `make run`, `uvicorn <team>.api.main:app`, etc. all work.
# `pytest` does not need JOB_SERVICE_URL exported — backend/conftest.py
# spins up the job service in-process for the test session.
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
npm run test:coverage # 90% line coverage target
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

The **Agent Console** (UI at `/agent-console`) is the single entry point for discovering, inspecting, and running every specialist agent in the system. Three tabs: **Catalog** (searchable card grid + anatomy drawer), **Runner** (isolated invocation in an ephemeral sandbox), and **Provisioning & Environments** (embeds `AgentProvisioningDashboardComponent`).

- **Catalog / registry** — `backend/agents/agent_registry/` loads declarative per-agent YAML manifests from `backend/agents/<team_dir>/agent_console/manifests/*.yaml` and serves them at `/api/agents`. Manifest schema, field rules, route list, and golden-sample generation: `backend/agents/agent_registry/README.md`.
- **Runner + sandboxes** — each invocation runs the agent in its own ephemeral docker compose stack (the unified `khala-agent-sandbox` image plus a sandbox-internal Postgres, Temporal, Prometheus, and Grafana); the unified API proxies `POST /api/agents/{id}/invoke`. Stack contents, lifecycle, and idle teardown: `backend/agents/agent_provisioning_team/sandbox/README.md`. Agents tagged `requires-live-integration` are catalogued but not runnable.
- **Runs, saved inputs, diff** — `backend/agents/agent_console/` is the Postgres-backed data layer (run history, user-saved inputs, unified-diff) plus a background pruner; the Runner UI adds a Form/JSON editor, a saved-inputs picker, and a history panel. Tables, routes, and env vars: `backend/agents/agent_console/README.md`.

The old `/agent-provisioning` route redirects to `/agent-console` for backward compatibility.

### Product Delivery Loop

The `product_delivery` team (`backend/agents/product_delivery/`, mounted at `/api/product-delivery`) wraps the SE 4-phase pipeline in a persistent loop: backlog → groom (`ProductOwnerAgent`) → sprint plan (`SprintPlannerAgent`) → SE run with `{sprint_id}` → release hook (`ReleaseManagerAgent`) → feedback → next groom. The release hook (`_maybe_ship_sprint_release`) fires only on the legacy SE path today; the default `use_coding_team=True` path completes before reaching it. See `docs/ARCHITECTURE.md` §11 ("Product Delivery Loop") for the sequence diagram, runtime contracts, and known limitations.

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

One-line index below. The full reference — defaults, backoff math, fallback semantics, and edge cases — lives in [`docs/ENV_VARS.md`](docs/ENV_VARS.md). All numeric vars parse defensively: garbage → documented default, out-of-range → clamped to the documented floor/ceiling unless noted.

| Variable | Purpose |
|---|---|
| `OLLAMA_API_KEY` | Required for Ollama Cloud API |
| `LLM_PROVIDER` | LLM provider selection |
| `LLM_BASE_URL` | LLM server URL |
| `LLM_MODEL` | Model name |
| `LLM_NUM_CTX_FALLBACK_TTL_S` | TTL (default `300`s) for the Ollama client's provisional `num_ctx` fallback after an `/api/show` miss, so a transient outage can't permanently truncate prompts. [details](docs/ENV_VARS.md#llm-client-and-thinking) |
| `LLM_MAX_RETRIES` / `LLM_BACKOFF_BASE` / `LLM_BACKOFF_MAX` | Transient (5xx/conn/timeout) retry schedule for the central Ollama client (defaults `10`/`2`s/`120`s; 429s use `LLM_RATE_LIMIT_*`). [details](docs/ENV_VARS.md#llm-client-and-thinking) |
| `LLM_ENABLE_THINKING` | Global thinking default for calls that don't set `think` (default on; registered models think at their max level). [details](docs/ENV_VARS.md#llm-client-and-thinking) |
| `LLM_THINKING_LEVEL` | Overrides the thinking level for models with registered levels (e.g. `medium`); invalid → max with a warning. [details](docs/ENV_VARS.md#llm-client-and-thinking) |
| `LLM_THINKING_DOWNGRADE_RETRY` | Empty 200 responses get one immediate proof-of-change retry at reduced thinking, then fail hard with a `semantic_exhaustion` receipt (default on; falsy restores legacy transient retries). [details](docs/ENV_VARS.md#llm-client-and-thinking) |
| `LLM_RATE_LIMIT_MAX_RETRIES` / `LLM_RATE_LIMIT_BACKOFF_INITIAL` / `LLM_RATE_LIMIT_BACKOFF_MAX` | Slow backoff schedule for HTTP 429 rate limits (defaults `5` retries, `300`s→`3600`s), independent of the transient schedule. [details](docs/ENV_VARS.md#llm-rate-limits) |
| `LLM_RATE_LIMIT_HONOR_RETRY_AFTER` | When on (default), honor an integer-seconds `Retry-After` on a 429 additively — never below the configured floor. [details](docs/ENV_VARS.md#llm-rate-limits) |
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
| `POSTGRES_HOST` (and `POSTGRES_PORT`/`USER`/`PASSWORD`/`DB`) | Required for migrated teams; enables Postgres-backed stores via `shared_postgres`; no SQLite fallback. [details](docs/ENV_VARS.md#shared-infrastructure-and-storage) |
| `ARCHITECT_MODEL_SPECIALIST` / `ARCHITECT_MODEL_ORCHESTRATOR` | Per-role model overrides for the AI Systems team |
| `ALPHA_VANTAGE_API_KEY` / `FRED_API_KEY` | Market data providers used by the Investment Strategy Lab |
| `INVESTMENT_MARKET_DATA_CACHE_ROOT` | Override for the on-disk root of the Investment Team's market-data cache (falls back to `${AGENT_CACHE}/investment_team/market_data`, then a tempdir). [details](docs/ENV_VARS.md#investment-and-market-data) |
| `MARKET_DATA_FETCH_WORKERS` | Worker count for multi-symbol market-data fetch (default `min(len(symbols), 16)`). [details](docs/ENV_VARS.md#investment-and-market-data) |
| `STRATEGY_LAB_LLM_*` | LLM fault-tolerance envelope for every Strategy Lab call: per-call `_TIMEOUT`, `_MAX_RETRIES`, transient `_BACKOFF_BASE`/`_BACKOFF_MAX`, 429 `_RATE_LIMIT_BACKOFF_INITIAL`/`_RATE_LIMIT_BACKOFF_MAX`, and cumulative `_TOTAL_BUDGET` (each falls back to the matching `LLM_*`). [full reference](backend/agents/investment_team/strategy_lab/README.md) |
| `STRATEGY_LAB_DESIGN_*` / `STRATEGY_LAB_MECHANICAL_REPAIR_ENABLED` | Design ↔ review loop tuning: `_DESIGN_REVIEW_ROUNDS`/`_DESIGN_REVIEW_STALL_ROUNDS`, mechanical-repair pre-flight toggle, `_DESIGN_SELF_REVIEW_ENABLED`/`_DESIGN_SELF_REVISION_ROUNDS`, `_DESIGN_PARSE_RETRIES`/`_REFINEMENT_PARSE_RETRIES`, `_STRUCTURED_OUTPUT_ENABLED`, and the per-cycle `_DESIGN_MAX_LLM_CALLS` cap. [full reference](backend/agents/investment_team/strategy_lab/README.md) |
| `STRATEGY_LAB_*` (sizing, alignment, universe) | Gates & data tuning: `_SIZING_COHERENCE_TOLERANCE`, `_CODE_CONFORMANCE_RETRIES`, `_ALIGNMENT_RETRIES`/`_ALIGNMENT_ADJUDICATION_CONCURRENCY`, `_MAX_UNIVERSE_SYMBOLS`, `_MARKET_DATA_*`. Full knob reference (defaults, backoff math, edge cases): [`strategy_lab/README.md`](backend/agents/investment_team/strategy_lab/README.md). |
| `AGENT_INVOKE_MAX_PAYLOAD_BYTES` | Hard cap on invoke request body (default 1 MiB; overflow → 413 without spinning up a sandbox). [details](docs/ENV_VARS.md#agent-console-and-invoke) |
| `AGENT_INVOKE_MAX_OUTPUT_BYTES` | Hard cap on agent response body; oversized → truncated with `truncated: true` (default 1 MiB). [details](docs/ENV_VARS.md#agent-console-and-invoke) |
| `AGENT_EXEC_TIMEOUT_S` | Default per-agent execution timeout in the sandbox (default `60`; overflow → 504). [details](docs/ENV_VARS.md#agent-console-and-invoke) |
| `AGENT_COGNITION_REFLECTION_SUMMARY_LIMIT` | Most-recent summaries per scale fed to the reflection engine (default `6`). [details](docs/ENV_VARS.md#agent-cognition-and-knowledge-graph) |
| `AGENT_COGNITION_REFLECTION_MAX_PROPOSALS` | Cap on `pending` rule proposals per `reflect` run (default `5`). [details](docs/ENV_VARS.md#agent-cognition-and-knowledge-graph) |
| `AGENT_COGNITION_REFLECTION_INPUT_CHARS` | Character budget for the reflection LLM input block (default `8000`; uses the `cognition` model). [details](docs/ENV_VARS.md#agent-cognition-and-knowledge-graph) |
| `GITHUB_TOKEN` | Default token for the coding team's `run-from-github` flow; per-request `github_token` overrides. Needs Issues/PRs/Contents read-write + Metadata read. [details](docs/ENV_VARS.md#coding-team-and-github) |
| `GITHUB_API_URL` | Optional override for the GitHub REST base URL (default `https://api.github.com`; set for GH Enterprise). [details](docs/ENV_VARS.md#coding-team-and-github) |
| `GITHUB_DEPENDENCY_CONCURRENCY` | Semaphore width for per-issue `blocked_by` dependency fetches enriching the issue picker (default `8`). [details](docs/ENV_VARS.md#coding-team-and-github) |
| `GIT_COMMIT_USER_NAME` | Author/committer name for platform git commits (default `Khala`; native `GIT_AUTHOR_*`/`GIT_COMMITTER_*` win). [details](docs/ENV_VARS.md#se-ci-gate-and-git-identity) |
| `GIT_COMMIT_USER_EMAIL` | Author/committer email for platform git commits (default `brandon.kindred@gmail.com`). [details](docs/ENV_VARS.md#se-ci-gate-and-git-identity) |
| `SE_CI_GATE_ENABLED` | Master toggle for CI gate verification after code generation (default `true`) |
| `SE_CI_GATE_TIMEOUT_S` | Timeout (s) for GitHub CI status polling when a remote is available (default `300`) |
| `SE_CI_GATE_LOCAL_FALLBACK` | When `true` (default), run CI checks locally via subprocess when no GitHub remote is available |
| `CODING_TEAM_REVIEW_RETRIES` | Retries for the Tech Lead `run_code_review` LLM call on transient failure (default `2` → 3 attempts). [details](docs/ENV_VARS.md#coding-team-and-github) |
| `CODING_TEAM_ANSWER_WAIT_TIMEOUT_S` | Wall-clock cap the human-in-the-loop decision gate blocks for answers (default `3600`s; timeout → fail closed). [details](docs/ENV_VARS.md#coding-team-and-github) |
| `NEO4J_BOLT_URL` | Bolt URL of the Neo4j server backing the Graphiti knowledge-graph layer; also the layer's enablement gate. [details](docs/ENV_VARS.md#agent-cognition-and-knowledge-graph) |
| `NEO4J_USER` / `NEO4J_PASSWORD` / `NEO4J_DATABASE` | Neo4j credentials/database (defaults `neo4j` / empty / `neo4j`). Change the password before any non-local deployment |
| `GRAPHITI_LLM_MODEL` | Model Graphiti uses for entity/edge extraction (defaults to the resolved `cognition` model). [details](docs/ENV_VARS.md#agent-cognition-and-knowledge-graph) |
| `GRAPHITI_EMBED_MODEL` / `GRAPHITI_EMBED_DIM` | Embedding model + dimensionality for Graphiti hybrid search (defaults `nomic-embed-text` / `768`). [details](docs/ENV_VARS.md#agent-cognition-and-knowledge-graph) |
| `AGENT_COGNITION_GRAPH_SYNC_INTERVAL_S` / `AGENT_COGNITION_GRAPH_SYNC_BATCH` | Cadence (default `300`s) and batch size (default `50`) of the background graph sync worker. [details](docs/ENV_VARS.md#agent-cognition-and-knowledge-graph) |
| `NEO4J_SLOW_OP_MS` | Slow-call log threshold (ms, default `1000`) for `shared_neo4j.timed_graph_op` |
| `AGENT_COGNITION_SCHEDULER_INTERVAL_S` | Cadence (default `3600`s, floored `60`) of the cognition scheduler (rollups → reflect → prune; never activates rules). [details](docs/ENV_VARS.md#agent-cognition-and-knowledge-graph) |
| `AGENT_COGNITION_GRAPH_SEARCH_TOP_K` | Max related facts per knowledge-graph search in `build_graph_context` (default `10`), scoped to the agent's `group_id` |
| `AUTHOR_PROFILE_PATH` | Path to author profile YAML injected into blogging prompts (falls back to `$AGENT_CACHE/author_profile.yaml`, then the bundled example) |
| `AUTHOR_PROFILE_STRICT` | When `true`, missing/invalid profile raises instead of falling back to the bundled example. Recommended for production |
| `JOB_SEEKER_PROFILE_PATH` | Path to the job-matching job-seeker profile YAML (falls back to `$AGENT_CACHE/job_seeker_profile.yaml`, then the bundled example) |
| `JOB_SEEKER_PROFILE_STRICT` | When `true`, a missing/invalid job-seeker profile raises instead of falling back to the bundled example |
| `JOB_MATCHING_SERVICE_URL` | Upstream URL the unified API proxies `/api/job-matching/*` to (the team reuses `OLLAMA_API_KEY` for web search) |
| `SOCIAL_MARKETING_WINNING_POSTS_TOP_K` | Max exemplars retrieved from the social marketing Winning Posts Bank per concept run (default `5`) |
| `SOCIAL_MARKETING_WINNING_POSTS_RERANK_ENABLED` | Enable LLM rerank stage in the Winning Posts Bank retrieval (default `true`; set to `false` to disable) |
| `SOCIAL_MARKETING_WINNING_POSTS_INGEST_THRESHOLD` | Engagement-score cutoff (0..1) above which performance observations are auto-promoted into the Winning Posts Bank (default `0.7`) |

**Blogging pipeline:** `research → planning (ContentPlan) → writer → gates`; `POST /research-and-review` runs research + the same planning step. See `backend/agents/blogging/README.md` and repo `CHANGELOG.md`.

**Google browser login (shared):** **`GET/PUT/DELETE /api/integrations/google-browser-login`** stores one Fernet-encrypted Gmail/Google email+password for **any** integration that signs in with Google via Playwright in **Postgres only** (`encrypted_integration_credentials` when `POSTGRES_HOST` is set, e.g. Docker). **Not available** without Postgres (credentials are never stored in SQLite). Code: `unified_api/google_browser_login_credentials.py` — reuse for new integrations when the site uses “Sign in with Google”.

**Medium.com integration:** **Medium statistics** need **`storage_state`** on disk (`INTEGRATIONS_BROWSER_SESSION_ROOT`). With provider **Google**, Playwright uses the **shared** credentials above; **`POST /api/integrations/medium/session/browser-login`** captures the session; the stats resolver **auto-logs in** if the session file is missing. Optional **Google OAuth client** in the UI is only for `GET /api/integrations/medium/oauth/google/connect`.

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
