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

> **The Postgres-backed provider list is the sole source of LLM resolution.** Each provider entry
> (managed at `/api/llm-config/providers`) carries its own provider/model/base URL and its **own API
> key** — there is no environment fallback for keys. The variables below no longer configure a live
> provider on their own: `LLM_PROVIDER=dummy` selects the no-LLM harness, and `LLM_BASE_URL`/`LLM_MODEL`
> only supply *defaults* for an entry whose corresponding field is left blank. With an empty list (or
> `POSTGRES_HOST` unset) and a non-`dummy` provider, `get_client` raises `LLMNotConfiguredError`.

### OLLAMA_API_KEY
Used only by the operator "browse Ollama models" utility (`GET /api/llm-config/ollama-models`) to
authenticate a listing against Ollama Cloud. It does **not** authenticate agent requests — a provider
entry uses its own stored key.

### LLM_PROVIDER
`dummy` selects the no-LLM test/dev harness (a hard override that pre-empts the provider list). Any other
value (`ollama`/`claude`) means "not dummy" → resolve from the provider list; it no longer selects a live
single provider on its own.

### LLM_BASE_URL
Default Ollama server URL for a provider-list entry whose `base_url` is blank. Local
(`http://host:11434`) or Ollama Cloud (`https://ollama.com`, default).

### LLM_MODEL
Default model for a provider-list entry whose `model` is blank. For a Claude entry, defaults to
`claude-opus-4-8` when unset.

### LLM_RUNTIME_CONFIG_TTL_S
TTL (seconds, default `30`) for the cross-container runtime config cache backing the resolvers that
supply entry defaults (model/base URL). Each team container caches resolved defaults for this window.
Garbage → default; negative floors to `0` (read-through every call). No effect when Postgres is unset.

### LLM_NUM_CTX_FALLBACK_TTL_S
TTL (seconds, default `300`) for the Ollama client's provisional `num_ctx` fallback. When a model's
context size is not in `KNOWN_MODEL_CONTEXT` / `LLM_CONTEXT_SIZE` and `/api/show` fails, the client
degrades to a 16384-token context but only caches it for this window before re-attempting — a
transient `/api/show` outage can no longer poison the process into silently truncating large prompts
for its whole lifetime. A successfully-resolved (or known/env) context size is still cached
permanently. Negative floors to `0` (retry on next call).

### LLM_CONTEXT_SIZE
Optional global override for the model context window (tokens). When set to a valid integer,
clamped to a floor of `2048` and used for both Ollama (`num_ctx`) and Claude input-window
resolution ahead of the known-model tables / `/api/show`. Invalid values are ignored (fall
through to known-model / provider discovery). Distinct from `LLM_MAX_OUTPUT_TOKENS`.

### LLM_MAX_OUTPUT_TOKENS
Optional cap on **output** tokens per completion (generation), for both Ollama and Claude.
When unset, malformed, or non-positive, clients fall through to their provider defaults
(typically `min(context, 32768)` for Ollama; Claude's default max-output constant). This is
**not** the context window — use `LLM_CONTEXT_SIZE` for that. Formerly named `LLM_MAX_TOKENS`
(hard rename; the old name is ignored).

### LLM_MAX_RETRIES / LLM_BACKOFF_BASE / LLM_BACKOFF_MAX
**Transient** (5xx / connection / timeout) retry schedule for the central Ollama client — defaults
`10` / `2`s / `120`s. These no longer govern HTTP 429 rate limits (see the `LLM_RATE_LIMIT_*` row),
nor empty 200 responses, which get a proof-of-change thinking-downgrade ladder instead (see
`LLM_THINKING_DOWNGRADE_RETRY`).

### LLM_ENABLE_THINKING
Global thinking default for all LLM calls that don't specify `think` explicitly (default enabled;
set `false`/`0`/`no` to disable). When enabled, models registered in `KNOWN_MODEL_THINKING_LEVELS`
(e.g. `deepseek-v4-pro:cloud`: low/medium/high/max) think at their **highest** level; unregistered
models get boolean `think: true`. Explicit per-call `think=False` always wins.

### LLM_THINKING_LEVEL
Overrides the thinking level chosen for models with registered levels (e.g. `medium`). Values that
aren't a registered level for the model fall back to the max level with a warning; ignored for
models that only support boolean think.

### LLM_THINKING_DOWNGRADE_RETRY
Proof-of-change retry ladder for semantically exhausted calls (default on; `false`/`0`/`no`
disables). A **semantically exhausted** call is an HTTP 200 with zero assistant content — typically
a thinking model that produced only reasoning. Re-sending the identical payload rarely helps, so
instead of spending the transient `LLM_MAX_RETRIES` schedule on it, the client retries **immediately**
with progressively reduced reasoning, ending by disabling thinking entirely (the strongest proof of
change — a non-reasoning turn is forced to open the content channel): from a model's **top** tier it
steps one notch down (e.g. `max` → `high`) then, if still empty, to thinking-off; from an
**already-reduced** tier it goes straight to thinking-off (the wire-redundant intermediate tiers are
skipped — e.g. `deepseek-v4-pro:cloud` collapses low/medium/high to one reasoning effort); boolean/
unregistered thinking goes straight to `think=False`. When the ladder is exhausted — or thinking is
already off, leaving no provable change — the call fails hard with `LLMSemanticExhaustionError`,
whose receipt carries `failure_class`, `attempts_used`, the original and last retry thinking levels,
whether any raw (necessarily whitespace-only) content bytes were ever seen, the `finish_reason`, and a
fingerprint of the last payload. The same ERROR log line additionally reports a diagnostic of the
reasoning channel (its accumulated length across attempts and whether a JSON object was found there —
to detect answers misrouted into reasoning); these two fields are logged only, not carried on the
exception object. Each downgrade rung is logged at WARNING. Transient 5xx/connection/timeout faults and 429s keep their own
independent schedules before and after the downgrades. Disabling the toggle restores the legacy
behavior (empty 200s retried verbatim on the transient schedule). The code-review engine layers a
further recovery on top of this for its chunk reviews — see `CODE_REVIEW_THINKING_OFF_RETRY`.

Agents that run a reasoning model in JSON mode where the top `max` tier reliably reasoning-loops can
pin a reduced default tier via `AGENT_DEFAULT_THINK` in `llm_service/config.py` (e.g. `code_review`
defaults to `high`), so the **first** call already runs at a tier that opens the content channel
rather than relying on this post-hoc ladder.

---

## LLM Rate Limits

### LLM_RATE_LIMIT_MAX_RETRIES / LLM_RATE_LIMIT_BACKOFF_INITIAL / LLM_RATE_LIMIT_BACKOFF_MAX
Dedicated backoff schedule for HTTP **429** rate limits, applied independently of the
transient schedule above. A 429 means the provider budget is exhausted and won't reset in seconds,
so the first retry waits `LLM_RATE_LIMIT_BACKOFF_INITIAL` seconds (default `30`), doubling with
additive jitter up to `LLM_RATE_LIMIT_BACKOFF_MAX` (default `120`), for `LLM_RATE_LIMIT_MAX_RETRIES`
retries (default `3` → 4 attempts, worst-case ~3.6 min of waiting; hard ceiling `retries × cap` = 6 min)
before raising `LLMRateLimitError`. The schedule is kept short so a rate-limited call fails fast and
lets failover / the caller take over instead of hanging for an hour; a provider that genuinely needs
long waits can raise these values (e.g. `300`/`3600`/`5` for the previous behavior). Note a
`Retry-After` longer than `LLM_RATE_LIMIT_BACKOFF_MAX` is clamped down to the cap. The 429 backoff
`time.sleep` runs **after** the concurrency semaphore and HTTP stream are released (never while
holding them); a 429 retry never consumes a transient attempt and vice-versa. The shared schedule
lives in `llm_service/backoff.py` and is reused by the Strategy Lab envelope.

### LLM_MAX_CONCURRENCY
Process-global cap on how many LLM network calls run at once, shared by **both** the Ollama and
Claude client paths (`llm_service/concurrency.py`). Default `4`; a missing/garbage value falls back
to `4` and a zero/negative value is floored to `1`. A single `BoundedSemaphore` is acquired around
each provider's network call (and released before the 429 backoff sleep), so a review that fans out
many concurrent chunk/verification calls can never exceed this many in-flight requests regardless of
which provider the failover list resolves to. Lower it (e.g. `2`) when your provider plan permits few
concurrent requests; the limit is read once at first use per process.

### LLM_RATE_LIMIT_HONOR_RETRY_AFTER
When truthy (default on; `false`/`0`/`no` disables), the central Ollama client honors an
integer-seconds `Retry-After` header on a 429 as `min(max(computed_backoff, Retry-After), cap)` —
additive-only, so it can never shorten the configured floor. Only the integer-seconds form is
honored (HTTP-date / non-numeric / non-positive are ignored). Strands models (Strategy Lab) have no
HTTP-level access to the header, so this applies only to the central client.

### LLM_FAILOVER_FAST_429 / LLM_FAILOVER_RATE_WINDOW_S / LLM_FAILOVER_SESSION_WINDOW_S / LLM_FAILOVER_WEEKLY_WINDOW_S
Tune the **multi-provider fallback list** (the ordered providers configured in the LLM Provider
settings UI / `POST /api/llm-config/providers`, stored in the `llm_provider_configs` table). When
more than one provider is configured, a 429 on one provider marks it usage-limited (with a
`reset_at`) and hands off to the next available provider; once `reset_at` passes, the provider is
reset and used again. `LLM_FAILOVER_FAST_429` (default **on**; `false`/`0`/`no` disables) builds the
non-last failover-chain clients with a **zero** in-place 429-retry budget so the hand-off isn't
delayed by the slow `LLM_RATE_LIMIT_*` backoff above — the **last** provider in the chain keeps the
configured backoff (nowhere left to fail over to), so a single-entry list behaves exactly as before.

Ollama Cloud 429 bodies are classified into `session` / `weekly` / `rate` from phrases like
`session usage limit` and `weekly usage limit`. Reset windows:

| Kind | Env | Default | Notes |
|---|---|---|---|
| `session` | `LLM_FAILOVER_SESSION_WINDOW_S` | `3900` (65m) | Fixed from error time; **ignores** `Retry-After` |
| `weekly` | `LLM_FAILOVER_WEEKLY_WINDOW_S` | `86400` (24h) | Fixed from error time; **ignores** `Retry-After` (Cloud weekly bodies omit a reset timestamp) |
| `rate` | `LLM_FAILOVER_RATE_WINDOW_S` | `300` (5m) | Used only when the 429 carries no `Retry-After`; otherwise `Retry-After` wins |

The provider list is the sole source of LLM resolution: with an empty list (or
`POSTGRES_HOST` unset) and a non-`dummy` provider, `get_client` raises `LLMNotConfiguredError` (there
is no single-provider env fallback).

