# Environment Variables Reference

Complete reference for every Khala environment variable — defaults, behavior, backoff math,
and edge cases. `CLAUDE.md` carries a one-line quick index of these variables and links here for
the full detail.

All numeric env vars in this reference parse defensively: unparseable, blank, unset, or non-positive
values fall back to the documented default, and out-of-range values are clamped to the documented
floor/ceiling unless a row states otherwise. Per-row notes call out only the cases that deviate
(e.g. a non-standard floor) or that interact with other settings.

---

## LLM Client and Thinking

### OLLAMA_API_KEY
Required for Ollama Cloud API.

### LLM_PROVIDER
LLM provider selection.

### LLM_BASE_URL
LLM server URL.

### LLM_MODEL
Model name.

### LLM_NUM_CTX_FALLBACK_TTL_S
TTL (seconds, default `300`) for the Ollama client's provisional `num_ctx` fallback. When a model's
context size is not in `KNOWN_MODEL_CONTEXT` / `LLM_CONTEXT_SIZE` and `/api/show` fails, the client
degrades to a 16384-token context but only caches it for this window before re-attempting — a
transient `/api/show` outage can no longer poison the process into silently truncating large prompts
for its whole lifetime. A successfully-resolved (or known/env) context size is still cached
permanently. Negative floors to `0` (retry on next call).

### LLM_MAX_RETRIES / LLM_BACKOFF_BASE / LLM_BACKOFF_MAX
**Transient** (5xx / connection / timeout) retry schedule for the central Ollama client — defaults
`10` / `2`s / `120`s. These no longer govern HTTP 429 rate limits (see the `LLM_RATE_LIMIT_*` row).

### LLM_ENABLE_THINKING
Global thinking default for all LLM calls that don't specify `think` explicitly (default enabled;
set `false`/`0`/`no` to disable). When enabled, models registered in `KNOWN_MODEL_THINKING_LEVELS`
(e.g. `deepseek-v4-pro:cloud`: low/medium/high/max) think at their **highest** level; unregistered
models get boolean `think: true`. Explicit per-call `think=False` always wins.

### LLM_THINKING_LEVEL
Overrides the thinking level chosen for models with registered levels (e.g. `medium`). Values that
aren't a registered level for the model fall back to the max level with a warning; ignored for
models that only support boolean think.

---

## LLM Rate Limits

### LLM_RATE_LIMIT_MAX_RETRIES / LLM_RATE_LIMIT_BACKOFF_INITIAL / LLM_RATE_LIMIT_BACKOFF_MAX
Dedicated **slow** backoff schedule for HTTP **429** rate limits, applied independently of the
transient schedule above. A 429 means the provider budget is exhausted and won't reset in seconds,
so the first retry waits `LLM_RATE_LIMIT_BACKOFF_INITIAL` seconds (default `300`), doubling with
additive jitter up to `LLM_RATE_LIMIT_BACKOFF_MAX` (default `3600`), for `LLM_RATE_LIMIT_MAX_RETRIES`
retries (default `5` → 6 attempts, worst-case ~2h15m of waiting) before raising `LLMRateLimitError`.
The 429 backoff `time.sleep` runs **after** the concurrency semaphore and HTTP stream are released
(never while holding them); a 429 retry never consumes a transient attempt and vice-versa. The
shared schedule lives in `llm_service/backoff.py` and is reused by the Strategy Lab envelope.

### LLM_RATE_LIMIT_HONOR_RETRY_AFTER
When truthy (default on; `false`/`0`/`no` disables), the central Ollama client honors an
integer-seconds `Retry-After` header on a 429 as `min(max(computed_backoff, Retry-After), cap)` —
additive-only, so it can never shorten the configured floor. Only the integer-seconds form is
honored (HTTP-date / non-numeric / non-positive are ignored). Strands models (Strategy Lab) have no
HTTP-level access to the header, so this applies only to the central client.

---

## Temporal, Security, and Logging

### TEMPORAL_ADDRESS
Enables Temporal mode when set.

### TEMPORAL_NAMESPACE
Temporal namespace.

### TEMPORAL_TASK_QUEUE
Temporal task queue name.

### SECURITY_GATEWAY_ENABLED
Security gateway toggle (default: true).

### ENABLE_LOG_API
Exposes HTTP log endpoint.

---

## Blogging and Medium

