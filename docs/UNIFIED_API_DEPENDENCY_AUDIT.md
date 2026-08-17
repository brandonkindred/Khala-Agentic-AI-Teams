# Unified API Dependency Audit

Audit deliverable for the unified-api slim Docker image and dependency set
initiative. This document is the agreed allow/deny list referenced by the
follow-up work:

- A follow-up creates `requirements-unified-api.txt` from the allow-list below.
- A follow-up points `backend/Dockerfile` at that slim file.
- A follow-up smoke-verifies the slim image and documents the split in `docker/README.md`.

This document makes no code changes. Editing the Dockerfile or creating the slim
requirements file is explicitly out of scope here.

## Method

1. Traced the process entry point (`backend/run_unified_api.py` → `unified_api.main:app`)
   through every module imported at **module load time** and at **FastAPI lifespan
   startup** in `backend/unified_api/main.py`, to determine what actually executes
   in the unified-api process before it accepts a request.
2. Cross-referenced `backend/unified_api/config.py` (`TEAM_CONFIGS`) to determine
   which of the 24 configured teams run in-process vs behind an HTTP proxy to
   their own container.
3. Grepped the entire `backend/` tree for every heavy/notable third-party package
   named in the issue (`strands`, `pyarrow`, `numpy`, `pandas`) plus every other
   package pinned in `backend/agents/requirements.txt` — the file `backend/Dockerfile`
   actually installs into the unified-api production image today — to classify each
   import site as import-time (module scope) vs lazy (function-local/deferred), and
   which team it belongs to.
4. Spot-checked the highest-risk claims directly (e.g. confirmed zero `pandas`/
   `numpy`/`pyarrow` import sites anywhere under `backend/unified_api/`).

## Architecture summary: proxy vs in-process

`backend/unified_api/main.py` states outright that "no team code is imported or
run in-process" for most teams. Of the teams/modules configured in
`backend/unified_api/config.py`, only these run **in-process** (imported and
mounted directly into the FastAPI app, or started as an in-process worker thread
at lifespan startup):

- `user_profile`
- `product_delivery`
- `agent_studio` (authoring CRUD is in-process; its Temporal worker starter is a no-op)
- Platform modules that aren't "teams" in `TEAM_CONFIGS` but are always mounted:
  `agent_platform.console`, `agent_platform.registry`, `agent_cognition`, `team_assistant`, `llm_service`,
  `agent_llm_tools_service`, `agent_platform.sandbox` (sandbox reaper + routes)

Every other team (`software_engineering`, `investment`, `blogging`, `branding`,
`market_research`, `soc2_compliance`, `social_marketing`, `accessibility_audit`,
`ai_systems`, `planning`, `coding_team`, `sales`, `road_trip_planning`,
`agentic_team_provisioning`, `startup_advisor`, `user_agent_founder`, `deepthought`,
`job_matching`) is proxied over HTTP (`unified_api/team_proxy.py`) to its own
container and never imported by the unified-api process at all — so their heavy
per-team dependencies (strands-based agent code in every team, `pandas`/`numpy`/
`pyarrow`/`yfinance` in `investment_team`, `beautifulsoup4` in `blogging`/
`job_matching_team`, etc.) are irrelevant to what unified-api needs installed.

One exception worth naming explicitly: `unified_api/routes/integrations.py` does
reach into `software_engineering_team.clone_workspace` and
`software_engineering_team.github_source.client`, and lazily into
`investment_team.tradingview_mcp.client`, for specific integration endpoints — none
of those modules import any heavy package.

## Allow/deny table

Verdicts below apply to what should be installed for the **unified-api container
specifically**. "Keep" means the slim requirements file should include it; "Drop"
means it should be omitted (left to team-specific requirements files); "Needs
decision" means the audit found a real gap that the follow-up work needs to
resolve explicitly rather than silently drop or silently keep.