### LLM_PROVIDER_RESET_SWEEP_INTERVAL_S
Interval (seconds, default `5`) for the background sweep that clears a provider entry's
`limit_exceeded` mark once its `reset_at` window has passed. `select_active_entry` (the pure
selection logic behind `get_client`'s failover, called on every LLM call) no longer performs this
reset itself — doing so meant a blocking `UPDATE`/`commit`/cache-clear round trip to Postgres on
whichever call happened to discover the expiry. Instead it enqueues the entry's id in-process
(pure Python, no I/O) and a lazily-started background thread (`shared.concurrency.heartbeat.BackgroundHeartbeat`,
mirroring the SE team's `trace_flusher` pattern — see `SE_TRACE_FLUSH_INTERVAL_S`) drains the queue
on this interval and performs the actual reset write. The caller still gets the entry back
immediately either way; only the DB write (and its cross-container visibility) is deferred by up to
this interval. Floored at `0.1`s internally to avoid busy-looping; garbage/missing env falls back to
the default. `mark_exhausted` (the write on an actual 429) is unaffected and stays synchronous.

### LLM_COMPACTION_CACHE_SIZE
Capacity of the `compact_text` memoization store (`llm_service/compaction.py`),
default **256**. Backed by `shared.cache`: Redis when `REDIS_URL`/`REDIS_HOST` is
set (value TTL via `REDIS_CACHE_TTL_S`; this size still caps how many keys the
Redis LRU ZSET retains per namespace), otherwise an in-process bounded LRU of this
capacity. `compact_text` results are keyed on the `(model, budget, content_description, content)`
tuple and reused on identical calls — most notably the code review agent's
review→fix→re-review loop. Only genuine full compactions are cached; every fallback
path (LLM failure, empty result, or a chunked run with any degraded chunk) is
retried rather than frozen. Set to `0` to
disable the cache (pure passthrough); a value below 0 is floored to 0, and unparseable values fall
back to the default.

---

## Temporal, Security, and Logging

### TEMPORAL_ADDRESS
Enables Temporal mode when set.

### TEMPORAL_NAMESPACE
Temporal namespace.

### TEMPORAL_TASK_QUEUE
Temporal task queue name.

### SE_WORKFLOW_V2
Selects which Temporal workflow class the SE team's `/run-team` endpoints
use when starting a run, on the `software-engineering` task queue. Default
(unset, blank, or any value other than the recognized falsy ones below)
selects the current multi-phase `RunTeamWorkflowV2`. Set to `"0"`, `"false"`,
or `"no"` (case-insensitive) to opt back into the legacy single-activity
`RunTeamWorkflow` (V1), which the worker keeps registered solely to let any
still-open V1 histories keep running or be drained — see the
[HITL pause/resume contract](../backend/agents/software_engineering_team/system_design/hitl_pause_resume_contract.md)'s
"V1 drain status" note for the current inventory of open V1 executions and
the go/no-go call on removing V1 entirely. HITL `submit_answers`/pause-resume
behavior is identical on both paths today (both block on the same job-store
poll loop; neither defines a Temporal signal), so flipping this var does not
change pause/resume semantics.

### Investment team Temporal queues
The investment team runs three Temporal queues, all booted from
`investment_team.temporal.worker.start_investment_temporal_worker_thread` (each on
a distinct team key, since `start_team_worker` is idempotent per team key):

- `investment-queue` — the ad hoc single-backtest `InvestmentBacktestWorkflow`
  and the long-running `PaperTradingWorkflow` (cancel via a `stop` signal).
  Tuned by `INVESTMENT_MAX_CONCURRENT_ACTIVITIES` (below).
- `strategy-lab-queue` — the fine-grained Strategy Lab batch/cycle workflows
  (tuned by `STRATEGY_LAB_MAX_CONCURRENT_ACTIVITIES`).
- `investment-advisory-queue` — the interactive proposal / validation / promotion
  / committee-memo / advisor-session workflows, dispatched execute-and-wait so a
  multi-hour backtest activity can't head-of-line-block a quick request. Each
  call runs under a fresh, randomly-suffixed workflow id (never a bare
  `{op}-{key}`) so two calls for the same logical operation — e.g. two chat
  messages in the same advisor session — can never collide on a live
  workflow id.

The paper-trading (`/strategy-lab/paper-trade`, `/stop`), Strategy Lab run
(`/strategy-lab/run`, `/strategy-lab/runs/{id}/resume`,
`/strategy-lab/runs/{id}/restart`), and orchestrator/advisor mutation endpoints
(`POST /proposals/create`, `POST /proposals/{id}/validate`, `POST /strategies`,
`POST /strategies/{id}/validate`, `POST /promotions/decide`, `POST /memos`,
`POST /advisor/sessions`, `POST /advisor/sessions/{id}/messages`,
`POST /advisor/sessions/{id}/complete` — not the read-only `GET /proposals/{id}`
or `GET /advisor/sessions/{id}`, which read local state directly and work
regardless of Temporal) are **Temporal-only**: with `TEMPORAL_ADDRESS` unset they return
HTTP 503 rather than falling back to in-process execution (`_require_temporal()`'s check,
shared by all of them). When `TEMPORAL_ADDRESS` is set but the shared client itself is
unreachable, most of these routes still 503 (each dispatch helper's own except-block maps
the resulting `RuntimeError` to 503) — except `/strategy-lab/paper-trade/{session_id}/stop`,
whose narrower exception handling (`stop_live_paper_trading`'s generic delivery-failure
catch) maps that case to HTTP 502 instead; only the `TEMPORAL_ADDRESS`-unset path 503s for
that one route. Only the ad hoc `POST /backtests` endpoint keeps a thread
fallback. Note the 503 only covers a missing/unreachable *client*, not a missing poller
on the target queue — the two dispatch styles above degrade differently when a
specific queue has no worker but the shared client is still connected:
paper-trading and Strategy Lab run/resume/restart are fire-and-forget starts
(`start_workflow_sync` only waits for the server to accept the start), so they
return 200 with the workflow left queued indefinitely (see
`strategy_lab/README.md`'s "Temporal dispatch" section for detail); the
orchestrator/advisor endpoints instead execute-and-wait (`execute_workflow_sync`,
via `_execute_advisory`), so a dead `investment-advisory-queue` poller instead
surfaces as an HTTP 502 (`_translate_advisory_failure`) after the ~180s execute
timeout (`ADVISORY_TIMEOUT` — 2 minutes — plus a 60s buffer), not a silent 200.

### INVESTMENT_MAX_CONCURRENT_ACTIVITIES
Int (default `8`, floor `1`). Ceiling on how many `investment-queue` activities
the investment worker runs at once. A live paper-trading session
(`run_paper_trading_activity`) can hold a worker thread for hours (up to
`max_hours`), so this queue defaults above the shared framework's 4-thread cap
to avoid a handful of concurrent sessions silently starving backtest dispatch.
Parsed defensively as an int (unset or unparseable → default `8`; value
`< 1` → floored to `1`). Only read by the investment
worker; mirrors `STRATEGY_LAB_MAX_CONCURRENT_ACTIVITIES`.

### SALES_TEMPORAL_MAX_CONCURRENT_ACTIVITIES
Int (default `8`, floor `1`). Ceiling on how many sales activities the sales
Temporal worker runs at once. The sales pipeline fans each stage out into one
activity per prospect, so this — not the old in-process
`SalesPipelineConfig.pipeline_stage_workers` thread pool — bounds fan-out
throughput; the default matches that pool's width (`8`) so wall-clock is
preserved. Parsed via the shared `env_int` (unset/garbage → default; a parsed value
`≤0` is clamped up to the floor of 1, not reset to the default, with a warning
logged only for the set-but-unparseable case). Only read by the sales worker.

### SALES_TEMPORAL_HEARTBEAT_INTERVAL_S
Float seconds (default `30`, clamped to `[1, 60]`). How often each long sales
LLM activity emits `activity.heartbeat` so Temporal can detect a hung activity
faster than its full timeout. The ceiling is one third of the fixed 180s
activity heartbeat timeout, guaranteeing at least ~3 beats per window regardless
of configuration — so a mis-set value can never make a healthy activity
heartbeat-timeout window. Parsed via the shared `env_float` (unset/garbage/non-finite →
default, with a warning on a set-but-unparseable value).

### MARKET_RESEARCH_TEMPORAL_HEARTBEAT_INTERVAL_S
Float seconds (default `30`, clamped to `[1, 60]`). How often each long market
research LLM activity (UX, psychology, consistency, viability, scripts) emits
`activity.heartbeat` so Temporal can detect a hung activity faster than its full
timeout. Like the sales knob, the ceiling is one third of the fixed 180s activity
heartbeat timeout, guaranteeing at least ~3 beats per window regardless of
configuration. Parsed via the shared `env_float` (unset/garbage/non-finite →
default, with a warning on a set-but-unparseable value). Only read by the market
research worker.

### TEMPORAL_ADDRESS (code review agent default)
**The code review agent runs Temporal by default** (unlike the other teams, which
only switch on when `TEMPORAL_ADDRESS` is set): `CodeReviewAgent.run` dispatches
the durable `CodeReviewWorkflow` and falls back to the in-process coordinator only
when Temporal is explicitly disabled or unavailable. Its address resolves to
`TEMPORAL_ADDRESS` when set, otherwise the built-in default `temporal:7233` (the
app's own deployed Temporal container). There is intentionally no code-review-only
address override: the worker connects through the process-wide shared Temporal
client, which reads only `TEMPORAL_ADDRESS`, so `TEMPORAL_ADDRESS` is the single
override for where reviews run. Setting `TEMPORAL_ADDRESS` to an empty /
`disabled` / `none` / `off` / `0` / `false` / `no` value falls back to
thread mode. Only the
code review agent's *default* is flipped on; every other team's thread-default
dispatch decision is unchanged.

### TEMPORAL_PAYLOAD_COMPRESSION
Boolean (default `false` — opt-in). The shared Temporal client
(`shared.temporal.client`, used by every team's client and worker) always
installs a gzip `PayloadCodec` on its `DataConverter`; this var gates only
whether that codec *writes* compressed payloads — decoding an already-compressed
payload is never gated, so any process running this codec can always read what
another process wrote. Source code and JSON compress well, so turning this on
transparently keeps large activity/workflow payloads — e.g. the code review
agent's map-reduce chunks, which deliberately carry the full, untruncated diff —
under Temporal's 512 KiB `PayloadSizeWarning` (`TMPRL1103`) threshold instead of
just alerting on it; smaller payloads (below `TEMPORAL_PAYLOAD_COMPRESSION_MIN_BYTES`)
pass through uncompressed either way, and compression that wouldn't actually
shrink a payload (already-compressed/high-entropy binary data) is skipped too.

**Rollout**: many teams here are independently deployable services sharing one
Temporal cluster, so a fleet-wide upgrade is not atomic — a process built
*before* this codec existed can never decode a payload one running it already
compressed. That's why encoding defaults off: deploy this code everywhere first
(every process can now decode, but nothing compresses yet, so behavior is
unchanged), then set this to `true` once every service on that cluster is
confirmed running a build with the codec. Turn it off again (or never turn it
on) if you need the Temporal Web UI to render raw, human-readable payloads
without a codec server configured there.

### TEMPORAL_PAYLOAD_COMPRESSION_MIN_BYTES
Int (default `1024`, floor `0`). Serialized payloads smaller than this many bytes
skip gzip entirely (compression overhead outweighs the benefit below this size,
and small payloads are nowhere near the warning threshold anyway).

### SECURITY_GATEWAY_ENABLED
Security gateway toggle (default: true).

### UNIFIED_API_SANDBOX_TEMPORAL_WORKER
Agent Console sandbox reaper/worker toggle (default: true). When true, the
unified-api `lifespan` starts the Agent Console sandbox idle reaper — a
durable `SandboxReaperWorkflow` served by this process's own sandbox-only
Temporal worker thread when Temporal is enabled, or an in-process asyncio
task otherwise. Set to `false`/`0`/`no` to run unified-api without starting
the sandbox reaper or its Temporal worker thread at all.

### UNIFIED_API_TEAM_ASSISTANTS_ENABLED
Team-assistant conversational sub-app mount toggle (default: true). When
true, the unified-api `lifespan` registers every team's assistant chat
sub-app (`<team-prefix>/assistant`) into a mount-spec registry at startup —
each sub-app is then actually constructed and mounted lazily, on that team's
first matching request (a cold-request mount cost, paid once per team, only
for teams that receive assistant traffic). Set to `false`/`0`/`no` to skip
registration entirely (no assistant sub-app is ever mounted, regardless of
traffic) — team proxy routes and health checks are unaffected.

### UNIFIED_API_AGENT_STUDIO_TEMPORAL_WORKER
Agent Studio Temporal worker toggle (default: true). When true, the
unified-api `lifespan` starts the in-process Agent Studio Temporal worker
thread (Agent Studio is Temporal-only; the team's requests fail without it).
Set to `false`/`0`/`no` to run unified-api without booting this worker
thread, e.g. when Agent Studio is unused.

### ENABLE_LOG_API
Exposes HTTP log endpoint.

---

## Observability (OpenTelemetry)

Every team microservice, the unified API, and the blogging service bootstrap
OpenTelemetry via `shared.observability.init_otel`. Metrics are collected by
Prometheus scraping `/metrics`; traces are exported over OTLP only when
`OTEL_EXPORTER_OTLP_ENDPOINT` is set. See `backend/shared/observability/README.md`
for the full SDK-honored list.

### OTEL_EXPORTER_OTLP_ENDPOINT
OTLP collector endpoint for trace (and, unless disabled, metric) export. When
unset, no exporter is built and spans are created but not shipped. In the docker
stack this is set only on the services you are actively debugging — currently
`se-service`, `investment-service`, and `branding-service` (via the compose
`*team-otel-export` anchor, defaulting to the in-stack Grafana Tempo backend at
`http://tempo:4318`). All other team containers, blogging, and the unified API
leave it unset so they do not flood Tempo. Point it at any external collector to
override on those opted-in services.

### OTEL_EXPORTER_OTLP_PROTOCOL
`http/protobuf` (default) or `grpc`.

### OTEL_METRICS_EXPORTER
Standard OTel selector. Set to `none` to skip OTLP metric export while still
exporting traces — the docker stack uses this so metrics stay on Prometheus
scraping and aren't pushed at the traces-only Tempo backend. Any other value (or
unset) leaves OTLP metric export gated on `OTEL_EXPORTER_OTLP_ENDPOINT`.

### OTEL_TRACES_SAMPLER / OTEL_TRACES_SAMPLER_ARG
Standard OpenTelemetry head-sampling knobs. The docker stack defaults to
`parentbased_traceidratio` / `0.05` (5% of root traces; child spans follow the
parent decision). The Python SDK's `TracerProvider` reads these when no explicit
sampler is passed (see `shared.observability.otel.init_otel`). Raise the ratio
toward `1.0` when you need denser traces on the opted-in services; leave unset
services alone — without an endpoint they never export regardless of sampler.

### OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT / OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT
Standard OpenTelemetry span-limit knobs bounding attribute value length and the
number of attributes per span (SDK defaults: unbounded length, 128 count). The
docker stack and `shared.observability.otel.init_otel` both default these to
`2048` / `64` so a single oversized LLM prompt/response attribute can't push a
span past Tempo's `max_bytes_per_trace` ingest cap (`docker/tempo/tempo.yaml`)
and get refused. `init_otel` applies this default in every runtime mode —
including thread-mode, local dev, and pytest, which never source
docker-compose.yml's env block — by resolving these vars itself before
constructing `SpanLimits` (see `_build_span_limits` in `otel.py`).

### OTEL_EXPORTER_OTLP_TIMEOUT
Standard OTel exporter timeout in seconds (SDK default 10). The docker stack sets
`5` so a slow/unavailable collector fails fast instead of stalling the in-process
span-export thread inside a memory-constrained worker.

### OTEL_BSP_MAX_QUEUE_SIZE / OTEL_BSP_MAX_EXPORT_BATCH_SIZE / OTEL_BSP_SCHEDULE_DELAY
Standard OpenTelemetry `BatchSpanProcessor` knobs (SDK defaults 2048 / 512 /
5000 ms). The docker stack sets `512` / `128` / `2000` to bound the in-process
span buffer (so a stalled collector can't pile spans up in a worker that is
already near its memory budget) and flush more often. `MAX_EXPORT_BATCH_SIZE`
must be ≤ `MAX_QUEUE_SIZE`.

---

## Software Engineering Observability & Learning

The SE team instruments its pipeline on top of the OpenTelemetry layer above.
Every `llm_service` call emits a span carrying `agent.name`, `task.id`,
`job.id`, `phase`, `llm.model`, `llm.input_tokens`, `llm.output_tokens`,
`cost.usd`, and `outcome`, plus a `khala.llm.cost_usd` counter. Per-job cost is
accumulated and written to the job-store entry (`cost_usd`). DORA metrics and
cost are exposed at `GET /api/se/metrics` (alias of
`/api/software-engineering/dora`) and rendered in the Agent Console
"Metrics" tab. Post-mortems and quality-gate rejections are distilled into the
`se_learnings` Postgres table and the top-N relevant ones are injected into the
Tech Lead's Design prompt. All Postgres-backed pieces no-op when `POSTGRES_HOST`
is unset; there is **no** per-job budget cap.

### `LLM_PRICE_<model>`
Per-model price override for token→USD cost estimation, formatted
`<usd_per_1k_input>/<usd_per_1k_output>`. `<model>` is the model name uppercased
with each run of non-alphanumerics collapsed to `_` (e.g.
`LLM_PRICE_DEEPSEEK_V4_PRO_CLOUD=0.0003/0.0012`). A malformed value is ignored
(falls back to the built-in table); an unknown, unpriced model costs `$0` rather
than a guessed amount.

### SE_COST_FLUSH_INTERVAL_S
Minimum seconds between flushes of a job's running cost to the job store
(default `2`; garbage → `2`, negatives clamped to `0` = flush every call). The
in-process accumulator is the fast path; the job-store `cost_usd` is the durable
figure.

### SE_TRACE_TO_POSTGRES
When truthy (`true`/`1`/`yes`; default off), each SE-attributed LLM call is also
persisted as a row in `se_agent_traces` — the substrate the metrics endpoint
reads for per-job and total spend, so cost metrics work even without an OTLP
collector. No-op when Postgres is disabled. Traces are written off the LLM call
path: the observer enqueues into a bounded in-memory buffer and a background
heartbeat drains it via batched `executemany` (see `SE_TRACE_FLUSH_INTERVAL_S`
and `SE_TRACE_BUFFER_MAX`); a final drain runs at shutdown before the Postgres
pool closes, so no row is lost on a clean shutdown.

### SE_TRACE_FLUSH_INTERVAL_S
Seconds between background drains of the in-memory trace buffer to
`se_agent_traces` (default `2`, mirroring `SE_COST_FLUSH_INTERVAL_S`; garbage →
`2`, negatives clamped to `0` which floors the loop at `0.1s` so it never
busy-loops). The observer does zero DB I/O on the LLM call path; rows are
eventually consistent within this interval.

### SE_TRACE_BUFFER_MAX
Maximum number of trace rows held in memory before the oldest is dropped
(default `1000`; floor `1`). Overflow drops the oldest row and logs a WARNING
once per burst — bounded memory, never blocks the caller.

### SE_TRACE_RETENTION_DAYS
Retention window for `se_agent_traces` rows used by `trace_store.prune_traces`
(default `30`; garbage → `30`, negatives clamped to `0`).

### SE_LEARNINGS_TOPN
Number of past-sprint learnings injected into the Tech Lead's Design prompt
(default `5`, clamped to `[0, 50]`; `0` disables injection; garbage → `5`).

### SE_LEARNINGS_RETENTION_DAYS
Retention window (by `last_seen`) for `se_learnings` rows used by
`learnings_store.prune_learnings` (default `365`).

---

## Process Health and Worker Sizing

`shared.observability.process_health` arms each team worker with fault
diagnostics and a memory watchdog; `team_service/entrypoint.py` controls how many
workers run. See `backend/shared/observability/process_health.py`.

### TEAM_WORKERS
uvicorn worker processes per team service (default 2; parsed defensively, clamped
to `[1, 16]`). Each worker is a full Python interpreter loading the whole app, so
on a memory-constrained host fewer workers means a materially smaller footprint —
the docker stack sets `1`. The upper bound of 16 prevents a misconfigured value
from fork-bombing the host into resource exhaustion.

### TEAM_MEMORY_WATCHDOG_INTERVAL_S
Watchdog sample interval in seconds (default 30; floor 1). The same loop also
polls the cgroup `oom_kill` counter, so a shorter interval surfaces a silent OOM
kill sooner — the docker stack sets `10`. The watchdog logs an ERROR whenever the
cgroup `memory.events` `oom_kill` counter rises (a worker was SIGKILLed with no
traceback) and reports `memory.peak`; a peak far below the limit indicates a
host/VM-wide (global) OOM rather than this container exceeding its own limit.
Other watchdog knobs (`TEAM_MEMORY_WATCHDOG_ENABLED`, `_LIMIT_MB`, `_THRESHOLD`)
are unchanged — see the Shared Infrastructure section.

### STRATEGY_LAB_MAX_PARALLEL
Investment team only. Upper bound (Pydantic `le=`) on a Strategy Lab request's
`max_parallel` field, evaluated at import (default 6). Raise it to allow more
parallelism per request on larger hosts without a code change; it also becomes
the default ceiling for `STRATEGY_LAB_MAX_CONCURRENT_CYCLES`.

### STRATEGY_LAB_MAX_CONCURRENT_CYCLES
Investment team only. Hard ceiling on concurrent Strategy Lab cycles per wave
(default = `STRATEGY_LAB_MAX_PARALLEL`, i.e. no extra constraint; floored at 1).
Each concurrent cycle holds its own market data + LLM contexts in the single
worker process, so this caps the worker's peak memory — the dominant OOM driver.
The docker stack sets `2`.

---

## Agent Provisioning

`agent_provisioning_team`'s per-`agent_id` ownership lock
(`shared/agent_lock.py`) is gated behind `workflow.patched(...)` markers in
`temporal/workflows.py` so a workflow history recorded before the lock
existed keeps replaying its original, lock-free command sequence. The
markers can only be safely removed once no such pre-lock history is still
open.

### AGENT_PROVISIONING_LOCK_PATCH_CUTOFF_AT
ISO-8601 timestamp (e.g. `2026-07-17T00:00:00Z`) — the deploy time of the
lock-patch release, set by ops once that release is live. Read by
`shared/visibility_query.find_open_pre_patch_executions`, which reports every
open `AgentProvisioningWorkflow`/`AgentDeprovisioningWorkflow` execution
started before this cutoff — the drain-gate/runbook signal for when the
`workflow.patched(...)` markers can be deleted. Unset or unparseable values
are treated as "no cutoff configured" (fail safe: every open execution of
either workflow type is reported, never silently none) rather than falling
back to a default timestamp.

### AGENT_PROVISIONING_DRAIN_GATE_ENABLED
Enables (default) or disables the rollout drain gate enforced by
`api/main.py`'s `POST /provision` and `DELETE /environments/{agent_id}`
handlers: before starting a new workflow for an `agent_id`, each calls
`find_open_pre_patch_executions(agent_id=...)` and refuses the request
(`409`, with a `Retry-After` header) when an open pre-lock-patch execution for
that `agent_id` is still running, rather than letting the new request race it.
Set to `0`/`false`/`no`/`off` (case-insensitive) to disable the gate entirely;
any other value, or leaving it unset, keeps it enabled. Once no workflow
history predates the lock patch, this gate — and the
`AGENT_PROVISIONING_LOCK_PATCH_CUTOFF_AT` cutoff it depends on — can be
retired along with the `workflow.patched(...)` markers themselves. If the
visibility query itself cannot be answered (Temporal client not ready, or the
query times out), the gate fails open — logged as a warning — rather than
blocking all provisioning/deprovisioning traffic on a visibility-RPC hiccup.

## Agentic Team Provisioning

WAIT-state reliability for Agent Studio pipeline test runs
(`agent_team_studio/agentic_team_provisioning/runtime/pipeline_runner.py`). All three parse
defensively (garbage → default) and are read once when the `PipelineRunner`
singleton is constructed.

When `TEMPORAL_ADDRESS` is set, a pipeline test run dispatches to a durable
`AgenticPipelineWorkflow` instead of the in-process daemon thread: each step runs
as an activity, WAIT steps pause on a `submit_input` Temporal **signal** (bounded by
`AGENTIC_TEAM_PIPELINE_WAIT_TIMEOUT_S` as a workflow timer, not a DB poll), and the
run survives a worker/process restart. Temporal-owned runs are excluded from the
heartbeat-staleness reaper below (Temporal owns their recovery); the poll/stale knobs
apply only to the daemon-thread fallback when Temporal is unset.

### AGENTIC_TEAM_PIPELINE_WAIT_TIMEOUT_S
Maximum time a pipeline test run blocks at a WAIT step for human input before it
fails cleanly (default `259200` = 72h; clamped to `[60, 604800]` — floor 60s,
ceiling 7 days). On expiry the run is transitioned to `failed` via a DB
compare-and-swap with `error` prefixed `wait_timeout:`, `finished_at` set, and the
WAIT step marked `timed_out`, so the persona-test audit panel can distinguish a
timeout from a genuine agent failure. The ceiling exists so a fat-fingered value
can't recreate the original unbounded wait.

### AGENTIC_TEAM_PIPELINE_WAIT_POLL_S
How often (seconds) a waiting run re-checks the durable run status and refreshes
its liveness heartbeat (default `5`; clamped to `[1, 60]`). This bounds how quickly
a run observes a resume that landed on a *different* uvicorn worker (or a
cancellation), since the in-memory wakeup event is only reachable on the worker
that owns the run's thread.

### AGENTIC_TEAM_PIPELINE_STALE_S
Heartbeat-staleness threshold (seconds) used by the orphaned-run reaper (default
`30`; floored to `2 × AGENTIC_TEAM_PIPELINE_WAIT_POLL_S`). A run whose
`heartbeat_at` is older than this (or NULL) while still `running`/`waiting_for_input`
is considered orphaned — its worker thread died on a restart or crash — and is
reaped to `failed` (`error` prefix `orphaned:`). Because staleness is measured on
the heartbeat rather than row age, a live sibling worker's run (which heartbeats
every poll interval) is never reaped. The reaper runs once at startup and then on
a periodic background sweep, guarded by a Postgres advisory lock so only one worker
reaps at a time.

---

## Blogging and Medium

### BLOGGING_RUN_ARTIFACTS_ROOT
Optional root for pipeline run artifacts (default: `{tempdir}/blogging_runs`; Docker sets
`/data/blogging/runs`).

### BLOGGING_ASYNC_MAX_WORKERS
Size of the bounded thread pool that runs asynchronous blogging jobs (the non-Temporal
fallback path for `full-pipeline-async`, its resume/restart, and `medium-stats-async`).
Default `16`, floor `1` (garbage/out-of-range values fall back to/clamp against these).
Each pool worker can stay busy for a long time — a pipeline thread blocks on
human-in-the-loop polling until the user responds or the ~1h stale-job monitor fires — so
this caps the number of idle-but-alive OS threads under many concurrent HITL jobs. When
every worker is busy, further async jobs queue (the endpoints still return a `job_id`
immediately). Temporal remains the durable path for high HITL concurrency and is used
instead whenever `TEMPORAL_ADDRESS` is set.

### BLOGGING_HITL_POLL_INTERVAL_S
Poll cadence (seconds) for every human-in-the-loop wait loop in the blogging
pipeline (draft feedback, uncertainty answers, title selection). Default `10`.
One value keeps all HITL wait loops consistent and configurable in one place.
Independent of the Temporal activity heartbeat — `start_pipeline_heartbeat`
runs a background thread that heartbeats on its own schedule, so these
blocking sleeps never risk a heartbeat timeout.

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

## Software Engineering Code Review

All code-review knobs parse defensively: unset or unparseable values fall back to
the documented default, and parsed values are clamped to the documented floor.

### CODE_REVIEW_MAP_CHUNK_CHARS
Absolute ceiling on chars of code per review map call, independent of model
context. Default `80000`, floor `10000`. Lower it for models that degrade on
large prompts; raise it to cut the number of map calls per large review.

### CODE_REVIEW_SPEC_EXCERPT_CHARS / CODE_REVIEW_ARCH_OVERVIEW_CHARS / CODE_REVIEW_EXISTING_CHARS
Absolute ceilings on the spec / architecture-overview / existing-codebase
excerpts repeated in every review map call. Defaults `16000` / `4000` / `8000`,
floors `1000` / `500` / `500`.

### CODE_REVIEW_SIBLING_SURFACE_CHARS
Cap (chars) on the cross-file "sibling surface" block added to every map prompt —
the top-level symbols (Python `def`/`class`, TS/JS exports) defined by the *other*
changed files in the submission, shown so the reviewer can flag a reference to a
symbol a sibling renamed or removed. Default `2000`, floor `0` (`0` drops the
block). This single value is reserved in the per-chunk code budget
(`compute_code_review_chunk_chars`), used to truncate `_sibling_surface`, and
sliced in the prompt, so the reservation, the cache key, and the prompt can never
diverge.

### CODE_REVIEW_MAP_PARALLELISM
Ceiling on concurrent review LLM calls per review run, shared by both phases:
the map phase (chunk reviews) and the later false-positive verification phase
(one call per cited file). The two phases run sequentially, so this is a
single budget, not two. Default `16`, floor `1` (`1` runs both phases' calls
sequentially). Results merge in deterministic order regardless of completion
order.

The map phase's outer fan-out width is `min(_map_parallelism(), chunk_count)`,
i.e. `min(CODE_REVIEW_MAP_PARALLELISM, LLM_MAX_CONCURRENCY, chunk_count)` (see
`LLM_MAX_CONCURRENCY` above) -- an adaptive fan-out width rather than a flat
one: small reviews (few chunks) never request more workers than they have
chunks, and raising this ceiling for large reviews can never push the map
phase past the process-wide `LLM_MAX_CONCURRENCY` gate, no matter what else is
concurrently in flight. With both vars at their defaults (`16` and `4`), the
effective width is unchanged from the previous fixed-`4` behavior; raise
`LLM_MAX_CONCURRENCY` (and this var, if a higher value is desired) together to
let large-PR reviews fan out wider. This outer, chunk-count-clamped width is
distinct from the run-wide `reviewer.run()` semaphore ceiling described below,
which uses `_map_parallelism()` directly and is intentionally **not** clamped
to `chunk_count`.

The map-phase fan-out governed by this knob **applies only to the in-process
thread-mode fallback** (`coordinator.run_coordinator`, via `mapping.py`'s
parallel-map helper) — used only when Temporal is disabled or unavailable. It
has **no effect** on the default Temporal-dispatch *map phase* (code review
runs Temporal by default; see `TEMPORAL_ADDRESS (code review agent default)`
above): there, each chunk is reviewed by its own durable `review_chunk_activity`,
and fan-out concurrency is governed instead by the Temporal worker's own
activity-slot ceiling, `CODE_REVIEW_MAX_CONCURRENT_ACTIVITIES` (below), which
has its own adaptive-width behavior mirroring this one — raising
`CODE_REVIEW_MAP_PARALLELISM` itself still does nothing to speed up a large PR
review's map phase running in the (default) Temporal mode. The
false-positive verification phase, however, always runs its per-file calls
via an in-process `ThreadPoolExecutor` (even under Temporal, inside the
single verify activity — see `CODE_REVIEW_VERIFY_TIMEOUT_SECONDS` below), so
this ceiling still bounds *verification* concurrency in all dispatch modes.

**Bisection-recovery concurrency.** A chunk whose review fails a recoverable
content error (`mapping._review_chunk_with_recovery`) is retried by splitting
it in half and reviewing both halves concurrently. Those bisection-halves
calls are also `reviewer.run()` calls, so this knob's enforcement contract
covers them too, but the two dispatch modes differ in *how* it is enforced:

- **Thread-mode fallback** (in-process `run_coordinator`): a single
  `threading.Semaphore`, sized to `chunking._map_parallelism()` (`min(CODE_REVIEW_MAP_PARALLELISM,
  LLM_MAX_CONCURRENCY)`, floored at 1) and created once per review run, is
  threaded through the whole map phase — `_map_chunks` → `_cached_review_chunk`
  → `_review_chunk_with_recovery` — and acquired around every actual
  `reviewer.run()` call: the top-level chunk review, any same-input retry, the
  thinking-off retry, and both bisection halves. Unlike the outer map-phase fan-out
  width, this size is **not** further clamped to `chunk_count`: a review with a
  single top-level chunk that bisects can still issue up to that same budget of
  concurrent `reviewer.run()` calls (e.g. both halves at once), not artificially
  limited to 1. This makes `CODE_REVIEW_MAP_PARALLELISM` a true **run-wide**
  ceiling: the total number of concurrent chunk-review LLM calls for one run —
  top-level fan-out plus every bisection recursion combined, at any depth —
  never exceeds it, even when several top-level chunks bisect at the same time
  (previously, each bisecting worker added its own independent 2-worker pool on
  top of whatever else was in flight, so two simultaneously-bisecting top-level
  chunks could issue four concurrent recovery calls even with this knob set to
  `2`).
- **Temporal mode**: only the top-level chunk fan-out is split across
  independent activities — `temporal/workflows.py` schedules one
  `review_chunk_activity` per prepared chunk. A chunk's bisection recursion is
  **not** its own activity: `review_chunk_activity` calls
  `mapping._cached_review_chunk` once (`temporal/activities.py`), and any
  bisection halves it triggers run as nested, in-process calls inside that
  same activity, exactly like the thread-mode recursion but with no run-scoped
  semaphore threaded in (an activity has no reference to another activity's
  in-process objects, so `run_coordinator`'s semaphore can't reach across
  them, and no per-activity equivalent is constructed either). Concurrency
  within one activity's bisection is therefore bounded only by the fixed
  2-worker pool per split point and the process-wide `LLM_MAX_CONCURRENCY`
  semaphore (every provider client call still acquires it) — **not** by the
  Temporal worker's activity-slot ceiling, `CODE_REVIEW_MAX_CONCURRENT_ACTIVITIES`
  (below), which bounds how many *top-level* chunk activities run concurrently,
  not the nested bisection calls inside any single one of them. This is an
  accepted, documented gap rather than a bug: it only matters for an operator
  who sets `CODE_REVIEW_MAP_PARALLELISM` *below* `LLM_MAX_CONCURRENCY`
  specifically for a stricter per-review budget (cost control, or a provider
  tier that throttles below the global default), and even then concurrent
  bisections can only transiently exceed the ceiling they configured, never
  cause literal overload beyond the still-enforced global limit.

### CODE_REVIEW_VERIFY_TIMEOUT_SECONDS
Int (default `3600`, floor `1`). Per-group timeout for the false-positive
verification phase's LLM calls (one call per cited file, fanned out across
`CODE_REVIEW_MAP_PARALLELISM` worker threads when there is more than one
group). The default matches the Temporal verify activity's 60-minute
`start_to_close` budget so a tool-using verifier agent on a slow host is not
cut off mid-call while the activity still has wall-clock left. A group whose
verification call exceeds this timeout is treated the same as any other
verification failure — fail-safe: its findings are kept and a warning is
logged — rather than blocking the rest of the phase indefinitely. Unlike
`CODE_REVIEW_MAP_PARALLELISM`'s map-phase restriction, this applies in
**both** dispatch modes: the verification phase always runs its per-file
calls via an in-process `ThreadPoolExecutor`, even under the default Temporal
dispatch mode, where it executes inside the single
`code_review_verify_false_positives` activity.

### CODE_REVIEW_VERIFY_MAX_FINDINGS_PER_GROUP
Int (default `40`, floor `1`). Cap on how many findings the false-positive
verification phase inlines into a single per-file LLM call
(`_build_group_prompt`/`_verify_group`). A cited file whose genuine findings
exceed this cap is split into multiple same-sized batches — each its own
verification call, fanned out the same way calls for additional files are
(subject to `CODE_REVIEW_MAP_PARALLELISM` and
`CODE_REVIEW_VERIFY_TIMEOUT_SECONDS`, above) — instead of growing one
prompt/agent turn without bound as a single file's finding count grows.
Verdicts from every batch are merged back onto the *original* finding list
via the batch's own slice of original indices, so which batch (and which
within-batch index) confirmed a false positive does not change which finding
gets dropped. Lowering this cap increases the number of verification LLM
calls (and therefore cost/latency) for files with many findings; raising it
trades that against a larger prompt per call. This is a cap on how many
*findings* share one verification call — separate from any cap on how much
*file content* a single tool read can return (out of scope here; tracked in
a separate sub-issue).

### CODE_REVIEW_MAX_CONCURRENT_ACTIVITIES
Int (default `8`, floor `1`). Two things, both governed by this one knob (see
`code_review_agent/temporal/config.py::resolve_max_concurrent_activities`, the
shared resolver both read from):

1. Ceiling on how many `code_review-queue` activities the code review Temporal
   worker runs at once, across every concurrently-executing workflow on that
   worker. The shared framework default (`4`) is sized for narrow, fixed-width
   fan-out; code review fans out one `review_chunk_activity` per review chunk,
   and a large PR can produce dozens of chunks, so a low ceiling turns a large
   review into many sequential rounds — this was the root cause of a review
   timing out its whole-review client wait (`CODE_REVIEW_EXECUTE_TIMEOUT_S`
   below) even though it was still executing durably. `8` mirrors
   `SALES_TEMPORAL_MAX_CONCURRENT_ACTIVITIES` and
   `INVESTMENT_MAX_CONCURRENT_ACTIVITIES`. The worker boot code
   (`code_review_agent/temporal/worker.py`) reads this via
   `resolve_max_concurrent_activities` to size the activity slot pool.
2. The validated-capacity ceiling for each individual review's own **adaptive**
   map-phase fan-out width (`config.resolve_temporal_fanout_width`, computed
   once per review inside `prepare_review_activity` and applied in
   `CodeReviewWorkflow.run` via an `asyncio.Semaphore` bounding how many of
   that review's own `review_chunk_activity` calls are in flight at once): the
   value actually used per review is `max(1, min(CODE_REVIEW_MAX_CONCURRENT_ACTIVITIES,
   chunk_count))`, mirroring `CODE_REVIEW_MAP_PARALLELISM`'s in-process
   `min(ceiling, LLM_MAX_CONCURRENCY, chunk_count)` formula (collapsed to two
   terms plus a floor here because, for Temporal, the ceiling and the
   validated-capacity gate are the same knob, and a review is never given
   zero workers) — a small review (few chunks) never requests more activity
   slots than it has chunks, and a large review can never request more than
   the worker's own provisioned capacity, so this cannot reintroduce the 4→8
   timeout incident: raising this var still raises worker capacity as before,
   and no single review can now request more of that capacity than was
   actually validated. With the default (`8`), a large PR (dozens of chunks,
   per `code_review_agent/docs/CODE_REVIEW_CHUNK_COUNT_TELEMETRY.md`'s
   ~20-50-chunk "large PR" band) requests exactly `8` concurrent slots rather
   than scheduling every chunk unconditionally — the same worker ceiling
   already bounded the old unconstrained `asyncio.gather`, so the benefit for
   a review this size is bounded, replay-safe scheduling (via the
   `workflow.patched` gate; see `temporal/workflows.py`), not freed-up
   capacity for other reviews: a review with `chunk_count >= 8` still uses
   the worker's full provisioned capacity while its own chunks are in flight.

Parsed via the shared `env_int` (unset/garbage → default; a parsed value
`<=0` is clamped up to the floor of 1, not reset to the default, with a
warning logged only for the set-but-unparseable case).

### CODE_REVIEW_EXECUTE_TIMEOUT_S
Int seconds (default `21600` = 6h, floor `60`). Ceiling on how long
`CodeReviewAgent.run`'s synchronous Temporal dispatch
(`execute_code_review_workflow_sync`) waits for the whole durable review before
giving up client-side. This is a pragmatic ceiling, not a proven worst-case
bound: the map phase's chunk count scales with PR size with no enforced cap, so
no finite number is formally guaranteed to exceed every run's worst case.
`CODE_REVIEW_MAX_CONCURRENT_ACTIVITIES` (above) is the primary lever for keeping
large-PR reviews inside this ceiling; raise this var (rather than expecting the
default to cover every PR size) if your fleet regularly reviews very large PRs.
Parsed via the shared `env_int`. A Temporal-side `execution_timeout` derived
from this value (minus a fixed 120s margin, floored at 60s — see
`resolve_execution_timeout_s` in `code_review_agent/temporal/config.py`) is also
applied to the workflow execution itself, so an execution this client gives up
waiting on is reclaimed (freeing its worker slots) at essentially the same
moment, rather than running unbounded server-side.

### CODE_REVIEW_MIN_SPLIT_SEGMENT_CHARS
Int (default `8000`, floor `1000`). A failing chunk smaller than twice this is
retried once as-is instead of being bisected. Parsed via the shared `env_int`
(unset/garbage → default; a parsed value below the floor is clamped up to it,
not reset to the default).

### CODE_REVIEW_MAX_BISECT_DEPTH
Int (default `3`, floor `0`). Max bisect-and-retry recursion depth for a
failing review chunk before the chunk is treated as unreviewable and degraded
(see `CODE_REVIEW_BLOCK_ON_UNREVIEWED`: by default its range is recorded
non-blockingly in `not_reviewed_ranges`; with the opt-out on it becomes a
blocking `high` finding). The whole run only raises
`CodeReviewUnavailableError` when *no* chunk could be reviewed at all. `0`
disables bisection; a chunk then gets only the single same-input retry, then
the thinking-off retry, before degrading. Parsed via the shared `env_int`
(unset/garbage → default; a parsed value below `0` is clamped up to the floor
of `0`, not reset to the default, with a warning logged only for the
set-but-unparseable case).

### CODE_REVIEW_THINKING_OFF_RETRY
Default-on last-resort retry for a chunk whose review could not be recovered by
bisection or the same-input retry **and** failed with a reasoning-only
exhaustion (`LLMSemanticExhaustionError`) or an output-token truncation
(`LLMTruncatedError`). One more review is attempted with thinking forced off
(`get_strands_model("code_review", think=False)`), which turns the common
"thinking model reasoned but never emitted the final JSON" case into a real
review instead of a degraded "not reviewed" range — the main lever that makes
that degradation rare. Set to `false`/`0`/`no`/`off` to disable. Only fires on
the production path (an injected strands model, used in tests, has no
re-resolvable thinking level and is skipped); a successful thinking-off recovery
is treated as reduced-fidelity and is never frozen in the map-phase cache, so the
next identical cycle re-attempts at full fidelity. Complements the client-level
`LLM_THINKING_DOWNGRADE_RETRY` (which does a single one-level downgrade inside the
Ollama client); this knob forces reasoning fully off in one shot at the
code-review layer for any provider.

### CODE_REVIEW_BLOCK_ON_UNREVIEWED
Default-**off** toggle that, when enabled, restores the legacy fail-closed behavior
for a chunk that still could not be reviewed after all recovery (bisection,
same-input retry, and the thinking-off retry). By default such a chunk degrades **gracefully**: its
range is recorded non-blockingly on `CodeReviewOutput.not_reviewed_ranges` and in
a telemetry log, but it is **never** posted as a PR comment and **never** blocks
the review — a reviewer-side hiccup is not a code defect, so the chunks that did
review drive the verdict. Set to `true`/`1`/`yes`/`on` to instead turn each
unreviewable range into a blocking `high` "not reviewed" finding that is posted
and rejects the merged review (so unreviewed code cannot pass the gate as
approved). Either way, the **total-failure** guard is unchanged: when *no* chunk
could be reviewed at all the run still raises `CodeReviewUnavailableError`.

### CODE_REVIEW_CHUNK_OUTCOME_CACHE_SIZE
Max entries in the shared map-phase chunk-outcome cache (`shared.cache`; owned by
`mapping._cached_review_chunk`). Applies to both backends: the in-process LRU and
the Redis ZSET trim on write. Redis keys also expire via `REDIS_CACHE_TTL_S`, and
compose Redis is further bounded by `maxmemory`/`noeviction` (app trim + TTLs;
avoids LRU-evicting idle single-flight locks). Hits are shared
across workers when `REDIS_URL`/`REDIS_HOST` is configured; otherwise each process
keeps an in-process store. Backend failures fail open to a miss/recompute.
The review→fix→re-review loop re-invokes the whole coordinator after every batch fix,
but a fix only mutates the files that had issues — so most chunks are byte-identical
to the previous cycle. The cache reuses the prior map-phase result for any chunk
whose exact LLM input (rendered `### path ###` content + segment notes) and context
fingerprint (task/spec/architecture/acceptance/profile inputs plus the resolved
review model) are unchanged, so only the chunks the fix actually touched go back
through the LLM. The same knob also gates **single-flight de-duplication**: within
one submission the map phase reviews chunks in parallel, so two byte-identical
chunks could otherwise miss the cache at the same moment and each fire the LLM
before either result is stored. Instead the first worker reviews while the rest
block and reuse its outcome, so concurrent duplicates trigger a single review.
Default `512`, floor `0` (`0` disables the cache **and** the single-flight
de-duplication entirely — every chunk is reviewed from scratch). Only
fully-reviewed chunk outcomes are cached;
degraded "not reviewed" outcomes are never stored, so a transient failure is
retried for real next cycle. The cache covers the **map phase only** — the
false-positive verification pass always re-runs against the current whole
submission, so no coverage or fail-safe guarantee is weakened, and a changed
profile, task context, or model invalidates the key. Each chunk reviewer is also
given the *sibling surface* (the top-level symbols the other changed files
define/export), which is folded into the chunk's cache key: a sibling's
surface change (a renamed/removed export) re-runs the dependent chunk so the
reviewer can flag the now-broken cross-file reference, while a body-only sibling
edit leaves the surface — and the cached chunk — unchanged.

### PR_REVIEW_POST_OUTAGE_NOTICE
Default-on toggle for the `/review-pr` flow. When an automated PR review cannot
complete (the review engine is unavailable, a chunk's review could not be
recovered, or the reviewer returned no output), the job/review row is still marked
`failed` with the real detail captured in the store, but the pull request receives
at most a single neutral, non-blocking note ("Automated code review could not
complete and did not post findings; it can be re-run.") instead of the raw
exception text. Set to `false`/`0`/`no`/`off` to post nothing at all on the PR for
a review outage (the failure is still recorded in the job store either way). This
does not affect the distinct "no engine provider configured" deploy-misconfig
abort, which remains a loud operator-facing comment.

### PR_REVIEW_DUPLICATE_THRESHOLD_WITH_LOCATION / PR_REVIEW_DUPLICATE_THRESHOLD_NO_LOCATION
Similarity-ratio overrides (0.0–1.0) for the `/review-pr` flow's duplicate-issue
check: before a finding is offered to a human as a "file a new
GitHub issue?" candidate, the reviewed repository's open issues are checked for
one that already tracks the same bug (`difflib.SequenceMatcher` ratio between the
finding's description headline and a candidate issue's title, casefolded). A
match found this way is pre-linked to the existing issue instead of being offered
for creation. `_WITH_LOCATION` (default `0.5`) applies when the finding's
`file_path` also appears in the candidate issue's title/body (a corroborating
structural signal, so a looser text bar is safe); `_NO_LOCATION` (default `0.8`)
applies otherwise, requiring a near-identical headline/title on text alone. Each
parses defensively: a missing, blank, unparsable, or out-of-range value falls
back to its documented default (clamped to `[0.0, 1.0]` when it does parse)
rather than raising or disabling the check.

### PR_REVIEW_DUPLICATE_TOKEN_OVERLAP_MIN
Word-set (Jaccard) overlap override (0.0–1.0, default `0.8`) additionally
required, alongside `PR_REVIEW_DUPLICATE_THRESHOLD_NO_LOCATION`, for the
`/review-pr` duplicate-issue check's text-alone signal (no `file_path` match to
corroborate it). `SequenceMatcher`'s character-level ratio alone is fooled by
two headlines sharing a long templated prefix/suffix around one differing
keyword — e.g. "hardcoded secret in config" vs "hardcoded timeout in config"
scores a character ratio above the default no-location bar despite describing
unrelated bugs — so the tokenized descriptions must also overlap this much
before a match is pre-linked. Parses defensively, same convention as the two
threshold vars above.

### PR_REVIEW_DUPLICATE_TOKEN_OVERLAP_MIN_WITH_LOCATION
Word-set (Jaccard) overlap override (0.0–1.0, default `0.7`) additionally
required, alongside `PR_REVIEW_DUPLICATE_THRESHOLD_WITH_LOCATION`, for the
`/review-pr` duplicate-issue check's location-corroborated signal (the
finding's `file_path` appears in the candidate issue's title/body). The
location signal alone is weaker corroboration than it looks — many
genuinely distinct bugs share the same file — so the same "one differing
keyword amid shared boilerplate" false positive described above (e.g.
"hardcoded secret in config" vs "hardcoded timeout in config", both
mentioning `config.py`) is also reachable via the looser with-location ratio
bar; this closes that gap. Set lower than
`PR_REVIEW_DUPLICATE_TOKEN_OVERLAP_MIN` (0.7 vs 0.8) since the location match
is itself real corroborating evidence a same-file paraphrase can still
clear. Parses defensively, same convention as the other threshold vars.