### BLOGGING_RUN_ARTIFACTS_ROOT
Optional root for pipeline run artifacts (default: `{tempdir}/blogging_runs`; Docker sets
`/data/blogging/runs`).

### BLOGGING_MEDIUM_STATS_ROOT
Optional base dir for Medium stats job `work_dir` (default:
`{AGENT_CACHE}/blogging_team/medium_stats_runs`).

### MEDIUM_GOOGLE_REDIRECT_URI
Optional; fixed OAuth redirect for Medium’s Google identity link
(`…/api/integrations/medium/oauth/google/callback`) when the API is behind a proxy.

### BLOG_PLANNING_MAX_ITERATIONS
Blog planning refine loop cap (default 5).

### BLOG_PLANNING_MAX_PARSE_RETRIES
JSON parse/repair attempts per planning LLM call (default 3).

### BLOG_PLANNING_MODEL
Optional Ollama model name for **planning only** (same base URL as `LLM_*`).

### INTEGRATIONS_BROWSER_SESSION_ROOT
Root for Playwright `storage_state` files used by browser-based integrations (Medium, etc.); Docker
maps to the shared `agents_data` volume.

---

## Shared Infrastructure and Storage

### SE_WORKSPACE_DIR
Root for software-engineering team per-job workspaces.

### AGENT_CACHE
Shared cache root for all teams (Docker: `/data/agents`); each team namespaces under `{team_name}/`.

### UNIFIED_API_PORT / UNIFIED_API_HOST
Bind address/port for the Unified API (default `0.0.0.0:8080`).

### POSTGRES_HOST (and POSTGRES_PORT / USER / PASSWORD / DB)
Required for migrated teams (blogging, branding, team_assistant, startup_advisor,
user_agent_founder, agentic_team_provisioning, unified_api credentials). Enables Postgres-backed
stores via `shared_postgres`; no SQLite fallback.

---

## AI Systems

### ARCHITECT_MODEL_SPECIALIST / ARCHITECT_MODEL_ORCHESTRATOR
Per-role model overrides for the AI Systems team.

---

## Investment and Market Data

### ALPHA_VANTAGE_API_KEY / FRED_API_KEY
Market data providers used by the Investment Strategy Lab.

### INVESTMENT_MARKET_DATA_CACHE_ROOT
Issue #376. Operator override for the on-disk root of the Investment Team's content-hashed
market-data cache. Falls back to `${AGENT_CACHE}/investment_team/market_data`, then to a tempdir
(with WARN — non-persistent).

### MARKET_DATA_FETCH_WORKERS
Issue #376. Worker count for `MarketDataService.fetch_multi_symbol_range` and
`MarketDataCache.get_or_fetch_multi`. Default `min(len(symbols), 16)`; the previous hard cap of 5
is gone.

---

## Strategy Lab

The full `STRATEGY_LAB_*` knob reference (defaults, backoff math, cascade semantics, and edge cases) now lives in
[`backend/agents/investment_team/strategy_lab/README.md`](../backend/agents/investment_team/strategy_lab/README.md).


---

## Agent Console and Invoke

### AGENT_INVOKE_MAX_PAYLOAD_BYTES
Hard cap on request body for `POST /api/agents/{id}/invoke` and the sandbox shim (default `1048576`
= 1 MiB; overflow returns 413 without spinning up a sandbox).

### AGENT_INVOKE_MAX_OUTPUT_BYTES
Hard cap on agent response body; oversized outputs are truncated with `truncated: true` on the
envelope (default `1048576` = 1 MiB). Applies inside the shim and on the proxy's re-serialize path.

### AGENT_EXEC_TIMEOUT_S
Default per-agent execution timeout (`asyncio.wait_for`) inside the sandbox; overflow returns 504
with `timeout_hit: true` (default `60`). Per-agent override via `invoke.timeout_seconds` in the
manifest.

---

## Agent Cognition and Knowledge Graph

### AGENT_COGNITION_REFLECTION_SUMMARY_LIMIT
Most-recent memory summaries the cognition reflection engine (`agent_cognition/rules/reflection.py`)
fetches **per scale** (month/week/day) as input when proposing rule changes (default `6`).

### AGENT_COGNITION_REFLECTION_MAX_PROPOSALS
Hard cap on the number of `pending` rule proposals reflection writes in one `reflect` run; LLM
suggestions beyond the cap are ignored (default `5`).

