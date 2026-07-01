# Central LLM service

Single backend LLM layer used by all agent teams. Provides a provider-agnostic interface and factory so teams request completions through one place; context/config and provider logic live here.

## Providers

The Postgres-backed **provider list is the sole source of LLM resolution** (see below). `LLM_PROVIDER=dummy` is the only override — a hard-coded no-LLM harness that pre-empts the list.

| Provider | Client | Notes |
|----------|--------|-------|
| `ollama` | `OllamaLLMClient` | Local (`base_url=http://host:11434`) or Ollama Cloud (`https://ollama.com`, entry carries its own key). |
| `claude` | `ClaudeLLMClient` | Uses the **official `anthropic` Python SDK** (streaming + `get_final_message()`). Default model `claude-opus-4-8`; entry carries its own key. Adaptive thinking + `output_config.effort`; never sends `temperature`/`top_p`. |
| `dummy` | `DummyLLMClient` | Heuristic stub for tests; also a Strands `Model`. `LLM_PROVIDER=dummy` pre-empts the list. |

### Provider list (settings UI) — the sole source of LLM resolution

The settings UI manages an **ordered list of provider entries** (`GET/POST/PUT/DELETE /api/llm-config/providers` + `PUT /api/llm-config/providers/order`), persisted in the dedicated `llm_provider_configs` table by `llm_service.provider_store`. All containers share the Fernet key (`INTEGRATION_ENCRYPTION_KEY` env, or `$AGENT_CACHE/integration.key` on the shared volume) and the same Postgres, so every team container reads the list back with **no dependency on `unified_api`**. Entries are ordered most→least preferred and are **self-contained**: each carries its own provider/model/base URL and its **own API key** — there is no environment fallback for keys (a keyless Claude / Ollama-Cloud entry is rejected at write time). Blank non-secret fields (model/base URL) fall back to provider defaults via the shared resolvers.