### PR_REVIEW_DUPLICATE_MAX_OPEN_ISSUES
Caps how many of the reviewed repository's open issues the `/review-pr` flow's
duplicate-issue check reads per review (default `100`). `GitHubClient.list_open_issues`
already paginates and self-limits at 1000, but reading that many issues synchronously
on every review's critical path is real added latency for what is only an
enhancement (skipping a redundant "file a new issue?" offer) — this trades recall
(a match among issues past the cap won't be found) for bounded, predictable review
latency. Parses defensively: a missing, blank, unparsable, or non-positive value
falls back to the default rather than raising or disabling the cap.

### CODE_REVIEW_FALSE_POSITIVE_FILTER
Default-on toggle for the false-positive verification pass. After the map-reduce
review merges its findings, each genuine finding is re-checked against the
*whole* submission — the verifier has read access to every file under review
(`read_file`/`list_files`/`search_codebase` tools), so it can confirm a finding
the bounded chunk reviewer flagged in isolation (e.g. "symbol never defined")
against the real cross-file code and drop it when it is a false positive.
Fail-safe: a finding is removed only on an explicit, confident false-positive
verdict; any ambiguity or verifier error keeps the finding, and the not-reviewed
coverage findings are never removed. Set to `false`/`0`/`no` to disable the pass
(any other value, or unset, leaves it enabled).

When the review is invoked with a repository reader (the GitHub PR-review path
fetches whole files at the PR head and supplies a reader; the software-engineering
pipeline supplies one rooted at the job workspace), the verifier can additionally
read existing repository files *outside* the diff. This lets it confirm that a
file/module a finding claims is missing ("add X" / "X does not exist") already
exists, and drop that false positive. The reader is read-only, bounded, and
fail-safe (a read failure only ever keeps a finding).

### CODE_REVIEW_ARCHITECTURE_CONSISTENCY_PASS
Default-on toggle for the architecture-consistency / cross-codebase-redundancy
pass. This toggle enables the architecture half of the in-process coordinator's
merged whole-submission tail pass (`merged_architecture_side_effect_pass`).
In the in-process coordinator path, when either architecture and/or side-effect
toggle is enabled, the coordinator makes a single merged LLM call and splits
`architecture_findings` back into this category. In Temporal execution mode
(standalone activities), enabling this toggle controls the
architecture-consistency activity independently (one additional LLM call).

An architecture document on `CodeReviewInput.architecture` is optional:
when present (document, overview, components, and/or decisions), it is inlined
in full when context allows; when absent, the pass still runs and the model must
derive expectations from established repository structure and patterns. The
in-process merged call budgets the changed-file path manifest, architecture
body, and code inlining against the (half-filtered) system prompt and a
dual-array response reserve that shrinks on small contexts — and skips the call
entirely when even an empty payload cannot fit. Disabled halves are omitted from
the system/user prompt (not merely discarded after the fact). It can only ADD
findings, in two categories: `architecture` (the change contradicts a stated or
established boundary/pattern/decision in a way that would break integration) and
`refactor` (the change duplicates a capability that already exists elsewhere in
the repository, tool-verified before it is flagged — never guessed from naming
alone). It never removes or alters any finding the map phase or the
false-positive filter already produced. Any setup or LLM failure is fail-safe:
it is logged and yields no additional findings, so a broken pass never blocks or
changes the rest of the review. Set to `false`/`0`/`no` to disable the pass
(any other value, or unset, leaves it enabled).

### CODE_REVIEW_SIDE_EFFECT_IMPACT_PASS
Default-on toggle for the side-effect / blast-radius pass. This toggle enables
the side-effect half of the in-process coordinator's merged whole-submission
tail pass. In the in-process coordinator path, when either architecture and/or
side-effect toggle is enabled, the coordinator makes a single merged LLM call
and splits `side_effect_findings` back into this category. In Temporal
execution mode (standalone activities), enabling this toggle controls the
side-effect-impact activity independently (one additional LLM call).

In Temporal mode this pass makes exactly one additional LLM call (never once
per chunk). In the in-process coordinator it shares a single merged LLM call
with the architecture-consistency pass, with `side_effect_findings` split back
out after the call. That merged call raises a tight `LLM_MAX_TOKENS` (when set
below 8192) to the dual-array output floor so both finding lists can fit in one
completion; unset / already-generous caps keep the provider default. Either
way it has read access to the rest of the repository
(the same `read_file`/`list_files`/`search_codebase`/`find_function_at_line`
tools the false-positive filter and architecture pass use, plus a new
`search_repository` tool that searches the REST of the repository — beyond the
submission — for a substring, capped well below the GitHub PR-review path's
shared per-review fetch budget since the in-process coordinator's tail passes
share a single prompt budget across `filter_false_positives` and the merged
tail pass).

It can only ADD findings, in two
categories: `side-effects` — a genuine side effect with an unintended logical
consequence, where the current implementation's behavior (return value,
exceptions, mutation of shared/passed-in state, I/O, ordering/timing) breaks a
tool-verified caller elsewhere in the system — and `documentation` — a
docstring/comment that no longer matches the implementation (a
documentation-accuracy problem, not a side effect, reported under its own
category rather than mislabeled as `side-effects`).
This pass is only ever given CURRENT file content, never a prior revision, so
it judges behavior as written now rather than comparing against history. It
never removes or alters any finding the map phase, the false-positive filter,
or the architecture pass already produced. `search_repository` requires a
repository reader to be attached (the GitHub PR-review path and the
software-engineering pipeline both supply one; without one, this pass can
still find callers within the submission's own files via `search_codebase`).

Temporal is this agent's default execution mode. A live `RepoReader` cannot
cross that boundary directly, so the reader used for `search_repository`
depends on which kind was supplied: the software-engineering pipeline's
`DiskRepoReader` is rebuilt worker-side from a serializable `repo_root` field
on `CodeReviewInput` (the same mechanism the false-positive filter and
architecture pass use), so repo-wide search works there even under Temporal;
the GitHub PR-review path's reader holds a per-request auth token that cannot
be reconstructed, so that caller forces in-process execution whenever it
supplies a reader instead, bypassing Temporal for that one review. The only
remaining gap is a Temporal review with no reader and no reachable
`repo_root` at all, where `search_repository` falls back to the submission's
own files via `search_codebase` — the same conservative (keep-more) behavior
the other two passes already have in that case.

Any setup or LLM failure is fail-safe: it is logged and yields no additional
findings, so a broken pass never blocks or changes the rest of the review. Set
to `false`/`0`/`no` to disable the pass (any other value, or unset, leaves it
enabled).

### CODE_REVIEW_SIDE_EFFECT_CONSOLIDATION
Default-on toggle for consolidating related `side-effects` findings from the
pass above. Two (or more) `side-effects` issues are merged into one when they
are anchored inside the same enclosing function/method/class, or when one
finding's description/suggestion cites a `path:line` that falls inside another
finding's own enclosing construct — grouping is transitive, so a chain of
findings that each reference or share a construct with the next all collapse
into a single issue. Every other category (including this same pass's own
`documentation` findings) passes through unchanged. Runs after the side-effect
pass and before the coordinator's exact-match dedupe, using the same
`shared_index` (no extra LLM calls; pure source analysis — AST-based for
Python only; non-Python files are not same-construct grouped, because the
column-0 heuristic cannot distinguish indented methods and would false-merge
independent findings). Path aliases such as bare basenames are canonicalized
via `CodebaseIndex.resolve_path` before grouping so a citation of `foo.py`
matches a finding keyed as `app/foo.py`. Set to `false`/`0`/`no` to
disable consolidation (any other value, or unset, leaves it enabled) — this
only turns off merging, the underlying findings are unaffected.

Any setup failure is fail-safe: it is logged and the original `side-effects`
findings pass through unchanged, so a broken consolidation step never blocks
or changes the rest of the review.

### CODE_REVIEW_SPEC_COMPLIANCE_PASS
Default-**off** toggle (`env_bool`, unlike the default-on tail passes above)
for moving spec/acceptance-criteria compliance checking out of every chunk's
prompt and into one dedicated post-merge pass. Design decision recorded in
`system_design/adr/ADR-010-code-review-spec-compliance-single-pass.md`
(see that ADR's Amendment section for how the implementation below deviates
from its original Decision).

When unset or any value other than `true`/`1`/`yes`/`on`, behavior is
unchanged from today: `acceptance_criteria` and `spec_excerpt` are rendered
into every chunk's review prompt, and per-chunk `spec_compliance_notes` are
synthesized into the final narrative exactly as they are now. When enabled
**and** the review profile is `CODE_REVIEW`, the per-chunk prompt omits the
acceptance-criteria/spec-excerpt blocks (architecture overview and
sibling-surface context are unaffected — out of scope for this toggle), and
`synthesize_spec_compliance` (`code_review_agent/synthesis.py`) runs once,
after the final issue list is deduped, over the full spec/acceptance-criteria
text plus the deduped findings list — **no source code is inlined into this
pass**, unlike the sibling architecture/side-effect tail pass. It runs in the
reduce phase (alongside `synthesize_review_findings`), not inside the
concurrent tail-pass set with the false-positive filter/architecture pass,
since it needs the final, post-dedupe issue list. Its note feeds into the
existing narrative synthesis step in place of the per-chunk notes. Any setup
or LLM failure is fail-safe: it is logged and yields an empty compliance
note, never blocking or changing the rest of the review.

**Measured token/cost delta.** Measured directly against the production
prompt-construction code (`ChunkReviewAgent.run` for the per-chunk prompts,
`synthesis._build_spec_compliance_framing`/`build_findings_digest` for the
single dedicated pass) on a representative fixture: a ~15,880-character spec
excerpt, 6 acceptance criteria, and an 8-issue post-dedupe finding list —
close to ADR-010's own "~14K chars, 15 chunks" illustrative example. Prompt
character counts (a ~4-chars/token rule of thumb converts these to an
approximate token figure; the *relative* delta between modes is what
matters, not the exact tokenizer):

| Chunks | Per-chunk mode (chars) | Single-pass mode (chars) | Delta |
|---|---|---|---|
| 1  | 18,143  | 19,279 | single-pass sends **6.3% more** |
| 2  | 36,286  | 20,968 | single-pass sends 42.2% less |
| 5  | 90,715  | 26,035 | single-pass sends 71.3% less |
| 15 | 272,160 | 42,940 | single-pass sends 84.2% less |
| 30 | 544,350 | 68,320 | single-pass sends 87.4% less |

The dedicated single-pass prompt has a fixed overhead (~17,590 chars in this
fixture, independent of chunk count) from carrying the full spec/AC text plus
the findings digest in one call. Below that fixed cost, single-pass mode
*loses* — this refines ADR-010's Risks section, which estimated savings as
merely "close to zero" on 1-2 chunk submissions; measured, a single-chunk
submission is a small net loss, and the crossover to a real win happens
between 1 and 2 chunks. Every submission with 2+ chunks measured here shows a
net reduction, growing toward the fixed-overhead floor as chunk count rises.
This is descriptive data for a future decision on the default, not a
recommendation to flip it (deliberately out of scope here).

---

## Shared Infrastructure and Storage

### SE_WORKSPACE_DIR
Root for software-engineering team per-job workspaces.

### AGENT_CACHE
Shared cache root for all teams (Docker: `/data/agents`); each team namespaces under `{team_name}/`.

### UNIFIED_API_PORT / UNIFIED_API_HOST
Bind address/port for the Unified API (default `0.0.0.0:8080`).

### HTTP_KEEPALIVE_EXPIRY_S
Seconds an idle keep-alive socket is kept in the shared outbound HTTP pool
(`shared.http.get_pooled_client`, used by `JobServiceClient` and other hot paths) before it is
recycled. Default `15.0`, floor `1.0` (a positive value below the floor is clamped up; non-numeric,
non-finite, or non-positive values fall back to the default). Recommended range: a few seconds up to
just under the idle-connection timeout of the upstream/proxy, so the client drops a socket before the
far end closes it — reusing a server-closed connection otherwise raises
`httpx.RemoteProtocolError` ("server disconnected without sending a response"). Read once at import
when `shared.http.DEFAULT_LIMITS` is built, so a change takes effect only on a fresh process.

### POSTGRES_HOST (and POSTGRES_PORT / USER / PASSWORD / DB)
Required for migrated teams (blogging, branding, team_assistant, startup_advisor,
user_agent_founder, agentic_team_provisioning, unified_api credentials). Enables Postgres-backed
stores via `shared.postgres`; no SQLite fallback. Setting `POSTGRES_HOST` only marks Postgres
*configured* — use `shared.postgres.check_connection()` for a real `SELECT 1` reachability probe
(the LLM Provider page and GitHub integration use it to tell "configured but unreachable" apart
from "not configured").

### POSTGRES_CONNECT_TIMEOUT_S
libpq `connect_timeout` (seconds) applied to **every** Postgres connection the platform opens —
both the `shared.postgres` pool (used by `check_connection()` and the migrated-team stores) **and**
the unified-API encrypted credential store (`postgres_encrypted_credentials`, the live read path for
the GitHub / Slack / Medium PAT/secret lookups, which is a per-call connection outside the pool).
Default `3`, floor `1`. Bounds how long a TCP connect to a down or unreachable host can hang —
without it, opening the pool (`open=True`) or a credential read against an unreachable host blocks
for the libpq default, defeating the bounded reachability probe. Note: it bounds only the TCP
*connect* phase; a host that accepts the connection then stalls is bounded by `statement_timeout`
(below) on the credential store, and at the request layer by the route's `asyncio.wait_for` guard.

### POSTGRES_STATEMENT_TIMEOUT_MS
`statement_timeout` (milliseconds) applied as a libpq `options` to the **unified-API credential
store's** per-call connections (GitHub / Slack / Medium secret reads/writes). Default `5000`, floor
`0`. Bounds the query phase that `connect_timeout` does not cover, so a Postgres that accepts the
connection then stalls mid-query can't pin the credential store's `_LOCK` and cascade a stall across
every credential consumer. The unified-API config-status routes also size their request-level
`asyncio.wait_for` budgets off this value (`connect_timeout + statement_timeout + 1s`) so the bounded
query finishes before the request gives up. **WARNING:** setting `0` disables the query bound — a
post-connect stall can then pin `_LOCK` indefinitely, reintroducing the cascade this var prevents.
Scoped to the credential store only — it is deliberately **not** applied to the shared pool, which
would cap legitimate long-running team queries.

### POSTGRES_PROBE_MAX_WORKERS
Caps the number of concurrent `shared.postgres.bounded_probe` worker threads **per surface** — the
bounded offload behind the LLM Provider page and the GitHub config-status reads each get their own
budget of this size, so a stall on one can't starve the other. Default `4`, floor `1`. A probe
normally completes in milliseconds and frees its slot immediately; the cap only bites when that many
workers for a surface are already *stuck* (a Postgres that accepts the connection then stalls
mid-query). The probe `SELECT 1` (via a transaction-local `statement_timeout`) and the credential
read are both query-bounded, so a stuck worker normally releases its connection well before the cap
matters; the cap is the backstop for paths left unbounded (e.g. `POSTGRES_STATEMENT_TIMEOUT_MS=0`).
Once a surface's cap is reached, its further config-status requests degrade to "unreachable"
immediately instead of spawning more threads. **Read once** when each surface's semaphore is first
created (a semaphore can't be resized), so unlike the timeout knobs this takes effect only on a fresh
process — not mid-run.

### POSTGRES_PROBE_MAX_KEYS
Caps how many *distinct* per-surface `bounded_probe` budgets (see `POSTGRES_PROBE_MAX_WORKERS`) the
process keeps. Default `32`, floor `1`. The platform's real probe surfaces are a small fixed set of
static labels, so this is a guard against a (mis)caller deriving a probe label from request data:
without it, each unique label would grow an internal registry without bound and mint a fresh full
worker budget, defeating the cap. Past this many distinct labels, further labels share one dedicated
**overflow** budget. Two consequences, both confined to that misuse regime: overflow callers lose the
"per surface" isolation `POSTGRES_PROBE_MAX_WORKERS` otherwise gives (a stall on one overflow surface
can starve another), and an uncached/dynamic label no longer hits the lock-free fast path. Raise this
only if you genuinely run more than 32 distinct *static* probe labels.

### TEAM_MEMORY_WATCHDOG_ENABLED / _LIMIT_MB / _THRESHOLD / _INTERVAL_S
Per-worker memory watchdog used by every `team_service` microservice
(`shared.observability.process_health`). A daemon thread samples the container's
cgroup memory usage (cgroup v2 `memory.current` → v1 `memory.usage_in_bytes` →
per-process RSS off-cgroup) and logs a single WARNING as it approaches the
budget, so the last log line before an OOM-kill (SIGKILL, no traceback) names the
cause. No-op when no memory limit can be detected.
- `TEAM_MEMORY_WATCHDOG_ENABLED` — master switch (default `true`).
- `TEAM_MEMORY_WATCHDOG_LIMIT_MB` — override the detected budget, in MB (default:
  the cgroup limit, i.e. the container's `mem_limit` / `deploy.resources.limits.memory`).
- `TEAM_MEMORY_WATCHDOG_THRESHOLD` — warn fraction in `(0, 1]` (default `0.85`, clamped to `[0.1, 0.99]`).
- `TEAM_MEMORY_WATCHDOG_INTERVAL_S` — sample interval in seconds (default `30`, floor `1`).

`PYTHONFAULTHANDLER` is also exported by the entrypoint: `install_fault_diagnostics()`
arms Python's `faulthandler` directly in the running process (so a native fault —
SIGSEGV/SIGABRT/… — dumps a Python stack instead of dying silently) **and** sets
`os.environ["PYTHONFAULTHANDLER"] = "1"` when it isn't already set, so any
spawned/forkserver worker inherits the same behaviour at interpreter startup
(an operator-set value is preserved).

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

### TRADINGVIEW_MCP_ENABLED / TRADINGVIEW_MCP_URL / TRADINGVIEW_MCP_TOKEN / TRADINGVIEW_MCP_TOOL
Environment overrides for the TradingView MCP data source the Strategy Lab pulls OHLCV bars from.
Configuration normally comes from the Integrations UI (`PUT /api/integrations/tradingview`, token
stored Fernet-encrypted); these env vars take precedence over the stored config so an operator can
point a container at a server without the Unified API store on its path (isolated team containers,
CI). `TRADINGVIEW_MCP_ENABLED` is truthy (`1/true/yes/on`, case-insensitive). The source is used
only when **enabled** *and* a URL is present; when active it becomes the first provider tried for
every asset class, ahead of the free public fallbacks (Yahoo → Twelve Data → CoinGecko/Alpha Vantage).
`TRADINGVIEW_MCP_TOOL` overrides the MCP tool name the client calls (default `get_ohlcv`).

### TRADINGVIEW_MCP_TIMEOUT_SEC
Per-request wall-clock timeout (seconds) for the TradingView MCP client. Default `30.0`;
non-numeric / non-positive values fall back to the default.

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

### AGENT_REGISTRY_TOMBSTONE_TTL_S
How long (seconds) a worker's own `unregister()` of a dynamic agent id masks that id from `get()`
on the same worker, closing the window where a failed best-effort Postgres delete would otherwise
resurrect the stale row (default `5.0`; clamped to `>= 0.0`).

### AGENT_REGISTRY_TOMBSTONE_MAX_ENTRIES
Max number of tombstoned ids `AgentRegistry` retains per worker; oldest entries are evicted first
once the cap is exceeded (default `1000`; clamped to `>= 1`).

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

### AGENT_COGNITION_INVOKE_ROLLUP_BUDGET
Max rollup periods the invoke gate's lazy catch-up (`agent_cognition/invoke_gate.py`) may process
inline per cognition invoke (default `8`, floor `1`; garbage falls back to the default). Each
processed period costs one LLM summarization call, so the budget keeps a cold-start backlog (up to
`AGENT_COGNITION_ROLLUP_MAX_LOOKBACK_DAYS` of unsummarized periods) from running hundreds of
sequential LLM calls on the invoke hot path. The pass is oldest-first and idempotent: repeated
budgeted invokes — and the unbudgeted central scheduler — drain the remainder.

### AGENT_COGNITION_ROLLUP_INPUT_CHARS
Character budget passed to `compact_text` for the events/lower-scale-summaries block before each
rollup LLM summarization call (`agent_cognition/memory/rollup.py`, default `12000`; garbage or a
non-positive value falls back to the default). Caps how much memory text one summarization period
feeds the model.

### AGENT_COGNITION_DIGEST_EVENT_TOP_N
Number of most-recent in-progress memory events folded into an agent's memory digest at invoke time
(`agent_cognition/memory/retrieval.py`, default `20`; garbage or a non-positive value falls back to
the default). The summary half of the digest is bounded separately by the caller-supplied
`token_budget` (trimmed via `compact_text`).

### NEO4J_BOLT_URL
Bolt URL of the Neo4j server backing the Graphiti knowledge-graph layer over Agent Cognition (e.g.
`bolt://neo4j:7687`). This is the per-process **enablement gate** (`shared.neo4j.is_neo4j_enabled()`),
and it is opt-in at zero cost when unset: `unified_api.main`'s lifespan checks `is_neo4j_enabled()`
before even importing `agent_cognition.graph.sync_worker`, and `shared.neo4j.client.get_graphiti()`
defers its `graphiti_core` imports to first real use — so leaving `NEO4J_BOLT_URL` unset keeps the
`graphiti_core` dependency (and its Neo4j driver) out of `sys.modules` entirely, with no import-time
or memory cost. Neo4j itself is required stack infrastructure for agents (Graphiti runs on top of it),
but processes that do not need Graphiti — notably the unified API (`khala`) reverse proxy — leave this
unset for exactly that reason. Compose defaults `khala`'s `NEO4J_BOLT_URL` empty; set
`NEO4J_BOLT_URL=bolt://neo4j:7687` to opt that process into graph sync (extra memory/CPU for the
driver + worker). An unset value is also how the unit-test suite runs against a faked Graphiti without
a live database. When enabled, the graph ingests agent memories as temporal episodes partitioned per
agent (`group_id = agent_id`) and serves recency-ranked related knowledge back for request context and
rule-proposal grounding.

### REDIS_URL / REDIS_HOST / REDIS_PORT / REDIS_PASSWORD / REDIS_DB
Redis endpoint for `shared.cache` — the shared backend behind the code-review chunk-outcome cache,
submission short-circuit cache, and `compact_text` memoization. When `REDIS_URL` (or `REDIS_HOST`) is
set, those caches persist across worker processes and restarts; when unset, each process keeps an
in-memory LRU (today's single-process behavior). Compose wires Redis **only on `se-service`** via
`*se-redis-env` (not every team container): default `REDIS_HOST=redis` + `REDIS_PASSWORD` (local
placeholder `please-change-me`, same as Neo4j) with blank `REDIS_URL`, so Python builds
`redis://[:quoted-password@]host:port/db` and percent-encodes the password. Do **not** embed an
unquoted password in a compose `REDIS_URL` default — characters like `@` / `:` / `/` break URL
userinfo parsing. For in-process memory on SE, set `REDIS_HOST=` (empty) and leave `REDIS_URL`
blank — compose uses `${REDIS_HOST-redis}` (no `:`) so an empty value is preserved rather than
re-defaulting to `redis`. `REDIS_URL` wins over host/port/password when non-blank. `REDIS_HOST`
must be a bare hostname or bare IPv6 literal (no embedded port — use `REDIS_PORT`; no zone-ID /
scoped addresses — use `REDIS_URL` for those). Backend unavailability fails open to a cache miss
(recompute),
never a review failure. When Redis is unreachable (or the `redis` wheel is missing),
`shared.cache` logs a warning that includes the exception class and falls back to the
in-process LRU / a local recompute — reviews continue without raising. Operators can
distinguish an outage from a genuine cold cache by those warning lines (and by Redis
client/healthcheck failures); there is no separate metric today. Optional
`REDIS_SOCKET_CONNECT_TIMEOUT_S` (default `1.0`) and `REDIS_SOCKET_TIMEOUT_S`
(default `1.0`) tune redis-py's connect and per-command socket timeouts. Command
timeout must stay finite so a Redis that accepts TCP but stalls on GET/SET/EVAL
fails open to miss/recompute instead of hanging the review thread forever.
`REDIS_MAX_CONNECTIONS` (default `50`) caps the redis-py connection pool so high
map parallelism cannot open unbounded sockets from one process.

**Local compose vs production auth.** The in-repo `docker-compose.yml` Redis service enables
`requirepass` from `REDIS_PASSWORD` (default `please-change-me`), written into a generated
config file so the password is not on the `redis-server` process argv (healthcheck still uses
`REDISCLI_AUTH`). Compose hex-encodes every password byte as redis.conf `\xHH`
escapes via `docker/redis/escape_requirepass.sh` (BusyBox-safe `od`/`sed`) so
backslash, quote, embedded newlines, and trailing newlines round-trip under
Alpine Redis. Redis credentials are injected only into `se-service`, so other
team containers on `stack` cannot read/write review or compaction namespaces
without credentials. Host publish is IPv4 loopback only (`127.0.0.1:6379`) —
from the host use `127.0.0.1:6379`, not `localhost` / `::1`. Production
deployments must use a strong secret, ACL/`requirepass`, and keep Redis off
public interfaces. Compose Redis is intentionally ephemeral (no volume) — a
restart is a cold cache; durable state lives elsewhere. Memory policy is
`noeviction` with `maxmemory 512mb` (app ZSET trim + 1h value TTLs bound
capacity; avoids LRU-evicting idle single-flight lock keys mid-compute).
`se-service` depends on Redis with `condition: service_started` (not
`service_healthy`) so SE still boots when Redis is unhealthy or when
`REDIS_HOST=` opts into in-process memory — per-op fail-open covers mid-run
outages.

### KHALA_CACHE_BUILD_ID / KHALA_BUILD_ID
Optional deploy/build suffix appended to `shared.cache` namespaces used by
code-review (`cr:chunk:v2`, `cr:sub:v1`) and LLM compaction (`llm:compact:v1`).
`KHALA_CACHE_BUILD_ID` wins when both are set. When unset/blank, namespaces stay
at their static stems (local-dev default). When set to a safe token
(`[A-Za-z0-9._@+/-]+`, no `:`), the effective namespace becomes
`{stem}:{build_id}` so a deploy that changes the id is inherently a cold cache
— prompt/logic changes cannot keep serving pre-deploy verdicts until TTL expiry.
An unsafe non-blank value is ignored (namespaces stay at the stem) and
`shared.cache` logs a warning so operators are not left thinking cold-cache is
active. Compose wires both onto `se-service` from the host env (blank by
default). CI / image builds should set `KHALA_BUILD_ID` (e.g. git SHA) for
production-like stacks.

### REDIS_CACHE_TTL_S / REDIS_LOCK_TTL_S / REDIS_WAITER_POLL_S / REDIS_WAITER_TIMEOUT_S / REDIS_RESULT_TTL_S
Redis backend tuning for `shared.cache`: value-key TTL (default `3600` s — short enough that a
full `noeviction` Redis recovers via natural expiry instead of wedging for a day), single-flight
lock TTL (default `3600` s — sized for long code-review computes so the lock is unlikely to
expire mid-flight), waiter poll interval (default `0.05` s), waiter timeout before recomputing
(default `300` s, **independent** of lock TTL so a crashed leader cannot stall waiters for up to
an hour), and short-lived single-flight result/error-marker TTL (`REDIS_RESULT_TTL_S`, defaults
to the effective lock TTL when unset; the Redis backend still hard-caps markers at 60 s so
abandoned publishes cannot linger past leadership).

**Long reviews vs waiter timeout:** lock TTL can be an hour, but waiters bail at 300 s by default
and recompute in parallel if the leader is still running. That is intentional crash-recovery
trade-off. If code-review map/compute steps routinely exceed five minutes and you need
cross-worker dedup for the whole flight, raise `REDIS_WAITER_TIMEOUT_S` (up to the lock TTL)
or both knobs together — otherwise this PR's single-flight benefit is limited to shorter keys.

Trim-then-retry after OOM only reclaims **this namespace's** LRU ZSET; memory held by other
namespaces is not freed, and the write/lock then fail-opens.

Successful `get` touches the LRU ZSET (write-on-read) so eviction order stays accurate under
multi-worker load. Published error markers reconstruct allow-listed `Exception` subclasses with
best-effort stringified `args` — match on type/message, not arg identity. All parse defensively
with floors: TTL / lock / waiter-timeout / result-TTL floored at `1` s; poll-interval floored at
`0.01` s.

### REDIS_KEY_PREFIX
Optional prefix for all `shared.cache` Redis keys (default `khala`). Blank falls back to the
default. Must not contain `:` — the backend appends `:{namespace}:` itself. An invalid prefix
(or other RedisBackend construction error) fails open: `get_shared_cache` logs and uses the
in-process memory backend instead of raising on first cache touch.

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
than overwriting the prior one. Unified-api's lifespan skips importing this worker's module at all when
`NEO4J_BOLT_URL` is unset; once started (i.e. `NEO4J_BOLT_URL` is set), the worker itself is a further
no-op when `POSTGRES_HOST` is unset.

### NEO4J_SLOW_OP_MS
Slow-call log threshold (ms, default `1000`) for `shared.neo4j.timed_graph_op`.

### AGENT_COGNITION_SCHEDULER_INTERVAL_S
Cadence (seconds, default `3600`, floored to `60`) of the Agent Cognition scheduler — the live driver
that, per agent with memory, sequences `ensure_rollups_current` → `reflect` → `prune_events`, then
runs one platform-wide ledger GC (`gc_terminal_runs`). It produces the day/week/month/year summaries
the graph ingests and the `pending` rule proposals the approval API serves; it **never** activates a
rule (reflection only writes proposals). No-op when `POSTGRES_HOST` is unset.

### AGENT_COGNITION_RUN_TTL_S
Idempotency TTL (seconds, default `604800` = 7 days, floored to `3600`) for the `agent_cognition_runs`
ledger. A *terminal* (`completed`/`blocked`) run row is retained this long so a retry can replay its
stored envelope without re-invoking; once `completed_at` is older than the TTL the scheduler's
`gc_terminal_runs` pass reclaims it. `in_progress` rows are **never** GC'd here — an expired lease is
reclaimed lazily by `claim_run`, preserving its `request_hash` for retry policing.

### AGENT_COGNITION_RUN_LEASE_S
Lease duration (seconds, default `120`, floored to `30`) a `claim_run` holds on an `in_progress`
`agent_cognition_runs` row (`agent_cognition/context.py`). A still-leased row makes a concurrent
retry conflict; once the lease expires the row is reclaimed in place (its `request_hash` retained) and
re-executed. Distinct from `AGENT_COGNITION_RUN_TTL_S`, which bounds *terminal* rows for replay.

### AGENT_COGNITION_WRITEBACK_MAX_BYTES
Byte cap on the `cognition_writeback` half of an invoke envelope
(`shared/agent_invoke/limits.py`, default `1048576` = 1 MiB). The user `output` field has its
own bound (`AGENT_INVOKE_MAX_OUTPUT_BYTES`), so control/memory data never competes with user output;
an over-cap writeback is truncated and flagged rather than dropping the response.

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
(`backend/agents/software_engineering_team/github_source/`). Defaults to
`https://api.github.com`; set to a GitHub Enterprise URL when relevant.

### GITHUB_WEBHOOK_SECRET
Signing secret for the GitHub webhook receiver `POST /api/integrations/github/events`, which lets a
collaborator trigger a PR review by commenting `@khala review` on a pull request. The receiver
verifies each delivery's `X-Hub-Signature-256` HMAC against this secret and rejects mismatches with
`401`. A secret stored via `PUT /api/integrations/github` (encrypted in Postgres) takes precedence
over this env var. When neither is set, the receiver fails closed: a `ping` still succeeds (so you can
verify webhook delivery during setup), but every review-triggering event is refused with `403` until a
secret is configured — an unsigned request must never be able to start a paid review. Only
`issue_comment` events from OWNER/MEMBER/COLLABORATOR authors trigger a review; the commented PR's
own repository is the review target, and whether the stored PAT can act on that repository is
GitHub's decision (the token's authorization configuration is the sole access list — there is no
Khala-side repository allowlist).

### GITHUB_WEBHOOK_DEDUP_TTL_S / GITHUB_WEBHOOK_DEDUP_MAX_ENTRIES
Tune the webhook receiver's in-process, per-worker de-duplication of GitHub redeliveries (same
`X-GitHub-Delivery` id). `GITHUB_WEBHOOK_DEDUP_TTL_S` (default `600`, floor `1`) is how long a delivery
id is remembered; `GITHUB_WEBHOOK_DEDUP_MAX_ENTRIES` (default `1000`, floor `2`) bounds the table size
(the oldest half is dropped when exceeded). A delivery stays remembered only while its review is (or
ended up) in flight: a delivery whose review *failed to start* is forgotten again, so GitHub's
"Redeliver" button (which reuses the same delivery id) can retry it. This is only a fast-path that
suppresses re-dispatch of a redelivery landing on the *same* worker; the authoritative, cross-worker
duplicate-review guard is the coding-team `POST /review-pr` endpoint, which serializes admission (a
process lock plus, when Postgres is configured, a `pg_advisory_xact_lock` keyed on the PR) and rejects
a second review while one is already running for the PR (and also covers the manual UI trigger). A
review job whose worker died mid-review stops blocking new reviews once its liveness heartbeat is
stale (~5 minutes).

### GITHUB_WEBHOOK_SECRET_CACHE_TTL_S
How long (seconds, default `30`, floor `0` = disable caching) the unified API caches the GitHub
webhook signing secret between reads. The webhook endpoint verifies every delivery — including pings
and event types it ignores — and each uncached read opens a fresh Postgres connection, a cost model
meant for config-page traffic, not per-delivery volume. Saving or clearing the GitHub integration
invalidates the cache immediately on the worker that handled it; other workers converge within the
TTL. Store-outage results are cached too (the route fails closed with 503 either way, and caching
keeps a delivery storm from hammering a database that is already down).

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

### CODING_TEAM_REVIEW_CONCURRENCY
Maximum number of Tech Lead `run_code_review` LLM calls the coding-team orchestrator dispatches
concurrently within a single review round (default `4`; garbage/empty → default; floored at `1`).
Each round's reviews are independent (a read-only branch diff plus an LLM call), so a round with `k`
tasks in review fans the reviews out on a thread pool and costs roughly one review latency instead of
`k`; the effective pool width is `min(this, number of tasks in review)`. A round with a single task
in review runs inline (keeping its live per-task progress bar). The merge/revision decisions are
always applied serially in the original order, so git writes and task-graph mutations stay
single-threaded (deterministic merge ordering and branch isolation preserved).

### CODING_TEAM_NO_CHANGE_REVISIT_CAP
Number of consecutive **no-change** revision rounds the coding team tolerates on a single task
before handing it to the Tech Lead for direction (default `3`; garbage/empty → default; floored at
`1` so the guard can never be disabled). A no-change round is one where the engineer revisits a task
it already flagged done but produces no change to the task's branch diff — the "is it done? maybe
not, let me look again" loop. This is distinct from `MAX_TASK_REVISIONS` (20): a revision that
actually changes the code resets this counter, so productive work keeps its full revision budget;
only zero-progress re-evaluation is bounded. On reaching the cap the Tech Lead adjudicates the
stalled task — `done` (mark it resolved with no diff), `fail` (terminal), or `continue` (a fresh
window). When the whole issue's work is already complete, the job ends with the terminal status
`already_complete` and the `run-from-github` flow comments a closure recommendation on the issue
instead of opening a no-op PR.

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
tools — all routed through `backend/shared/git/git_utils.py`). Default `Khala`. Blank
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

