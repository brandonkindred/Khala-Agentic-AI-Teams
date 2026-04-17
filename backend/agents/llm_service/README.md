# Central LLM service

Single backend LLM layer used by all agent teams. Provides a provider-agnostic interface and factory so teams request completions through one place; context/config and provider logic (Ollama, dummy, future OpenAI/Anthropic) live here.

## Usage

```python
from llm_service import get_client, LLMClient, LLMError

# Default client (uses LLM_MODEL / LLM_PROVIDER)
client = get_client()

# Per-agent client (uses LLM_MODEL_<agent_key> when set, else agent default)
client = get_client("backend")
client = get_client("personal_assistant")

# Interface
data = client.complete_json(prompt, temperature=0.0, system_prompt="...")
text = client.complete(prompt, temperature=0.0, max_tokens=4096)
max_ctx = client.get_max_context_tokens()
```

## When to use which entrypoint

New code should prefer the top-level helpers — they make the output contract
explicit at the call site and eliminate the class of "Markdown prompt routed
through a JSON parser" bugs that motivated
[FEATURE_SPEC_structured_output_contract.md](FEATURE_SPEC_structured_output_contract.md).

```python
from llm_service import generate_text, generate_structured
from pydantic import BaseModel

# Free-form prose / Markdown / code — never JSON-parsed.
spec_md = generate_text(prompt, system_prompt=PERSONA, agent_key="user_agent_founder")

# Typed structured output — JSON mode + one self-correction retry applied automatically.
class Answer(BaseModel):
    selected_option_id: str
    rationale: str

answer = generate_structured(prompt, schema=Answer, agent_key="user_agent_founder")
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
| `LLM_PROVIDER` | `dummy` or `ollama` |
| `LLM_MODEL` | Model name |
| `LLM_MODEL_<agent_key>` | Per-agent model override |
| `LLM_BASE_URL` | Ollama base URL (default `https://ollama.com`) |
| `LLM_TIMEOUT` | Request timeout in seconds (default 900 / 15 min; all calls use streaming) |
| `LLM_CONTEXT_SIZE` | Override context size |
| `LLM_MAX_TOKENS` | Max output tokens |
| `LLM_MAX_RETRIES` | Retries for transient errors |
| `LLM_BACKOFF_BASE` | Backoff base (seconds) |
| `LLM_BACKOFF_MAX` | Max backoff (seconds) |
| `LLM_MAX_CONCURRENCY` | Max concurrent Ollama calls |
| `LLM_ENABLE_THINKING` | Enable thinking for qwen3.5 |
| `OLLAMA_API_KEY` | **Required for Ollama Cloud.** API key from https://ollama.com/settings/keys. All LLM requests use this when set. |

### Troubleshooting

**ConnectErrors / timeouts**

- **Docker:** If the app runs in Docker, the container may not resolve `ollama.com` or reach the internet. Set `LLM_BASE_URL` to a reachable host (e.g. `http://host.docker.internal:11434` for local Ollama, or ensure the container has outbound HTTPS and DNS for `https://ollama.com`).
- **Ollama Cloud:** Ensure `OLLAMA_API_KEY` is set (from https://ollama.com/settings/keys). If you get 401, the key is missing or invalid.
- **Firewall / proxy:** Ensure the host (or container) can open HTTPS to your `LLM_BASE_URL`.

**500 Internal Server Error from Ollama Cloud**

- **Thinking mode:** With `qwen3.5:397b-cloud`, the client may send `think: true`, which some endpoints reject. Set `LLM_ENABLE_THINKING=false` and retry.
- **Quota / capacity:** Check your Ollama Cloud account and https://status.ollama.com (or Ollama’s status page) for outages or rate limits.
- **Model / size:** Try a smaller model (e.g. `LLM_MODEL=qwen3.5:8b-cloud`) or reduce prompt size to rule out server-side overload.

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

When `LLM_MODEL_<agent_key>` and `LLM_MODEL` are unset, `config.AGENT_DEFAULT_MODELS` is used (e.g. `backend` → `qwen3.5:397b-cloud`). See `config.py`.

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
- `LLMRateLimitError` – 429 after retries
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