### AGENT_COGNITION_REFLECTION_INPUT_CHARS
Character budget passed to `compact_text` for the rendered summaries + active-rules block before the
reflection LLM call (default `8000`). The reflection LLM uses the shared `cognition` model key, so
`LLM_MODEL_cognition` overrides its model.

### NEO4J_BOLT_URL
Bolt URL of the Neo4j server backing the Graphiti knowledge-graph layer over Agent Cognition (e.g.
`bolt://neo4j:7687`). This is the layer's **enablement gate** (`shared_neo4j.is_neo4j_enabled()`): a
real deployment always sets it (Neo4j is required infra — Graphiti runs on top of it), and an unset
value is tolerated only so the unit-test suite can run against a faked Graphiti without a live
database. The graph ingests agent memories as temporal episodes partitioned per agent
(`group_id = agent_id`) and serves recency-ranked related knowledge back for request context and
rule-proposal grounding.

### NEO4J_USER / NEO4J_PASSWORD / NEO4J_DATABASE
Neo4j credentials/database for the knowledge-graph layer (defaults `neo4j` / empty / `neo4j`). Change
the password before any non-local deployment.

### GRAPHITI_LLM_MODEL
Model Graphiti uses for entity/edge extraction. Defaults to the platform's resolved `cognition` model
so the graph reasons with the same model as the rest of the cognition stack. Graphiti talks to the
platform's Ollama via its OpenAI-compatible `/v1` endpoint, reusing `LLM_BASE_URL` and
`OLLAMA_API_KEY`.

### GRAPHITI_EMBED_MODEL / GRAPHITI_EMBED_DIM
Embedding model and dimensionality for Graphiti hybrid search (defaults `nomic-embed-text` / `768`).
The model must be available on the Ollama `/v1/embeddings` endpoint or hybrid search degrades.

### AGENT_COGNITION_GRAPH_SYNC_INTERVAL_S / AGENT_COGNITION_GRAPH_SYNC_BATCH
Cadence (seconds, default `300`) and per-pass batch size (default `50`) for the background graph sync
worker that ingests new `agent_cognition_events` and rollup summaries into the knowledge graph per
agent since a watermark (`agent_cognition_graph_watermarks`). Events keyset on `(recorded_at, id)` and
are added as `event:<id>` episodes; summaries keyset on `(updated_at, id)` (the content-write time
advanced by each accepted `upsert_summary`) and are added as `summary:<id>:<version>` episodes, so a
recomputed summary — whose `version` advanced — is re-ingested as a fresh per-version episode rather
than overwriting the prior one. The worker is a no-op when `NEO4J_BOLT_URL`/`POSTGRES_HOST` are unset.

### NEO4J_SLOW_OP_MS
Slow-call log threshold (ms, default `1000`) for `shared_neo4j.timed_graph_op`.

### AGENT_COGNITION_SCHEDULER_INTERVAL_S
Cadence (seconds, default `3600`, floored to `60`) of the Agent Cognition scheduler — the live driver
that, per agent with memory, sequences `ensure_rollups_current` → `reflect` → `prune_events`. It
produces the day/week/month/year summaries the graph ingests and the `pending` rule proposals the
approval API serves; it **never** activates a rule (reflection only writes proposals). No-op when
`POSTGRES_HOST` is unset.

### AGENT_COGNITION_GRAPH_SEARCH_TOP_K
Max related facts retrieved per knowledge-graph search in `build_graph_context` (default `10`),
scoped to the agent's `group_id`.

---

## Coding Team and GitHub

### GITHUB_TOKEN
Default token for the coding team's `POST /api/coding-team/run-from-github` flow. Per-request
`github_token` in the body overrides this. Needs `Issues: read/write`, `Pull requests: read/write`,
`Contents: read/write`, `Metadata: read` (or classic `repo`).

### GITHUB_API_URL
Optional override for the GitHub REST base URL used by the coding team's GitHub client
(`backend/agents/coding_team/github_source/`). Defaults to `https://api.github.com`; set to a GitHub
Enterprise URL when relevant.

### GITHUB_DEPENDENCY_CONCURRENCY
Bounds the concurrent per-issue `blocked_by` dependency fetches that enrich
`GET /api/integrations/github/issues` (the coding-team issue picker). Each open issue is annotated
with the issues it depends on so the UI can flag blocked issues; the lookups fan out under a
semaphore of this width (default `8`). A failed/absent lookup degrades to no dependencies for that
issue and never fails the list.

