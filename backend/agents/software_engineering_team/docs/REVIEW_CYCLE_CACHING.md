# Review-Cycle Provider-Side Caching

## Summary

The three review gates (Code Review, QA, Security) share a stable file-context
prefix — the language label and code under review — across every call in a single
review cycle. When the backing LLM provider supports prompt caching (currently
Anthropic Claude via `cache_control: {"type": "ephemeral"}`), this shared prefix is
billed once (on the first call in the cycle) and served from the provider's cache on
all subsequent calls that share the same byte-identical prefix. On retry cycles
(QA/Security failure → batch-fix → restart from Code Review), the prefix often
remains cache-warm because the fixed files still share significant content overlap
with the prior cycle's request.

## How it works

### Cache-breakpoint marking

The `CacheBreakpoint` marker (`llm_service/cache_breakpoint.py`) is a frozen
dataclass that wraps a stable prompt segment. It carries no provider logic — it
simply declares "this text is safe to cache." The wire translation happens downstream:

1. **Code Review** (`code_review_agent/chunk_reviewer.py`): wraps the spec excerpt,
   architecture overview, and existing-codebase excerpt in a `CacheBreakpoint` and
   passes it as `system_prompt_content` on the reasoning Agent. These are trusted
   metadata — identical across every chunk in the coordinator's map phase.

2. **QA and Security** (`qa_agent/agent.py`, `security_agent/agent.py`): the file
   context (language + code under review) stays in the **user message** — it is
   untrusted repository content and must not be elevated to system-level
   instructions. Provider-side caching still applies because the entire request
   prefix (system prompt persona + user-message file context) is byte-identical
   across calls with the same code under review.

3. **Wire translation** (`llm_service/strands_adapter.py`,
   `llm_service/clients/claude.py`): the Strands model wrapper recognizes
   `CacheBreakpoint` instances in `system_prompt_content` and passes them to
   `ClaudeLLMClient`, which renders them as Anthropic `cache_control` blocks. For
   non-caching clients (e.g. `DummyLLMClient`), breakpoints are flattened to plain
   text — no error, byte-identical behavior.

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

1. **Code Review reasoning pass**: creates the provider cache for the shared
   spec/architecture prefix (`cache_creation_tokens > 0`, `cache_read_tokens == 0`
   on a cold start).
2. **Code Review formatting pass**: reads back the cached prefix
   (`cache_read_tokens > 0`).
3. **QA call**: the user-prompt file-context prefix is byte-identical to what Code
   Review reviewed — the same `microtask_files` content. Provider serves it from
   cache (`cache_read_tokens > 0`).
4. **Security call**: same byte-identical prefix as QA — also served from cache
   (`cache_read_tokens > 0`).

On a retry cycle (QA or Security failure triggers a fix and restart from Code
Review), the shared prefix often remains cache-warm because:
- The spec/architecture metadata in Code Review's system prompt is unchanged.
- The file-context prefix in QA/Security's user message may be partially or fully
  unchanged (a batch-fix touches only the files with reported issues; untouched
  files retain their byte-identical content).

### What is NOT cached across gates

- **LLM responses**: only the *input* prefix is cached. The model still generates a
  fresh response for each call.
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

1. **Cross-gate caching** (`test_qa_and_security_show_nonzero_cache_read_after_code_review`):
   QA and Security calls following Code Review in the same cycle show non-zero
   `cache_read_tokens`.

2. **Cross-retry caching** (`test_code_review_retry_shows_nonzero_cache_read`,
   `test_qa_retry_shows_nonzero_cache_read`,
   `test_security_retry_shows_nonzero_cache_read`): on a second cycle with
   identical input, all three gates read from the provider cache.

3. **Output stability** (`test_code_review_output_unchanged_regardless_of_cache_state`,
   `test_qa_output_unchanged_regardless_of_cache_state`,
   `test_security_output_unchanged_regardless_of_cache_state`): gate outputs are
   byte-identical regardless of whether the call was cache-served or not.

4. **Structural invariant** (`test_shared_file_context_text_is_byte_identical_across_gates`):
   QA and Security render the same file-context prefix text for the same input —
   the precondition for a provider cache hit.

## Cost impact

For a typical review cycle with a 3000-token file-context prefix:
- Without caching: 3000 input tokens billed × 4 calls = 12000 input tokens.
- With caching: 3000 tokens billed once (creation) + 3000 tokens × 3 reads at the
  cached-input discount (typically 90% cheaper on Anthropic) = 3000 + 900 = 3900
  effective input tokens. A ~68% reduction in input-token cost for the shared
  prefix portion.

The savings compound with retries: each additional cycle adds only the discounted
cache-read cost, not the full prefix cost.
