# Review-Cycle Provider-Side Caching

## Summary

The review gates (Code Review, QA, Security) use provider-side prompt caching to
reduce input-token costs on repeated calls with identical prefixes. Two distinct
caching mechanisms are in play:

1. **Explicit cache breakpoints** (Code Review only): the spec/architecture
   metadata is wrapped in a `CacheBreakpoint` and sent as a `cache_control`-marked
   system-content block. Anthropic caches this prefix across chunks within a single
   coordinator run and across retry cycles.

2. **Automatic prefix caching** (all gates, within-gate retries): when a gate is
   re-invoked with byte-identical input (e.g. QA or Security retried after a fix
   that didn't change the code under review), the entire request is identical and
   Anthropic's automatic prefix matching serves it from cache.

**Important**: Code Review, QA, and Security use different system prompts
(personas), so there is no cross-gate cache hit at the system-prompt level. Each
gate's caching operates independently within its own retry/re-invocation cycles.

## How it works

### Cache-breakpoint marking (Code Review)

The `CacheBreakpoint` marker (`llm_service/cache_breakpoint.py`) is a frozen
dataclass that wraps a stable prompt segment. It carries no provider logic — it
simply declares "this text is safe to cache." The wire translation happens
downstream:

1. **Code Review** (`code_review_agent/chunk_reviewer.py`): wraps the spec excerpt,
   architecture overview, and existing-codebase excerpt in a `CacheBreakpoint` and
   passes it as `system_prompt_content` on the reasoning Agent. These are trusted
   metadata — identical across every chunk in the coordinator's map phase and
   across retry cycles.

2. **Wire translation** (`llm_service/strands_adapter.py`,
   `llm_service/clients/claude.py`): the Strands model wrapper recognizes
   `CacheBreakpoint` instances in `system_prompt_content` and passes them to
   `ClaudeLLMClient`, which renders them as Anthropic `cache_control: {"type":
   "ephemeral"}` blocks in the system message. For non-caching clients (e.g.
   `DummyLLMClient`), breakpoints are flattened to plain text — no error,
   byte-identical behavior.

### QA and Security: user-message file context

QA and Security keep the file-context prefix (language + code under review) in the
**user message** — it is untrusted repository content and must not be elevated to
system-level instructions. There is no explicit `CacheBreakpoint` marker on these
calls.

Provider-side caching for QA/Security works only in the **within-gate retry** case:
when the same gate is re-invoked with identical input (same system prompt + same
user message), Anthropic's automatic prefix matching serves the entire request from
cache. This happens when:
- A retry cycle restarts and the file context hasn't changed for that gate
- The same code is re-reviewed after a fix that didn't alter the reviewed files

### Telemetry

Every LLM call records `cache_read_tokens` and `cache_creation_tokens` on the
`LLMCallRecord` (`llm_service/telemetry.py`). These values come directly from the
Anthropic SDK's response `usage` object (`cache_read_input_tokens`,
`cache_creation_input_tokens`). The telemetry pipeline exposes them via:

- `telemetry.get_recent_calls()` — per-call dicts with `cache_read_tokens` and
  `cache_creation_tokens` fields.
- `telemetry.get_usage_summary()` — aggregated `total_cache_read_tokens` and
  `total_cache_creation_tokens`.
- OpenTelemetry spans — `llm.usage.cache_read_tokens` and
  `llm.usage.cache_creation_tokens` attributes.

### Review-cycle flow

In `_run_review_cycles` (`shared/phases/review_cycle.py`), per outer cycle:

```
Code Review (reasoning + formatting)  →  QA  →  Security
     ↑                                       |
     └───── batch-fix on QA/Security failure ←┘
```

**Code Review caching (explicit breakpoint)**:
- Reasoning pass: the `CacheBreakpoint`-marked spec/architecture prefix is sent
  with `cache_control`. On the first call, Anthropic creates the cache
  (`cache_creation_tokens > 0`). On subsequent reasoning calls with the same
  prefix (across chunks and retries), Anthropic serves it from cache
  (`cache_read_tokens > 0`).
- Formatting pass: may benefit from automatic prefix caching if the system
  prompt is identical.

**QA/Security caching (automatic prefix matching)**:
- Within-gate retries: when QA or Security is re-invoked with identical input
  (same `microtask_files` content), the entire request prefix matches and
  Anthropic serves from cache.
- Cross-gate: QA and Security have different system prompts (QA_PROMPT vs
  SECURITY_PROMPT), so they do NOT share a cache prefix with each other or
  with Code Review. Each gate's cache is independent.

### What is NOT cached across gates

- **Cross-gate prefix sharing**: Code Review, QA, and Security use different
  personas (system prompts), preventing cross-gate cache hits. Each gate only
  benefits from its own prior calls.
- **LLM responses**: only the *input* prefix is cached. The model still generates
  a fresh response for each call.
- **Application-level result caches**: QA and Security have their own
  shared-cache-backed result caches (keyed on the full input hash + model
  fingerprint). These are a separate, higher-level mechanism — a cache hit there
  skips the LLM call entirely. The provider-side prefix cache helps only when the
  application-level cache misses.
- **Cross-microtask caching**: each microtask's review cycle is independent. The
  provider cache is ephemeral (5-minute TTL on Anthropic) and may or may not be
  warm for the next microtask depending on timing and prefix overlap.

## Verification

End-to-end tests in `tests/test_review_cycle_cache_e2e.py` prove:

1. **Wire-level preconditions** (`test_qa_and_security_show_nonzero_cache_read_after_code_review`,
   `test_code_review_retry_shows_nonzero_cache_read`):
   - Code Review's system content carries `cache_control: {"type": "ephemeral"}`
     blocks on the wire.
   - Retry cycles send byte-identical system content (the precondition for a real
     provider cache hit).
   - QA/Security carry the shared file-context code in their user prompts.

2. **Telemetry propagation** (all AC1/AC2 tests): scripted fake responses with
   `cache_read_input_tokens` values are faithfully recorded by the telemetry
   pipeline, proving Story 2a's end-to-end data flow works.

3. **Within-gate retry caching** (`test_code_review_retry_shows_nonzero_cache_read`,
   `test_qa_retry_shows_nonzero_cache_read`,
   `test_security_retry_shows_nonzero_cache_read`): repeated calls with identical
   input produce cache hits (verified via both wire-level prompt identity and
   telemetry values).

4. **Output stability** (`test_code_review_output_unchanged_regardless_of_cache_state`,
   `test_qa_output_unchanged_regardless_of_cache_state`,
   `test_security_output_unchanged_regardless_of_cache_state`): gate outputs are
   byte-identical regardless of whether the call was cache-served or not.

5. **Structural invariant** (`test_shared_file_context_text_is_byte_identical_across_gates`):
   QA and Security render the same file-context prefix text for the same input.
   While this doesn't produce a cross-gate cache hit (different system prompts),
   it ensures both gates present the same code for review.

## Cost impact

**Code Review** (explicit breakpoint, most significant savings):
For a typical review with a 2000-token spec/architecture prefix reviewed across 5
chunks: without caching, 2000 × 5 = 10000 input tokens. With caching, 2000 billed
once + 2000 × 4 reads at the cached-input discount (90% cheaper on Anthropic) =
2000 + 800 = 2800 effective input tokens. ~72% reduction for the shared prefix
portion. Savings compound on retry cycles.

**QA/Security** (automatic prefix caching on retries):
When a gate is retried with unchanged code, the entire prompt (~3000 tokens) is
served from cache at the discounted rate. With 2-3 retry cycles per microtask, this
saves approximately 3000–6000 tokens at the cache discount per gate.