### CODING_TEAM_REVIEW_RETRIES
Number of times the coding-team Tech Lead `run_code_review` LLM call is retried (with jittered
exponential backoff) on a transient failure (rate limit / timeout / provider outage) before the
review is flagged as an infrastructure error (default `2` → 3 attempts; floored at 1 attempt). On
exhaustion the orchestrator fails the task once with a clear
diagnostic rather than re-sending the same failing prompt through the revision loop.

### CODING_TEAM_ANSWER_WAIT_TIMEOUT_S
Wall-clock cap (seconds, default `3600`) the coding team's human-in-the-loop decision gate blocks
waiting for the user to answer escalated open
questions. When the Tech Lead or a Senior SWE hits a product/design/policy/safety decision the plan
does not answer, the job pauses (`status="waiting_for_user"`, `waiting_for_answers=true`, questions
on `pending_questions`) and surfaces them (via `GET /status/{job_id}`, and as a GitHub issue comment
on the `run-from-github` path); answers are submitted to
`POST /api/coding-team/run/{job_id}/answers` (or, on the SE-driven path, the existing
`POST /run-team/{job_id}/answers`), which threads the user's decisions into the plan and resumes. On
timeout the job fails closed (`failed`) rather than proceeding on a guessed decision. A dead
orchestrator thread (e.g. server restart) is recovered via
`POST /api/coding-team/run/{job_id}/resume`.

---

## SE CI Gate and Git Identity

### GIT_COMMIT_USER_NAME
Author/committer name for every git commit platform code makes (SE pipeline, coding team, agent git
tools — all routed through `software_engineering_team/shared/git_utils.py`). Default `Khala`. Blank
values fall back to the default; natively-exported `GIT_AUTHOR_*`/`GIT_COMMITTER_*` env vars win over
this setting.

### GIT_COMMIT_USER_EMAIL
Author/committer email for platform git commits. Default `brandon.kindred@gmail.com`. Same precedence
rules as `GIT_COMMIT_USER_NAME`.

### SE_CI_GATE_ENABLED
Master toggle for CI gate verification after code generation (default `true`). When enabled, the
orchestrator runs lint/test/build checks on generated repos before marking jobs complete.

### SE_CI_GATE_TIMEOUT_S
Timeout in seconds for GitHub CI status polling when a remote is available (default `300`). Only
applies when `GITHUB_TOKEN` + repo info are provided.

### SE_CI_GATE_LOCAL_FALLBACK
When `true` (default), runs CI checks locally via subprocess (ruff, pytest, npm lint/test) when no
GitHub remote is available. Set to `false` to skip CI gate entirely without GitHub.

---

## Profiles

### AUTHOR_PROFILE_PATH
Path to user/author profile YAML injected into blogging prompts. Falls back to
`$AGENT_CACHE/author_profile.yaml`, then to the bundled example. See
`backend/agents/blogging/author_profile/`.

### AUTHOR_PROFILE_STRICT
When `true`, missing/invalid profile raises instead of falling back to the bundled example.
Recommended for production.

### JOB_SEEKER_PROFILE_PATH
Path to the job-matching team's job-seeker profile YAML (standing search criteria). Falls back to
`$AGENT_CACHE/job_seeker_profile.yaml`, then to the bundled example. See
`backend/agents/job_matching_team/profile/`.

### JOB_SEEKER_PROFILE_STRICT
When `true`, a missing/invalid job-seeker profile raises instead of falling back to the bundled
example.

### JOB_MATCHING_SERVICE_URL
Upstream URL the unified API proxies `/api/job-matching/*` to. The job-matching team also reuses
`OLLAMA_API_KEY` for live web search.

---

## Social Marketing

### SOCIAL_MARKETING_WINNING_POSTS_TOP_K
Max exemplars retrieved from the social marketing Winning Posts Bank per concept run (default `5`).

### SOCIAL_MARKETING_WINNING_POSTS_RERANK_ENABLED
Enable LLM rerank stage in the Winning Posts Bank retrieval (default `true`; set to `false` to
disable).

### SOCIAL_MARKETING_WINNING_POSTS_INGEST_THRESHOLD
Engagement-score cutoff (0..1) above which performance observations are auto-promoted into the
Winning Posts Bank (default `0.7`).