## DevOps Tool-Agent Subprocess Timeouts

Named, per-tool timeout constants for the devops team's stateless tool-agents (`shared/subprocess_timeouts.py`),
each parsed via the shared `env_int` with `floor=1` (unset or unparseable → the documented default, with a
warning on a set-but-unparseable value; a parsed value below `1` is clamped to `1`, not replaced by the
default). These are intended to replace the hardcoded `timeout=` literals in the individual tool-agent
files; that call-site migration is tracked as a separate follow-up and has not landed yet, so setting
one of these variables today has no effect until that migration lands.

### DEVOPS_HELM_DRY_RUN_TIMEOUT_S
Timeout (seconds) for `helm lint .` in the deployment dry-run tool-agent. Default `120`, floor `1`.

### DEVOPS_TERRAFORM_EXECUTION_TIMEOUT_S
Timeout (seconds) for `terraform init`/`validate`/`plan`/`apply`/`fmt` (the only commands
`TerraformExecutionInput` accepts) in the terraform execution tool-agent. Default `180`, floor `1`.

### DEVOPS_HELM_EXECUTION_TIMEOUT_S
Timeout (seconds) for `helm template`/`lint` — the only commands `HelmExecutionInput` accepts; this
tool-agent is read-only and cannot install/upgrade a release — in the helm execution tool-agent.
Default `120`, floor `1`.

