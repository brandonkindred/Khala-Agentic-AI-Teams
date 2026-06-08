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
    agent_console/           # Agent Console Phase 3: Postgres-backed saved inputs, run history, diff, pruner
    product_delivery/        # Persistent Product Delivery Loop (#243), in-process module mounted by
                             # unified_api at /api/product-delivery. Phase 1 (#369): backlog tables
                             # (products → initiatives → epics → stories → tasks + acceptance criteria
                             # + feedback_items), ProductOwnerAgent (WSJF/RICE). Phase 2 (#396): sprints
                             # + releases tables, sprint_planner_agent, _load_requirements_from_sprint
                             # in the SE orchestrator. Phase 3 (#371): release_manager_agent ships sprint
                             # completion → plan/releases/<version>.md + product_delivery_releases row,
                             # auto-promotes Integration-phase failures into sprint-tagged
                             # feedback_items (queryable via GET /feedback; operator triage feeds the
                             # next groom — POST /groom does not consume feedback automatically);
                             # POST/GET /releases routes; SE Integration-phase hook (legacy SE path
                             # only — default use_coding_team=True path currently skips the hook).
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

The **Agent Console** (UI at `/agent-console`, replaces the old `/agent-provisioning`) is the single entry point for discovering, inspecting, and (in later phases) running every specialist agent in the system. It has three tabs:

- **Catalog** — browsable/searchable card grid of every agent, with a drawer showing full anatomy metadata.
- **Runner** — placeholder; isolated agent invocation ships in Phase 2.
- **Provisioning & Environments** — embeds the existing `AgentProvisioningDashboardComponent` unchanged.

The catalog is backed by `backend/agents/agent_registry/`, which loads declarative per-agent YAML manifests from `backend/agents/<team_dir>/agent_console/manifests/*.yaml` and exposes them via `/api/agents` (router lives in `backend/unified_api/routes/agents.py`). Manifests describe each agent's id, team, summary, I/O schema refs, invoke metadata, and sandbox provisioning hints. See `backend/agents/agent_registry/README.md` for the authoring guide.