`get_client` resolves the active provider by loading the ordered list (`provider_store.load_ordered_entries`) and selecting with `provider_store.select_active_entry`: the first entry that is **not** usage-limited wins; an entry whose `reset_at` has passed is reset and used; when **all** are limited the entry with the soonest reset time is returned — the `FailoverLLMClient` still attempts the call on it, and if that 429s it marks the entry and tries the next provider, ultimately re-raising the last `LLMRateLimitError` once every provider is exhausted. `get_client` returns a `FailoverLLMClient` (wrapped in `_AttributingClient` for a keyed call) that, on an `LLMRateLimitError`, marks the current entry exhausted (computing `reset_at` from the 429's `Retry-After` or a fallback window, with a lightweight `weekly`/`rate` `limit_type`) and retries the **same** call on the next available provider. `unwrap_client` deliberately stops at the `FailoverLLMClient` (peeling only the attribution wrapper) so the Strands adapter's `unwrap_client(client).chat` dispatch still routes through failover.

When the list is empty (or Postgres unset) and the provider is not `dummy`, `get_client` raises **`LLMNotConfiguredError`** — there is no legacy single-provider env fallback. In an agent run this fails the job; the Angular UI shows a "No LLMs configured" dialog whose "Setup LLM" button routes to `/llm-config`. Latency/window tuning: `LLM_FAILOVER_FAST_429`, `LLM_FAILOVER_RATE_WINDOW_S`, `LLM_FAILOVER_WEEKLY_WINDOW_S` (see `docs/ENV_VARS.md`).

> **Migration note:** any pre-existing `llm_provider_configs` row that previously relied on the env key fallback (a Claude / Ollama-Cloud entry with an empty stored key) must have its key re-entered — such an entry now fails at call time with an auth error rather than pulling `ANTHROPIC_API_KEY` / `OLLAMA_API_KEY` from the environment.

## Usage

```python
from llm_service import get_client, LLMClient, LLMError

# Default client (uses LLM_MODEL / LLM_PROVIDER)
client = get_client()

# Per-agent client (uses LLM_MODEL_<agent_key> when set, else agent default)
client = get_client("backend")
client = get_client("personal_assistant")

# Interface — every generation call must declare an `objective` (the WHY)
data = client.complete_json(prompt, objective="rank job candidates", temperature=0.0)
text = client.complete(prompt, objective="draft email reply", temperature=0.0, max_tokens=4096)
max_ctx = client.get_max_context_tokens()
```

## Request attribution in logs

Every LLM call is attributable in the logs and telemetry to **which agent** made
it, **why** (its objective), and a per-call **request id** that ties the request,
completion, retry, and error lines of a single call together.

- **`agent_key`** is bound automatically: `get_client("ranker")` returns a thin
  wrapper that stamps that agent identity onto every call — no call site has to
  pass it. When no key is supplied (e.g. `get_strands_model()` without one), the
  Strands adapter fills the field with a path-derived identity (the calling
  agent's package directory) rather than leaving it empty.
- **`objective`** is a **required** keyword on `complete_json` / `complete` /
  `complete_text` / `chat` (and `generate_text` / `generate_structured` /
  `complete_validated`). Pass a short phrase describing the purpose.
- **`request_id`** is generated per call and printed as `rid=` on the request,
  completion, retry, and error log lines.
- **`team`** is auto-derived from the calling code's source path (the
  `backend/agents/<team>/` directory that owns the frame), so it is populated for
  every call without per-team wiring. An orchestrator may still set it explicitly
  via `llm_attribution(team=...)` to override the derived value (e.g. to use a
  canonical slug); an explicit value always wins.

```python
from llm_service import llm_attribution, get_client

with llm_attribution(team="job_matching", objective="match candidate to roles"):
    client = get_client("ranker")                     # agent_key auto-bound
    client.complete_json(prompt, objective="rank job candidates")
```

A request log line then reads:

```
LLM request: rid=8f3a1c2b9d4e agent=ranker team=job_matching objective=rank job candidates caller=ranker.agent.rank provider=ollama model=... think=...
```

The same `rid=` appears on that call's completion line and on any retry/error
lines, and `agent_key` / `team` / `objective` / `request_id` are recorded in
telemetry (`record_llm_call`) and emitted as OpenTelemetry span attributes
(`khala.agent_key`, `khala.team`, `khala.objective`, `khala.request_id`).

> Thread note: `agent_key` and `objective` are always correct (bound at call
> time / passed as an argument). An *outer* `team`/`objective` propagates across
> `asyncio.to_thread` (the Strands path) automatically, but not across a raw
> `ThreadPoolExecutor.submit`; for raw-thread fan-out, propagate with
> `contextvars.copy_context()` (see `shared_concurrency/heartbeat.py`).

Use `unwrap_client(client)` when you need the concrete provider client (e.g. an
`isinstance(c, OllamaLLMClient)` check), since `get_client` returns the wrapper
for keyed clients.

## When to use which entrypoint

New code should prefer the top-level helpers — they make the output contract
explicit at the call site and eliminate the class of "Markdown prompt routed
through a JSON parser" bugs that motivated
[FEATURE_SPEC_structured_output_contract.md](FEATURE_SPEC_structured_output_contract.md).

```python
from llm_service import generate_text, generate_structured
from pydantic import BaseModel

# Free-form prose / Markdown / code — never JSON-parsed.
spec_md = generate_text(
    prompt, objective="draft startup spec", system_prompt=PERSONA, agent_key="user_agent_founder"
)

# Typed structured output — JSON mode + one self-correction retry applied automatically.
class Answer(BaseModel):
    selected_option_id: str
    rationale: str

answer = generate_structured(
    prompt, schema=Answer, objective="answer founder question", agent_key="user_agent_founder"
)
```

Rule of thumb:

| Use | When |
|-----|------|
| `generate_text` | The response is prose, Markdown, code, or any free-form text. |
| `generate_structured` | The response must conform to a schema. Caller gets a validated Pydantic instance; single-shot parse/validation failures are auto-corrected once. |

The legacy methods (`client.complete`, `client.complete_text`,
`client.complete_json`, `client.chat_json_round`) remain fully supported for
existing callers — no migration is required. See
[FEATURE_SPEC_structured_output_contract.md](FEATURE_SPEC_structured_output_contract.md)
for the design rationale and migration notes.

## Config (environment variables)

| Variable | Meaning |
|----------|---------|
| `LLM_PROVIDER` | Only `dummy` is load-bearing (the no-LLM harness, pre-empts the list); other values just mean "not dummy" → use the provider list |
| `LLM_MODEL` | Default model for a provider-list entry whose `model` is blank |
| `LLM_MODEL_<agent_key>` | Per-agent default model (applies to a blank entry model) |
| `LLM_BASE_URL` | Default Ollama base URL for a provider-list entry whose `base_url` is blank |
| `LLM_RUNTIME_CONFIG_TTL_S` | TTL (seconds, default 30) for the runtime cache backing the entry-default resolvers |
| `LLM_TIMEOUT` | Request timeout in seconds (default 900 / 15 min; all calls use streaming) |
| `LLM_CONTEXT_SIZE` | Override context size |
| `LLM_MAX_TOKENS` | Max output tokens |
| `LLM_MAX_RETRIES` | Retries for **transient** (5xx / network) errors only — not 429 (default 10) |
| `LLM_BACKOFF_BASE` | Transient backoff base in seconds (default 2) |
| `LLM_BACKOFF_MAX` | Transient max backoff in seconds (default 120) |
| `LLM_RATE_LIMIT_MAX_RETRIES` | Retries for HTTP **429** rate limits before raising `LLMRateLimitError` (default 5 → 6 total attempts) |
| `LLM_RATE_LIMIT_BACKOFF_INITIAL` | First 429 retry wait in seconds — a 429 means the budget is exhausted, so the first retry is minutes out (default 300) |
| `LLM_RATE_LIMIT_BACKOFF_MAX` | Per-wait cap for the 429 schedule in seconds (default 3600) |
| `LLM_RATE_LIMIT_HONOR_RETRY_AFTER` | Honor an integer-seconds `Retry-After` header on a 429 (`max(computed, Retry-After)`, capped); default on, set `false` to disable |
| `LLM_MAX_CONCURRENCY` | Max concurrent Ollama calls |
| `LLM_ENABLE_THINKING` | Enable thinking mode (some Ollama Cloud models reject `think: true`; disable if you see 500s) |
| `OLLAMA_API_KEY` | **Required for Ollama Cloud.** API key from https://ollama.com/settings/keys. All LLM requests use this when set. |

### Troubleshooting

**ConnectErrors / timeouts**

- **Docker:** If the app runs in Docker, the container may not resolve `ollama.com` or reach the internet. Set `LLM_BASE_URL` to a reachable host (e.g. `http://host.docker.internal:11434` for local Ollama, or ensure the container has outbound HTTPS and DNS for `https://ollama.com`).
- **Ollama Cloud:** Ensure `OLLAMA_API_KEY` is set (from https://ollama.com/settings/keys). If you get 401, the key is missing or invalid.
- **Firewall / proxy:** Ensure the host (or container) can open HTTPS to your `LLM_BASE_URL`.

**500 Internal Server Error from Ollama Cloud**

- **Thinking mode:** Some Ollama Cloud models (e.g. `qwen3.5:397b-cloud`) reject `think: true` and return 500s. Set `LLM_ENABLE_THINKING=false` and retry.
- **Quota / capacity:** Check your Ollama Cloud account and https://status.ollama.com (or Ollama’s status page) for outages or rate limits.
- **Model / size:** Try a smaller model or reduce prompt size to rule out server-side overload.

### Docker and name resolution

If the app runs inside Docker, the default `LLM_BASE_URL` (`https://ollama.com`) may be unreachable (e.g. "Temporary failure in name resolution") if the container has no outbound DNS or network. Set `LLM_BASE_URL` to a reachable endpoint:

- **Local Ollama on the host:** `http://host.docker.internal:11434` (Mac/Windows Docker Desktop) or the host’s LAN IP and port (e.g. `http://192.168.1.2:11434`).
- **Ollama in another container:** Use the Docker service name and port (e.g. `http://ollama:11434`) and ensure both containers share a network.
- **Ollama Cloud:** Use `https://ollama.com` only if the container has outbound HTTPS and DNS; otherwise run Ollama locally and point `LLM_BASE_URL` at it as above.

**Legacy mapping (same behavior via central config):**

- `BLOG_LLM_*` → use `LLM_*` or `LLM_MODEL_blog`
- `SOC2_LLM_*` → use `LLM_*` or `LLM_MODEL_soc2`

## Known model context sizes

Context size is resolved in this order: `LLM_CONTEXT_SIZE` env, then known-model table, then (for Ollama) `/api/show`. The known-model table in `config.py` includes e.g. `qwen3.5:397b`, `qwen3.5:397b-cloud`, `qwen3-coder:480b` at 262144 tokens.

## Per-agent default models

When `LLM_MODEL_<agent_key>` and `LLM_MODEL` are unset, `config.AGENT_DEFAULT_MODELS` is used (e.g. `backend` → `deepseek-v4-pro:cloud`). See `config.py`.

## Strands Agents adapter

New agents should prefer the [AWS Strands Agents SDK](https://strandsagents.com/) via the built-in adapter. `get_strands_model(agent_key)` returns a `strands.models.Model` backed by this package — the Strands `Agent` automatically inherits per-agent model routing, retries, telemetry, and the dummy-client path for tests.

```python
from llm_service import get_strands_model
from strands import Agent

model = get_strands_model(agent_key="qa_agent", temperature=0.1, think=True)
agent = Agent(model=model, system_prompt="You are a QA expert.")
result = agent("Review this diff: ...")
```

Under the hood the adapter:

- Calls `get_client(agent_key)` so every `LLM_MODEL_<agent_key>` / `LLM_PROVIDER` rule still applies.
- Converts Strands `Messages` (Bedrock-style `ContentBlock` lists) to the OpenAI chat shape expected by `LLMClient.chat_json_round`, including `toolUse` / `toolResult` handoffs.
- Runs the blocking `LLMClient` call inside `asyncio.to_thread` so Strands' async event loop is never blocked.
- Replays the single-shot response as a minimal Strands stream: `messageStart → contentBlock(text|toolUse) → messageStop` with `stopReason="tool_use"` when the LLM requests a tool.

In tests, inject a client directly (bypasses the factory cache):

```python
from llm_service import get_strands_model
from llm_service.clients.dummy import DummyLLMClient

model = get_strands_model(agent_key="test_agent", client=DummyLLMClient())
```

See `tests/test_strands_adapter.py` for message-conversion, tool-loop, and `structured_output` examples.

### Migration rule: keep pattern anchors in the **user** prompt

`DummyLLMClient.complete_json` routes to its canned stubs by scanning the **user** prompt only (not the Strands system prompt). When migrating an agent and moving its persona to `Agent(system_prompt=...)`, the user prompt you build in `_build_user_prompt` must still include the distinctive tokens the matching dummy branch looks for — e.g. `bugs_found` + `test_plan` for the QA branch, or `integration expert` + `backend code` + `frontend code` for the Integration branch. An explicit "produce JSON with fields: foo, bar, baz" schema hint in the user prompt usually satisfies this for free. This only affects dummy-client tests; real LLMs see both prompts.

## Exceptions

- `LLMError` – base
- `LLMRateLimitError` – 429 after the slow rate-limit backoff (300s initial, 3600s cap, 5 retries by default; separate from the transient schedule)
- `LLMTemporaryError` – 5xx / network after retries
- `LLMPermanentError` – 4xx (except 429)
- `LLMJsonParseError` – response not valid JSON
- `LLMTruncatedError` – finish_reason=length
- `LLMUnreachableAfterRetriesError` – all retries failed

## Adding a new provider

1. Implement `LLMClient` in `clients/<name>.py` (e.g. `clients/openai.py`).
2. In `config.py`, add provider resolution (e.g. `LLM_PROVIDER=openai`).
3. In `factory.py`, branch on provider and return the new client (and cache if needed).
4. No changes required in agent teams; they keep using `get_client(agent_key)`.

## Khala platform

This package is part of the [Khala](../../../README.md) monorepo (Unified API, Angular UI, and full team index).