### DEVOPS_CDK_EXECUTION_TIMEOUT_S
Timeout (seconds) for `cdk synth`/`cdk diff` — the only commands `CDKExecutionInput` accepts; this
tool-agent is read-only and cannot deploy a stack — in the CDK execution tool-agent. Default `180`,
floor `1`.

### DEVOPS_IAC_VALIDATION_TIMEOUT_S
Timeout (seconds) for both `terraform fmt -check` and `terraform validate` in the IaC validation
tool-agent. Default `120`, floor `1`.

### DEVOPS_DOCKER_COMPOSE_TIMEOUT_S
Timeout (seconds) for `docker compose config`/`build`/`ps`/`logs` — the only commands
`DockerComposeExecutionInput` accepts; this tool-agent is read-only and cannot bring services up or
down — in the docker compose execution tool-agent. Default `120`, floor `1`.

### DEVOPS_POLICY_AS_CODE_TIMEOUT_S
Timeout (seconds) for the checkov scan in the policy-as-code tool-agent. Default `180`, floor `1`.

### DEVOPS_ARCHITECT_INTEGRATION_TIMEOUT_S
Timeout (seconds) for the `architect_agents/main.py` subprocess invoked from the enterprise architect
integration helper. Default `3600` (1 hour), floor `1`.

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
Path to the job-matching team's job-seeker profile YAML (standing search criteria). This explicit
pin is the highest-priority source. When unset, resolution falls back to the **career section of
the central user profile** (`user_profiles.profile_json["career"]`, written by
`PUT /api/job-matching/profile`), then `$AGENT_CACHE/job_seeker_profile.yaml`, then the bundled
example. See `backend/agents/job_matching_team/profile/`.

