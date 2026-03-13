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

## Config (environment variables)

| Variable | Meaning | Backward compat |
|----------|---------|-----------------|
| `LLM_PROVIDER` | `dummy` or `ollama` | `SW_LLM_PROVIDER` |
| `LLM_MODEL` | Model name | `SW_LLM_MODEL` |
| `LLM_MODEL_<agent_key>` | Per-agent model override | `SW_LLM_MODEL_<agent_key>` |
| `LLM_BASE_URL` | Ollama base URL (default `https://ollama.com`) | `SW_LLM_BASE_URL` |
| `LLM_TIMEOUT` | Request timeout in seconds (default 120; blog agent default 300) | `SW_LLM_TIMEOUT` |
| `LLM_CONTEXT_SIZE` | Override context size | `SW_LLM_CONTEXT_SIZE` |
| `LLM_MAX_TOKENS` | Max output tokens | `SW_LLM_MAX_TOKENS` |
| `LLM_MAX_RETRIES` | Retries for transient errors | `SW_LLM_MAX_RETRIES` |
| `LLM_BACKOFF_BASE` | Backoff base (seconds) | `SW_LLM_BACKOFF_BASE` |
| `LLM_BACKOFF_MAX` | Max backoff (seconds) | `SW_LLM_BACKOFF_MAX_SECONDS` |
| `LLM_MAX_CONCURRENCY` | Max concurrent Ollama calls | `SW_LLM_MAX_CONCURRENCY` |
| `LLM_ENABLE_THINKING` | Enable thinking for qwen3.5 | `SW_LLM_ENABLE_THINKING` |
| `OLLAMA_API_KEY` | **Required for Ollama Cloud.** API key from https://ollama.com/settings/keys. All LLM requests use this when set. | `LLM_OLLAMA_API_KEY`, `SW_LLM_OLLAMA_API_KEY` (overrides) |

### Docker and name resolution

If the app runs inside Docker, the default `LLM_BASE_URL` (`https://ollama.com`) may be unreachable (e.g. "Temporary failure in name resolution") if the container has no outbound DNS or network. Set `LLM_BASE_URL` (or `SW_LLM_BASE_URL`) to a reachable endpoint:

- **Local Ollama on the host:** `http://host.docker.internal:11434` (Mac/Windows Docker Desktop) or the host’s LAN IP and port (e.g. `http://192.168.1.2:11434`).
- **Ollama in another container:** Use the Docker service name and port (e.g. `http://ollama:11434`) and ensure both containers share a network.
- **Ollama Cloud:** Use `https://ollama.com` only if the container has outbound HTTPS and DNS; otherwise run Ollama locally and point `LLM_BASE_URL` at it as above.

**Legacy mapping (same behavior via central config):**

- `SW_LLM_*` → read when `LLM_*` unset (software engineering / shared)
- `BLOG_LLM_*` → use `LLM_*` or `LLM_MODEL_blog`
- `SOC2_LLM_*` → use `LLM_*` or `LLM_MODEL_soc2`

## Known model context sizes

Context size is resolved in this order: `LLM_CONTEXT_SIZE` env, then known-model table, then (for Ollama) `/api/show`. The known-model table in `config.py` includes e.g. `qwen3.5:397b`, `qwen3.5:397b-cloud`, `qwen3-coder:480b` at 262144 tokens.

## Per-agent default models

When `LLM_MODEL_<agent_key>` and `LLM_MODEL` are unset, `config.AGENT_DEFAULT_MODELS` is used (e.g. `backend` → `qwen3.5:397b-cloud`). See `config.py`.

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