| Package | Verdict | Justification |
|---|---|---|
| `strands-agents`, `strands-agents-tools` | **Keep** | Genuine in-process dependency: `agent_platform/studio/assistant.py` imports `strands` directly, and Agent Studio's Temporal worker/activities run in-process (started from `unified_api/main.py` lifespan). Also unconditionally pulls in `boto3` transitively via `strands.models.bedrock` — no separate `boto3` line is needed for unified-api. |
| `anthropic` | **Keep** | `llm_service/clients/claude.py` imports it lazily inside a helper (only paid when the Claude provider is actually used), but the `llm_service` package itself is imported eagerly by unified-api's own `routes/llm_config.py` and `routes/llm_usage.py`, which are always mounted. Lightweight HTTP SDK regardless. |
| `neo4j`, `graphiti-core` | **Keep** | `unified_api/main.py` lifespan unconditionally starts `agent_cognition`'s graph sync worker (self-disables via try/except if `NEO4J_BOLT_URL`/`POSTGRES_HOST` unset), which imports both eagerly through `shared/neo4j/client.py`. Real, if optional-at-runtime, in-process dependency. |
| `psycopg[binary]`, `psycopg_pool` | **Keep** | `shared.postgres` is imported eagerly at lifespan startup for schema registration (`register_team_schemas`/`ensure_team_schema`) across every in-process module (`agent_platform.console`, `agent_platform.registry`, `user_profile`, `product_delivery`, `agent_studio`, `agent_cognition`, `team_assistant`). |
| `cryptography` | **Keep** | Used for encrypted integration credentials (`Fernet`) — generated at Docker build time (`backend/Dockerfile` line 28) and used by unified-api's own credential store. |
| `temporalio` | **Keep** | `shared/temporal/__init__.py` is imported eagerly at startup for the sandbox reaper (started in-process). |
| `slack-sdk` | **Keep** | Directly imported by unified-api's own `slack_events_handler.py` and `slack_notifier.py` — not a proxied-team dependency. |
| `APScheduler`, `pytz` | **Keep** | Scheduling primitives used by in-process modules (e.g. sandbox reaper, cognition scheduler). |
| `fastapi`, `uvicorn[standard]`, `pydantic`, `httpx`, `PyYAML`, `json-repair`, `jsonschema`, `python-multipart` | **Keep** | Core web-framework / serialization / validation stack; all lightweight and directly required by `unified_api/main.py` and its always-mounted routes (`jsonschema` specifically: `agent_platform.registry.models` validates `IOSchema.inline_schema` at module load). |
| `prometheus-fastapi-instrumentator`, `opentelemetry-*` (api, sdk, otlp exporters, fastapi/httpx/logging instrumentation) | **Keep** | Observability wiring, imported (try/except-guarded) at unified-api startup for `/metrics` and tracing; moderate weight but not "heavy scientific stack" and directly wired into `main.py`. |
| `pytest`, `pytest-asyncio`, `pytest-cov`, `pytest-xdist` | **Drop from prod image** | Test-only tooling; not a "heavy" package per se, but has no place in the runtime image regardless of team scoping. Flagged here since it's currently in `agents/requirements.txt`, but the actual removal belongs to the slim-requirements follow-up (or a separate test/prod split, if the team wants to keep discussing that separately). |
| `pandas` | **Drop** | Confirmed zero import sites anywhere under `backend/unified_api/`. All usage is `investment_team/strategy_lab/**` and `investment_team/execution/metrics.py` — a proxied team, never imported in-process. |
| `numpy` | **Drop** | Same footprint as `pandas` — `investment_team` only (`strategy_lab/executor/indicators.py`, `execution/metrics.py`, `trading_service/service.py`), zero reach from unified-api. |
| `pyarrow` | **Drop** | Only used by `investment_team/market_data_cache/store.py`. The comment currently justifying its presence in `agents/requirements.txt` cites `shared.postgres.register_all_team_schemas`, but that function's own docstring (`shared/postgres/registry.py`) states it is **not** wired into the unified-api lifespan ("teams run in their own containers and register themselves") — the existing justification is stale and should be corrected when the slim-requirements follow-up drops the pin. |
| `beautifulsoup4` | **Drop** | Only used by `job_matching_team/tools/web_fetch.py`, `blogging/blog_research_agent/tools/web_fetch.py`, and `blogging/blog_medium_stats_agent/scraper.py` — all proxied-team code, zero reach from unified-api. |
| `ollama` (PyPI package) | **Drop — likely dead weight entirely** | Repo-wide search found no `import ollama` anywhere in `backend/`. `OllamaLLMClient` (`llm_service/clients/ollama.py`) talks to the Ollama REST API directly via `httpx`, not the `ollama` package. This isn't just an unified-api-scoping question — the package may be unused across the whole repo and worth a separate removal PR outside this initiative's scope; the slim requirements file should at minimum not carry it forward. |
| `jinja2`, `python-dotenv`, `playwright`, `yfinance` | **N/A — already excluded** | None of these appear in `backend/agents/requirements.txt` (the file the Dockerfile actually installs) today; they only live in `backend/requirements.txt` (local dev/CI) or per-team requirements files. No action needed for the unified-api image on these — see the `playwright` gap below, though. |

## Flagged gaps requiring an explicit decision

These aren't simple keep/drop calls — they're pre-existing inconsistencies the
audit surfaced that the follow-up work needs to resolve deliberately:

1. **`playwright` / Medium browser login.** `unified_api/medium_browser_login.py`
   implements a real, documented feature (env vars `MEDIUM_BROWSER_HEADLESS` /
   `MEDIUM_BROWSER_TIMEOUT_MS` are documented in `unified_api/README.md`), reached
   from `unified_api/routes/integrations.py`, and lazily does `import playwright`
   inside its route handler. However, `playwright` is **not** in
   `backend/agents/requirements.txt` — the file the production Dockerfile installs
   today. This means the Medium auto-login endpoint would raise `ImportError` in
   the currently-deployed unified-api container, independent of any slimming work.
   This audit did not introduce the gap; the follow-up work (or the repo owner)
   needs to choose: (a) add `playwright` to the new slim requirements file so
   this feature works in production, or (b) explicitly document that this endpoint is
   local-dev-only / best-effort in its current state.
2. **`ollama` package removal.** As noted above, this looks like an entirely
   unused pin. Since this initiative's scope is unified-api-specific slimming,
   the cleanest path is for the slim requirements file to simply not include
   it; whether to also remove it from
   `backend/requirements.txt`/`backend/agents/requirements.txt` repo-wide is a
   separate, smaller cleanup that can be raised independently.

## Non-goals

- This document does not create `requirements-unified-api.txt`.
- This document does not modify `backend/Dockerfile`.
- This document does not perform the smoke-verification or docker README update.