### JOB_SEEKER_PROFILE_STRICT
When `true`, a missing `JOB_SEEKER_PROFILE_PATH` file raises instead of falling back, and the
bundled-example fallback is disabled (a stored career section still satisfies resolution).

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

---

## Planning

### PLANNING_SERVICE_URL
Base URL the unified API proxies `/api/planning/*` requests to when the Planning team
runs as its own service (Docker/production). Unset in local dev, where the team runs
in-process. See the analogous `<TEAM>_SERVICE_URL` entries for other teams.

### PLANNING_SOFTWARE_ENGINEERING_URL / PLANNING_MARKET_RESEARCH_URL / PLANNING_AI_SYSTEMS_URL
Per-adapter base-URL overrides the Planning team uses when calling the Product
Requirements Analysis (SE), Market Research, and AI Systems adapters, respectively.
Each falls back to `UNIFIED_API_BASE_URL` when unset, so these only need to be set
when an adapter's target team is reachable at a different address than the unified API
(e.g. hitting a team's standalone service directly).

### TEMPORAL_TASK_QUEUE_PLANNING
Temporal task queue name the Planning worker polls and the API dispatches workflows to
when Temporal mode is enabled (`TEMPORAL_ADDRESS` set). Default `planning`.

### PLANNING_MANY_SECTIONS_WARN
Soft threshold for the spec-digestion engine (`planning_team/spec_digest.py`): when a
brief+spec splits into more than this many sections, `map_reduce` logs a warning (one LLM call
runs per section, so a very large spec has a proportional cost/latency). Observability only — it
never caps or drops sections (that would discard spec content). Default `50`; garbage or
non-positive values fall back to the default.

### PLANNING_RESERVED_PROMPT_TOKENS / PLANNING_RESERVED_RESPONSE_TOKENS
Token reserves the spec-digestion engine carves out of the model context before sizing each
per-section prompt — for the phase prompt template/headers and the model's response,
respectively. Defaults `6000` / `4096`; raise them for prompt-heavy phases or models that
need more response headroom. Garbage or non-positive values fall back to the defaults.

### PLANNING_MAP_PARALLELISM
Max concurrent per-section map-phase LLM calls in the spec-digestion engine
(`planning_team/spec_digest.py`'s `map_reduce`). Default `4`, floor `1` (`1` runs sections
sequentially). Results merge in deterministic (section) order regardless of completion order.
