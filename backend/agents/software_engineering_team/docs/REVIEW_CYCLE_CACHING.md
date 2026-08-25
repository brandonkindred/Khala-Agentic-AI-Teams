# Review-Cycle Provider-Side Caching

## Summary

The Code Review gate uses provider-side prompt caching to reduce input-token costs
on repeated calls with stable prefixes. The spec/architecture metadata is wrapped in
a `CacheBreakpoint` and sent as a `cache_control`-marked system-content block.
Anthropic caches this prefix across chunks within a single coordinator run and
across retry cycles.

QA and Security do not currently use explicit cache breakpoints. Their file context
is sent as unmarked user-message content. Any provider-side caching that occurs on
these gates is incidental (provider implementation detail) and not guaranteed by our
wire protocol.

## How it works

### Cache-breakpoint marking (Code Review)

The `CacheBreakpoint` marker (`llm_service/cache_breakpoint.py`) is a frozen
dataclass that wraps a stable prompt segment. It declares "this text is safe to
cache." The wire translation happens downstream:

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

### V2 tool-agent review path (security, testing/QA, accessibility, performance, UX): no cache opt-in

`shared/tool_agent_base.py::build_shared_tool_agent_review_system_content` exists
as the plumbing for a once-per-microtask `CacheBreakpoint` segment shared
across every wired tool agent's `review()` call (mirroring the two
established helpers above), but it always returns `None` today. Neither
field available to this call site clears the trust bar: `current_files` (the
reviewed code) is repository-controlled, and `task_description` can
originate directly from an externally-authored GitHub issue body (see
`github_source/issue_to_plan.py`), making it adversary-controllable the same
way. The `ToolAgentPhaseInput.shared_review_context` field and the
`system_prompt_content` threading through
`LlmToolAgentBase._invoke_llm`/`run_strands_agent` are kept in place for a
genuinely trusted, run-wide field that may become available to this call
site in the future -- not duplicated logic, an intentionally inert
placeholder pending one.

### QA and Security: no explicit cache opt-in

QA and Security keep the file-context prefix (language + code under review) in the
**user message** — it is untrusted repository content and must not be elevated to
system-level instructions. There is no explicit `CacheBreakpoint` marker on these
calls, and `ClaudeLLMClient` does not emit any `cache_control` block for them.

The file-context prefix is structurally stable (byte-identical for the same code
under review across retry cycles). If a future story adds a supported cache opt-in
for user-message content that preserves the trust boundary, QA/Security would
benefit from it.

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
Code Review (reasoning + formatting)  →  QA ──┐
     ↑                                        ├── join
     │                                Security ┘
     └───── batch-fix on QA/Security failure ←─────┘
```

In production, both backend and frontend `GATE_CONFIG`s set
`parallelize_qa_security=True`, so QA and Security run concurrently via
`parallel_map` against the same post-Code-Review snapshot. The sequential
fallback (`_qa_security_run_sequentially`) applies only when the backing LLM
client requires sequencing (e.g. `DummyLLMClient` in tests).

**Code Review caching (explicit breakpoint)**:
- Reasoning pass: the `CacheBreakpoint`-marked spec/architecture prefix is sent
  with `cache_control`. On the first call, Anthropic creates the cache
  (`cache_creation_tokens > 0`). On subsequent reasoning calls with the same
  prefix (across chunks and retries), Anthropic serves it from cache
  (`cache_read_tokens > 0`).

**QA/Security (no explicit cache opt-in)**:
- No `cache_control` is emitted on these gates' requests. Any non-zero
  `cache_read_tokens` reported by the provider reflects provider-internal
  optimizations, not behavior this codebase controls.
- Cross-gate: QA and Security have different system prompts (QA_PROMPT vs
  SECURITY_PROMPT), so they do NOT share a cache prefix with each other or
  with Code Review.

### What is NOT cached

- **Cross-gate prefix sharing**: Code Review, QA, and Security use different
  personas (system prompts), preventing cross-gate cache hits.
- **QA/Security explicitly**: these gates emit no `cache_control` blocks.
- **LLM responses**: only the *input* prefix is cached. The model generates a
  fresh response for each call.
- **Application-level result caches**: QA and Security have their own
  shared-cache-backed result caches (keyed on the full input hash + model
  fingerprint). These are a separate, higher-level mechanism — a cache hit there
  skips the LLM call entirely.
- **Cross-microtask caching**: microtasks within the same task share the same
  spec/architecture `review_context`, so their `CacheBreakpoint`-marked prefixes
  can be identical. If microtasks run within the 5-minute TTL and their marked
  system-content prefix is byte-identical, later microtasks may read the cache
  entry created by earlier ones. However, concurrent microtask execution and
  provider cache propagation latency make this non-deterministic.

## Verification

End-to-end tests in `tests/test_review_cycle_cache_e2e.py` verify:

1. **Wire-level Code Review cache opt-in** (`test_cross_gate_cache_telemetry_baseline`,
   `test_code_review_retry_shows_nonzero_cache_read`):
   - Code Review's system content carries `cache_control: {"type": "ephemeral"}`
     blocks on the wire.
   - Retry cycles send byte-identical system content (the precondition for a real
     provider cache hit).

2. **Telemetry propagation** (all tests): the telemetry pipeline faithfully records
   `cache_read_tokens` and `cache_creation_tokens` from the provider response,
   proving Story 2a's end-to-end data flow works.

3. **Output stability** (`test_code_review_output_unchanged_regardless_of_cache_state`,
   `test_qa_output_unchanged_regardless_of_cache_state`,
   `test_security_output_unchanged_regardless_of_cache_state`): gate outputs are
   byte-identical regardless of whether the call was cache-served or not.

4. **Structural invariant** (`test_shared_file_context_text_is_byte_identical_across_gates`):
   QA and Security render the same file-context prefix text for the same input,
   confirming both gates present the same code for review.

## Cost impact

**Code Review** (explicit breakpoint — realized savings on retries and sequential
calls):
The savings apply when a subsequent call reuses a prefix that an earlier call
already cached. In practice this means:
- **Retry cycles**: when the outer review loop re-invokes Code Review after a
  QA/Security failure, the spec/architecture prefix is already cached from the
  previous cycle — **provided the intervening batch-fix completes within
  Anthropic's 5-minute ephemeral-cache TTL**. Slow fixes that exceed the TTL
  invalidate the cache entry, and the next cycle pays the full creation cost again.
- **Sequential chunk pairs**: if a chunk finishes and its cache entry is still warm
  when the next chunk starts (within the same 5-minute TTL).

Note: the production coordinator fans out chunks concurrently via `parallel_map`
(default concurrency up to 8 in `code_review_agent/mapping.py`). In a concurrent
batch, all requests may start before the first cache entry is available, so
within-batch savings depend on timing and provider cache propagation latency. The
primary realized savings are on **retry cycles whose intervening fix completes
within the 5-minute TTL** and any sequential re-invocations within that window.

Example (retry cycle, fix within TTL): a 2000-token spec/architecture prefix with
Anthropic's ephemeral breakpoint pricing (1.25× input rate for the cache write,
0.1× for reads):
- Cycle 1 (write): 2000 × 1.25 = 2500 token-equivalents
- Cycle 2 (read): 2000 × 0.1 = 200 token-equivalents
- Total for two cycles: 2700 token-equivalents instead of 4000 (2× full price)

**QA/Security** (no explicit opt-in — potential future savings):
If a supported cache mechanism is added for QA/Security user-message content, the
structurally stable file-context prefix (~3000 tokens) would benefit from the same
discount model on retry cycles.