**Runner + sandboxes (shipped):** the Runner tab invokes a single specialist agent in a per-sandbox **docker compose stack** containing the unified `khala-agent-sandbox` image (`backend/agent_sandbox_image/`, `backend/agent_sandbox_runtime/`) plus a sandbox-internal Postgres, Temporal, Prometheus, and Grafana — every backing service runs *inside* the sandbox so the agent is fully testable as if live, and nothing in the stack joins the long-lived `khala-stack` compose network (#456). The lifecycle lives in `backend/agents/agent_provisioning_team/sandbox/`. The agent container is pinned to a single `SANDBOX_AGENT_ID`; its runtime mounts `shared_agent_invoke.mount_invoke_shim(app)` to expose `POST /_agents/{id}/invoke`; the unified API proxies via `POST /api/agents/{id}/invoke`. Idle sandbox stacks are torn down (volumes and all, via `docker compose down -v`) after `AGENT_PROVISIONING_SANDBOX_IDLE_MINUTES` (default 5). Permission tiers were removed in #456; every sandbox is provisioned with full access on every backing service. Golden sample inputs are generated from `inputs.schema_ref` via `python3 -m agent_registry.scripts.generate_sample_skeletons`. Agents with the `requires-live-integration` tag (e.g. `blogging.publication`) are catalogued but not runnable in sandboxes — the Runner's Run button is disabled with an explainer. See `backend/agents/agent_provisioning_team/sandbox/README.md`.

**Phase 3 — Runs, saved inputs, diff, form editor (shipped):** `backend/agents/agent_console/` is the Postgres-backed data layer (via `shared_postgres`) for two tables — `agent_console_saved_inputs` (user-curated payloads) and `agent_console_runs` (one row per invocation, best-effort persisted from the invoke proxy). New routes: `GET/POST/PUT/DELETE /api/agents/{id}/saved-inputs`, `GET/DELETE /api/agents/runs/{id}`, `GET /api/agents/{id}/runs`, `POST /api/agents/diff`. Every row is tagged with an `author` handle derived from the shared `AuthorProfile` so we can migrate to real auth without re-keying data. A background pruner started from the unified API lifespan trims runs to the newest `AGENT_CONSOLE_RUNS_RETENTION` (default 200) per agent every `AGENT_CONSOLE_PRUNE_INTERVAL_S` (default 3600s). The Runner UI gains a Form/JSON editor toggle (tiered renderer with JSON fallback for unions/deep nesting), a saved-inputs picker group, a history panel with compare/delete actions, and an "editing as JSON" chip where the form bails out. Diff endpoint returns a unified-diff string of pretty-printed, sorted-key JSON; UI colour-codes lines client-side with no additional library.

The old `/agent-provisioning` route redirects to `/agent-console` for backward compatibility.

### Product Delivery Loop

The `product_delivery` team (`backend/agents/product_delivery/`, mounted at `/api/product-delivery`) wraps the SE 4-phase pipeline in a persistent loop: backlog → grooming (`ProductOwnerAgent`, WSJF/RICE) → sprint planning (`SprintPlannerAgent`, capacity-aware) → SE run with `{sprint_id}` (orchestrator hydrates requirements directly via `_load_requirements_from_sprint`) → Integration-phase release hook (`ReleaseManagerAgent` writes `plan/releases/<version>.md` and a `product_delivery_releases` row, promotes Integration-phase failures into sprint-tagged `feedback_items`) → next groom. The release hook (`_maybe_ship_sprint_release`) fires on the legacy SE path today; the default `use_coding_team=True` path completes before reaching it. See `ARCHITECTURE.md` §11 ("Product Delivery Loop") for the sequence diagram, known limitations, and runtime contracts.

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

- **Never reference GitHub issues in code, comments, or docs.** Do not mention issue numbers (e.g. `#243`, `#369`, `Issue #531`), issue URLs, or "see issue X" anywhere in source code, comments, docstrings, commit messages, changelogs, or documentation. Describe the change on its own terms — what it does and why — without pointing at an external tracker. This rule applies to *new* writing; existing references in this file and historical docs are grandfathered until the surrounding section is rewritten.
- **Always use `Closes #N` notation in pull requests.** Every PR must reference the associated GitHub issue in its body using GitHub's auto-close keywords (`Closes #N`, `Fixes #N`, or `Resolves #N`) so merging the PR automatically closes the linked issue. This is the *only* place issue numbers belong — PR bodies — and it is required, not optional. If a change has no associated issue, open one first.
- **Design by Contract (DbC) is mandatory for all code and comments.** Every function, method, and module must make its contract explicit:
  - **Preconditions** — what callers must guarantee about inputs (types, ranges, invariants, required state). Enforce with `assert` or explicit validation that raises on violation at boundaries.
  - **Postconditions** — what the function guarantees about its return value and observable side effects when preconditions hold.
  - **Invariants** — properties of a class/module that hold before and after every public operation.
  - Document the contract in the docstring under explicit `Preconditions:`, `Postconditions:`, and (where relevant) `Invariants:` sections. Comments that are not part of a contract should still respect the existing "only write a comment when the WHY is non-obvious" rule.
  - Violations are bugs in the *caller* (precondition) or *callee* (postcondition/invariant) — never silently coerce, never `try`/`except` around a contract failure to hide it.

## Key Environment Variables

| Variable | Purpose |
|---|---|
| `OLLAMA_API_KEY` | Required for Ollama Cloud API |
| `LLM_PROVIDER` | LLM provider selection |
| `LLM_BASE_URL` | LLM server URL |
| `LLM_MODEL` | Model name |
| `LLM_NUM_CTX_FALLBACK_TTL_S` | TTL (seconds, default `300`) for the Ollama client's provisional `num_ctx` fallback. When a model's context size is not in `KNOWN_MODEL_CONTEXT` / `LLM_CONTEXT_SIZE` and `/api/show` fails, the client degrades to a 16384-token context but only caches it for this window before re-attempting — a transient `/api/show` outage can no longer poison the process into silently truncating large prompts for its whole lifetime. A successfully-resolved (or known/env) context size is still cached permanently. Garbage values fall back to the default; negative floors to `0` (retry on next call). |
| `LLM_MAX_RETRIES` / `LLM_BACKOFF_BASE` / `LLM_BACKOFF_MAX` | **Transient** (5xx / connection / timeout) retry schedule for the central Ollama client — defaults `10` / `2`s / `120`s. These no longer govern HTTP 429 rate limits (see the `LLM_RATE_LIMIT_*` row). Garbage values fall back to the defaults. |
| `LLM_RATE_LIMIT_MAX_RETRIES` / `LLM_RATE_LIMIT_BACKOFF_INITIAL` / `LLM_RATE_LIMIT_BACKOFF_MAX` | Dedicated **slow** backoff schedule for HTTP **429** rate limits, applied independently of the transient schedule above. A 429 means the provider budget is exhausted and won't reset in seconds, so the first retry waits `LLM_RATE_LIMIT_BACKOFF_INITIAL` seconds (default `300`), doubling with additive jitter up to `LLM_RATE_LIMIT_BACKOFF_MAX` (default `3600`), for `LLM_RATE_LIMIT_MAX_RETRIES` retries (default `5` → 6 attempts, worst-case ~2h15m of waiting) before raising `LLMRateLimitError`. The 429 backoff `time.sleep` runs **after** the concurrency semaphore and HTTP stream are released (never while holding them); a 429 retry never consumes a transient attempt and vice-versa. The shared schedule lives in `llm_service/backoff.py` and is reused by the Strategy Lab envelope. All three parse defensively (garbage → default). |
| `LLM_RATE_LIMIT_HONOR_RETRY_AFTER` | When truthy (default on; `false`/`0`/`no` disables), the central Ollama client honors an integer-seconds `Retry-After` header on a 429 as `min(max(computed_backoff, Retry-After), cap)` — additive-only, so it can never shorten the configured floor. Only the integer-seconds form is honored (HTTP-date / non-numeric / non-positive are ignored). Strands models (Strategy Lab) have no HTTP-level access to the header, so this applies only to the central client. |
| `LLM_ENABLE_THINKING` | Global thinking default for all LLM calls that don't specify `think` explicitly (default enabled; set `false`/`0`/`no` to disable). When enabled, models registered in `KNOWN_MODEL_THINKING_LEVELS` (e.g. `deepseek-v4-pro:cloud`: low/medium/high/max) think at their **highest** level; unregistered models get boolean `think: true`. Explicit per-call `think=False` always wins. |
| `LLM_THINKING_LEVEL` | Overrides the thinking level chosen for models with registered levels (e.g. `medium`). Values that aren't a registered level for the model fall back to the max level with a warning; ignored for models that only support boolean think. |
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
| `STRATEGY_LAB_MAX_UNIVERSE_SYMBOLS` | Hard ceiling on the asset-class default universe used when `spec.target_symbols` is empty (default `20`). When the cap actually truncates the default a `logger.warning` fires. Non-empty `spec.target_symbols` is returned verbatim by `resolve_strategy_symbols` (override semantics) so the fetched universe matches what `TargetSymbolCoverageGate.check_trades` allows the strategy to trade. |
| `STRATEGY_LAB_ALIGNMENT_RETRIES` | Number of envelope retries for the alignment fix-proposer (`TradeAlignmentAgent.propose_code_fix`) before it raises `AlignmentAuditError` and `_run_alignment_audit` falls closed with `aligned=False` (default `2` → 3 attempts total). The retry/backoff now lives inside the shared LLM envelope (see `STRATEGY_LAB_LLM_*`), so `_run_alignment_audit` makes a single call and adds jittered backoff between attempts; an exhausted proposer or any unexpected agent exception fails closed (never a green audit). |
| `STRATEGY_LAB_ALIGNMENT_ADJUDICATION_CONCURRENCY` | Max number of near-miss LLM adjudications the `DeterministicAlignmentChecker` runs concurrently per `check()` (default `4`; sub-1 floors to `1` = fully serial; garbage values fall back to the default). Near-miss candidates are collected during the trade loop and dispatched through a bounded `ThreadPoolExecutor` (the adjudicator is synchronous) instead of blocking the loop one trade at a time; verdicts are slotted back in trade order so the output is identical to the serial path regardless of completion timing. Trades cloud concurrency for wall time without changing the gate's result. |
| `STRATEGY_LAB_LLM_TIMEOUT` | Per-call wall-clock timeout (seconds) for every Strategy Lab LLM call routed through the shared fault-tolerance envelope (`strategy_lab/agents/_llm_envelope.py`). Also forwarded as the transport-level read timeout to the underlying strands model in `get_strands_model` (the only mechanism that actually cancels a hung HTTP call; the envelope adds a secondary daemon-thread guard on top). Falls back to `LLM_TIMEOUT` / the platform default (900). |
| `STRATEGY_LAB_LLM_MAX_RETRIES` | Retries (attempts = retries + 1) the envelope makes on a *retriable* (transient transport / 5xx / connection / timeout / throttle) failure before raising `StrategyLabLLMError`. Fatal failures (4xx / auth / malformed, or a weekly rate cap) are never retried. Falls back to `LLM_MAX_RETRIES`, else `2`. Garbage values fall back. |
| `STRATEGY_LAB_LLM_BACKOFF_BASE` / `STRATEGY_LAB_LLM_BACKOFF_MAX` | Jittered exponential backoff between envelope retries for **transient** (5xx / connection / timeout) failures: `min(base**attempt + uniform(0,1), max)` seconds. Fall back to `LLM_BACKOFF_BASE` / `LLM_BACKOFF_MAX`, else `2.0` / `60.0`. HTTP 429 rate limits use the separate rate-limit schedule below. |
| `STRATEGY_LAB_LLM_RATE_LIMIT_BACKOFF_INITIAL` / `STRATEGY_LAB_LLM_RATE_LIMIT_BACKOFF_MAX` | Slow **429** rate-limit backoff for the envelope: first rate-limit retry waits `…_INITIAL` seconds (default cascade `STRATEGY_LAB_LLM_RATE_LIMIT_BACKOFF_INITIAL` → `LLM_RATE_LIMIT_BACKOFF_INITIAL` → `300`), doubling with additive jitter up to `…_MAX` (→ `LLM_RATE_LIMIT_BACKOFF_MAX` → `3600`). The cap is floored at the initial so the 300s floor always holds. A weekly-usage cap (`OLLAMA_WEEKLY_LIMIT_MESSAGE`) stays **fatal** (never retried). **Interplay:** the envelope keeps its single attempt counter (`STRATEGY_LAB_LLM_MAX_RETRIES`) and the total-budget deadline as the terminator — each rate-limit sleep is clamped to the remaining `STRATEGY_LAB_LLM_TOTAL_BUDGET` (default `(max_attempts × timeout) × 1.5` ≈ 4050s), so under defaults a 429-storm realistically gets the 300s + 600s waits before budget exhaustion. Raise `STRATEGY_LAB_LLM_MAX_RETRIES` and `STRATEGY_LAB_LLM_TOTAL_BUDGET` to ride the full schedule. There is no separate rate-limit retry count for the envelope. |
| `STRATEGY_LAB_LLM_TOTAL_BUDGET` | Hard cap (seconds) on cumulative wall time across all attempts of a single envelope call (per-call timeout and each backoff sleep are clamped to the remaining budget). On exhaustion the envelope raises `StrategyLabLLMError` with `outcome="budget_exhausted"`. Defaults to `(max_attempts × timeout) × 1.5`. |
| `STRATEGY_LAB_DESIGN_REVIEW_ROUNDS` | Cap on the design ↔ design-review loop inside `_run_design_attempt` (default `20`, sub-1 values floored to `1`, garbage values fall back to `20`). The loop runs `DesignAgent → SpecReadinessGate → DesignReviewAgent → DesignAgent.revise` until the reviewer marks the spec ready or this cap is reached; exhaustion short-circuits the cycle with `status="failed: design_not_ready"` rather than running code against a spec that never converged. |
| `STRATEGY_LAB_DESIGN_REVIEW_STALL_ROUNDS` | Within-loop stall threshold (default `3`, sub-1 values floored to `1`, garbage values fall back to `3`). A `CritiqueLedger` assigns each reviewer critique a deterministic, content-derived `issue_id` and tracks the blocking (warning/critical) open-issue set round over round. When that set is non-empty and **unchanged for this many consecutive rounds** the loop short-circuits early with `status="failed: design_stalled"` — distinct from honest round-cap exhaustion (`failed: design_not_ready`) — instead of churning to `STRATEGY_LAB_DESIGN_REVIEW_ROUNDS`. The ledger also flags **regressions** (an issue resolved on an earlier round that reappears): the reintroduced issue is surfaced to `DesignAgent.revise` as an explicit "do not reintroduce" notice (flag-and-escalate; the round is not hard-blocked). Per-cycle generation-funnel telemetry (design-review round count + stop reason, critique-ledger resolved/regressed/open totals, per-gate pass/fail histograms, compiled-vs-custom share) is emitted live on the `on_phase` callback as `"telemetry"` events and persisted on `StrategyLabRecord.loop_telemetry`; `scripts/audit_recent_runs.py` surfaces the aggregate post-hoc. |
| `STRATEGY_LAB_MECHANICAL_REPAIR_ENABLED` | Master toggle for the deterministic mechanical-repair pre-flight inside the design ↔ review loop (default `true`; accepted truthy values `true`/`1`/`yes`, case-insensitive; anything else disables). Before **every** review round (regardless of the readiness verdict), `strategy_lab/mechanical_repair.py:repair_spec` applies fully-determined, semantics-preserving fixes so they never cost an LLM `DesignAgent.revise` round; mechanical fixes re-validate and only fall through to the revise path for criticals the machine cannot fix. Scope is intentionally minimal — (1) coerce an intraday `timeframe`→`"1d"` for asset classes with no intraday data (readiness Rule 7), (2) clamp `risk_limits.max_position_pct` to the shared `MAX_POSITION_PCT_CEILING` (Rule 8) — plus a trial `compile_strategy()` that flips `requires_custom_code=True` on `CompilerError`, so even a readiness-clean spec outside the deterministic-compiler envelope (e.g. a `volatility_target` spec without an ATR predicate — readiness only *warns* on that sizing mode) selects the custom-code path during design rather than discovering it later in synthesis. Each edit is recorded on the `on_phase` callback as a `"design_repair"` event and counted in `loop_telemetry.mechanical_repairs`; substantive defects (empty entry/exit rules, thesis coherence) are left to the LLM. Disable to restore the pure LLM-revise behaviour. |
| `STRATEGY_LAB_CODE_CONFORMANCE_RETRIES` | Number of predicate-conformance gate retries before demoting criticals to warnings (default `2`, garbage values fall back to `2`). The gate runs in the synthesis loop after `CodeConformanceGate`, only for `requires_custom_code=True` strategies. Each retry feeds the per-bar diff back through `_refine_or_exhaust`; after exhaustion the pipeline proceeds to backtest with the best-effort code. |
| `STRATEGY_LAB_SIZING_COHERENCE_TOLERANCE` | Relative tolerance for `SpecReadinessGate`'s position-sizing coherence rule (default `0.05` = 5%; garbage/negative values fall back to the default). The rule uses the correct risk model — position size (`sizing.fraction` / `max_position_pct`) is capital *deployed* as a % of the account, while `stop_loss.pct` / `take_profit.pct` are price moves off entry (% of the *trade*), so the realised per-trade loss = deployed × stop and can never exceed the deployed amount. Three deterministic checks: (A) `sizing.fraction` ≤ `max_position_pct` (critical); (B) the **realised** per-trade loss (`sizing.fraction` × `stop_loss.pct`) ≤ `risk_limits.max_loss_per_trade_pct` when that tolerance is set — `max_loss_per_trade_pct` is the most a trade can lose, governed by the stop, so a position that deploys `sizing.fraction` and stops out at `stop_loss.pct` realises `fraction × stop` of the account (e.g. 10% deployed × 5% stop = 0.5%); critical when that exceeds the tolerance. Skipped when the tolerance is unset or the deployed fraction is unknown (`volatility_target`, unconfigured `fixed_notional`). A stop only caps loss for the entry side the executor fires it on (`trailing_high`→long, `trailing_low`→short, `entry_price`→both), so when the tolerance is set and deployment is known but some entry side has **no effective stop** (none declared, or only a side-incompatible one), the realised loss is unbounded — a declared limit with no mechanism to honor it — so that is its own critical (`risk_limits:loss_tolerance_no_stop`) rather than a skip; (C) a prose-stated per-trade deployment % ("deploy/allocate/risk X% per trade") reconciled against the **actual** deployed fraction (`sizing.fraction`) when known — the cap is an upper bound, not the deployed amount, so matching it alone does not satisfy the claim (warning); `volatility_target` deployment is dynamic so this check abstains. This tolerance applies **only** to the prose check (C) — the hard cap checks (A and B) are strict (a negligible float-noise epsilon only) so a real limit breach can never pass readiness. A critical routes through the synthetic-critique path and, if the reviser cannot resolve it, trips the existing `design_stalled` early-terminate. |
| `STRATEGY_LAB_DESIGN_PARSE_RETRIES` | Number of times `DesignAgent._invoke_and_parse` re-prompts the LLM when its JSON parses but fails structured-DSL validation (default `2` → 3 attempts total; `0` disables retry; garbage values fall back to `2`). The re-prompt quotes the offending field and the pydantic error so the model can self-correct one-off slips (e.g. wrapping `bar.close` in an `IndicatorRef`, or setting `source` to an indicator name). Exhaustion still raises `StrategySpecParseError` — the cycle short-circuits exactly as before. |
| `STRATEGY_LAB_DESIGN_SELF_REVIEW_ENABLED` | Toggle for the internal self-review pass inside `DesignAgent.run()` and `DesignAgent.revise()` (default `true`; accepted truthy values `true`/`1`/`yes`, case-insensitive; anything else disables). When enabled, every spec the designer emits goes through a second LLM call (`design_self_review_system.md`) that audits prose ↔ predicate completeness and risk-math coherence; if the self-review marks the spec not-ready the designer self-revises (up to `STRATEGY_LAB_DESIGN_SELF_REVISION_ROUNDS` rounds, default once) with the self-critique as feedback and then **re-audits** the revised spec through self-review before returning — so a self-revision that introduces a fresh contradiction is caught rather than reaching the external reviewer. When invoked from `revise()`, the external critique lineage and regression notice are threaded into the self-revision so prior-round fixes are not regressed. Best-effort: any self-review failure logs a warning and returns the current spec — the external `DesignReviewAgent` loop remains authoritative. Disable to restore the pre-change single-call behaviour. |
| `STRATEGY_LAB_DESIGN_SELF_REVISION_ROUNDS` | Cap on internal self-revision rounds inside `DesignAgent._with_self_review` (default `1`, sub-0 floored to `0`, garbage values fall back to `1`). Each round is one self-revision LLM call followed by a re-audit through self-review; the re-audit closes the gap where a self-revision could introduce a fresh prose↔predicate / risk-math contradiction that then reached the external `DesignReviewAgent` loop unchecked. `0` disables self-revision (audit-only — the spec is audited but never internally revised). When `_with_self_review` runs inside `revise()` the external critique lineage and regression notice are threaded into the self-revision prompt so prior-round fixes are not regressed. Each enabled round adds two charged LLM calls to the design-phase budget — the self-revision plus its re-audit (more if the self-revision hits parse-retries) — so size `STRATEGY_LAB_DESIGN_MAX_LLM_CALLS` accordingly. |
| `STRATEGY_LAB_STRUCTURED_OUTPUT_ENABLED` | Master toggle for schema-constrained (structured-output) LLM decoding in the Strategy Lab spec-authoring agents — `DesignAgent` (spec generation/revision + self-review), `DesignReviewAgent`, `RefinementAgent`, `ZeroTradeRepairAgent`, and the alignment fix-proposer (`TradeAlignmentAgent.propose_code_fix`) (default `true`; accepted truthy values `true`/`1`/`yes`, case-insensitive; anything else disables). When enabled and the provider is Ollama, each agent passes a JSON Schema derived from the structured DSL / its response model to the model's `format` field so the decoder can only emit conforming JSON — near-eliminating the malformed-JSON and schema-drift failure classes that otherwise burn LLM re-prompt rounds. Pydantic validation still runs downstream as defense-in-depth. **Ollama-only:** the Bedrock path is unaffected (keeps prompt-only JSON). Disable to restore the prior prompt-only behaviour (e.g. against a model/endpoint that does not honour `format`-as-schema). |
| `STRATEGY_LAB_DESIGN_MAX_LLM_CALLS` | Per-cycle hard cap on the total number of LLM calls the design phase may make within a single `run_cycle`, spanning all `MAX_DESIGN_REENTRIES` re-entries (default `120`, sub-1 values floored to `1`, garbage values fall back to `120`). A `LLMCallBudget` is created once per cycle and charged before every design/review LLM call (generation, each parse-retry, the self-review verdict, each self-revision, and each `DesignReviewAgent` round); when it trips the cycle short-circuits with `status="failed: budget_exhausted"` (distinct from `failed: design_not_ready`) before runaway cloud spend. **Worst-case sizing:** at default settings one design round can cost up to ~9 LLM calls — `revise` is up to 8 (3 parse-retries + 1 self-review verdict + 3 self-revision parse-retries + 1 re-audit verdict) plus 1 `DesignReviewAgent` round — so the uncapped worst case is `~9 calls × STRATEGY_LAB_DESIGN_REVIEW_ROUNDS (20) × 3 attempts ≈ 540` calls per design phase; this budget ceilings that. Raise it for genuinely hard-but-converging specs; lower it to tighten the cost/quota ceiling. |
| `INVESTMENT_MARKET_DATA_CACHE_ROOT` | Issue #376. Operator override for the on-disk root of the Investment Team's content-hashed market-data cache. Falls back to `${AGENT_CACHE}/investment_team/market_data`, then to a tempdir (with WARN — non-persistent). |
| `MARKET_DATA_FETCH_WORKERS` | Issue #376. Worker count for `MarketDataService.fetch_multi_symbol_range` and `MarketDataCache.get_or_fetch_multi`. Default `min(len(symbols), 16)`; the previous hard cap of 5 is gone. |
| `AUTHOR_PROFILE_PATH` | Path to user/author profile YAML injected into blogging prompts. Falls back to `$AGENT_CACHE/author_profile.yaml`, then to the bundled example. See `backend/agents/blogging/author_profile/`. |
| `AUTHOR_PROFILE_STRICT` | When `true`, missing/invalid profile raises instead of falling back to the bundled example. Recommended for production. |
| `JOB_SEEKER_PROFILE_PATH` | Path to the job-matching team's job-seeker profile YAML (standing search criteria). Falls back to `$AGENT_CACHE/job_seeker_profile.yaml`, then to the bundled example. See `backend/agents/job_matching_team/profile/`. |
| `JOB_SEEKER_PROFILE_STRICT` | When `true`, a missing/invalid job-seeker profile raises instead of falling back to the bundled example. |
| `JOB_MATCHING_SERVICE_URL` | Upstream URL the unified API proxies `/api/job-matching/*` to. The job-matching team also reuses `OLLAMA_API_KEY` for live web search. |
| `SOCIAL_MARKETING_WINNING_POSTS_TOP_K` | Max exemplars retrieved from the social marketing Winning Posts Bank per concept run (default `5`). |
| `SOCIAL_MARKETING_WINNING_POSTS_RERANK_ENABLED` | Enable LLM rerank stage in the Winning Posts Bank retrieval (default `true`; set to `false` to disable). |
| `SOCIAL_MARKETING_WINNING_POSTS_INGEST_THRESHOLD` | Engagement-score cutoff (0..1) above which performance observations are auto-promoted into the Winning Posts Bank (default `0.7`). |
| `AGENT_INVOKE_MAX_PAYLOAD_BYTES` | Hard cap on request body for `POST /api/agents/{id}/invoke` and the sandbox shim (default `1048576` = 1 MiB; overflow returns 413 without spinning up a sandbox). |
| `AGENT_INVOKE_MAX_OUTPUT_BYTES` | Hard cap on agent response body; oversized outputs are truncated with `truncated: true` on the envelope (default `1048576` = 1 MiB). Applies inside the shim and on the proxy's re-serialize path. |
| `AGENT_EXEC_TIMEOUT_S` | Default per-agent execution timeout (`asyncio.wait_for`) inside the sandbox; overflow returns 504 with `timeout_hit: true` (default `60`). Per-agent override via `invoke.timeout_seconds` in the manifest. |
| `AGENT_COGNITION_REFLECTION_SUMMARY_LIMIT` | Most-recent memory summaries the cognition reflection engine (`agent_cognition/rules/reflection.py`) fetches **per scale** (month/week/day) as input when proposing rule changes (default `6`; unset/garbage/non-positive falls back to the default). |
| `AGENT_COGNITION_REFLECTION_MAX_PROPOSALS` | Hard cap on the number of `pending` rule proposals reflection writes in one `reflect` run; LLM suggestions beyond the cap are ignored (default `5`; unset/garbage/non-positive falls back to the default). |
| `AGENT_COGNITION_REFLECTION_INPUT_CHARS` | Character budget passed to `compact_text` for the rendered summaries + active-rules block before the reflection LLM call (default `8000`; unset/garbage/non-positive falls back to the default). The reflection LLM uses the shared `cognition` model key, so `LLM_MODEL_cognition` overrides its model. |
| `GITHUB_TOKEN` | Default token for the coding team's `POST /api/coding-team/run-from-github` flow. Per-request `github_token` in the body overrides this. Needs `Issues: read/write`, `Pull requests: read/write`, `Contents: read/write`, `Metadata: read` (or classic `repo`). |
| `GITHUB_API_URL` | Optional override for the GitHub REST base URL used by the coding team's GitHub client (`backend/agents/coding_team/github_source/`). Defaults to `https://api.github.com`; set to a GitHub Enterprise URL when relevant. |
| `GITHUB_DEPENDENCY_CONCURRENCY` | Bounds the concurrent per-issue `blocked_by` dependency fetches that enrich `GET /api/integrations/github/issues` (the coding-team issue picker). Each open issue is annotated with the issues it depends on so the UI can flag blocked issues; the lookups fan out under a semaphore of this width (default `8`). A failed/absent lookup degrades to no dependencies for that issue and never fails the list. Garbage or non-positive values fall back to the default. |
| `GIT_COMMIT_USER_NAME` | Author/committer name for every git commit platform code makes (SE pipeline, coding team, agent git tools — all routed through `software_engineering_team/shared/git_utils.py`). Default `Khala`. Blank values fall back to the default; natively-exported `GIT_AUTHOR_*`/`GIT_COMMITTER_*` env vars win over this setting. |
| `GIT_COMMIT_USER_EMAIL` | Author/committer email for platform git commits. Default `brandon.kindred@gmail.com`. Same precedence rules as `GIT_COMMIT_USER_NAME`. |
| `SE_CI_GATE_ENABLED` | Master toggle for CI gate verification after code generation (default `true`). When enabled, the orchestrator runs lint/test/build checks on generated repos before marking jobs complete. |
| `SE_CI_GATE_TIMEOUT_S` | Timeout in seconds for GitHub CI status polling when a remote is available (default `300`). Only applies when `GITHUB_TOKEN` + repo info are provided. |
| `SE_CI_GATE_LOCAL_FALLBACK` | When `true` (default), runs CI checks locally via subprocess (ruff, pytest, npm lint/test) when no GitHub remote is available. Set to `false` to skip CI gate entirely without GitHub. |
| `CODING_TEAM_REVIEW_RETRIES` | Number of times the coding-team Tech Lead `run_code_review` LLM call is retried (with jittered exponential backoff) on a transient failure (rate limit / timeout / provider outage) before the review is flagged as an infrastructure error (default `2` → 3 attempts; blank/garbage falls back to the default; floored at 1 attempt). On exhaustion the orchestrator fails the task once with a clear diagnostic rather than re-sending the same failing prompt through the revision loop. |
| `CODING_TEAM_ANSWER_WAIT_TIMEOUT_S` | Wall-clock cap (seconds, default `3600`; garbage/non-positive falls back to the default) the coding team's human-in-the-loop decision gate blocks waiting for the user to answer escalated open questions. When the Tech Lead or a Senior SWE hits a product/design/policy/safety decision the plan does not answer, the job pauses (`status="waiting_for_user"`, `waiting_for_answers=true`, questions on `pending_questions`) and surfaces them (via `GET /status/{job_id}`, and as a GitHub issue comment on the `run-from-github` path); answers are submitted to `POST /api/coding-team/run/{job_id}/answers` (or, on the SE-driven path, the existing `POST /run-team/{job_id}/answers`), which threads the user's decisions into the plan and resumes. On timeout the job fails closed (`failed`) rather than proceeding on a guessed decision. A dead orchestrator thread (e.g. server restart) is recovered via `POST /api/coding-team/run/{job_id}/resume`. |

**Blogging pipeline:** `research → planning (ContentPlan) → writer → gates`; `POST /research-and-review` runs research + the same planning step. See `backend/agents/blogging/README.md` and repo `CHANGELOG.md`.

**Google browser login (shared):** **`GET/PUT/DELETE /api/integrations/google-browser-login`** stores one Fernet-encrypted Gmail/Google email+password for **any** integration that signs in with Google via Playwright in **Postgres only** (`encrypted_integration_credentials` when `POSTGRES_HOST` is set, e.g. Docker). **Not available** without Postgres (credentials are never stored in SQLite). Code: `unified_api/google_browser_login_credentials.py` — reuse for new integrations when the site uses “Sign in with Google”.

**Medium.com integration:** **Medium statistics** need **`storage_state`** on disk (`INTEGRATIONS_BROWSER_SESSION_ROOT`). With provider **Google**, Playwright uses the **shared** credentials above; **`POST /api/integrations/medium/session/browser-login`** captures the session; the stats resolver **auto-logs in** if the session file is missing. Optional **Google OAuth client** in the UI is only for `GET /api/integrations/medium/oauth/google/connect`.

## Testing

- **Coverage requirement: tests must cover at least 90% of code (line coverage) on both backend and frontend.** This is a hard floor for new and modified code; CI enforces it. If a file or branch cannot reach 90%, document the reason explicitly in the PR and add a targeted `# pragma: no cover` (Python) or `/* istanbul ignore next */` (TypeScript) with a one-line justification — do not lower the global threshold.
- **Backend**: `pytest` with `pytest-cov` — CI runs per-team test suites (SE, blogging, market research, SOC2, social marketing, investment, planning v3, sales, deepthought, etc.) and fails the build below 90% line coverage.
- **Frontend**: Vitest + Angular testing utilities; **90% line coverage target** for `src/app`.
- **CI**: GitHub Actions — ruff lint must pass first, then parallel test jobs (coverage-gated at 90%), then docker smoke test.

## Reference Docs

- `backend/agents/agent_provisioning_team/AGENT_ANATOMY.md` — Required structure for AI agents (Input/Output, Tools, Memory, Prompts, Security Guardrails, Subagents); diagrams in `design_assets/`
- `ARCHITECTURE.md` — detailed architecture with Mermaid diagrams (12 sections, including the Product Delivery Loop)
- `backend/agents/software_engineering_team/README.md` — 31KB SE team deep dive
- `docker/README.md` — Full-stack setup, ports, env vars, security
- `user-interface/README.md` — UI setup and API configuration
