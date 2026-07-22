# Migrate compliance/fact-check agents to shared JSON-retry helper

**Date:** 2026-07-22  
**Status:** Approved for implementation planning

## Goal

Replace the duplicated 2-attempt JSON-retry loops in `blog_compliance_agent`
and `blog_fact_check_agent` with the shared `call_json_with_retry()` helper,
preserving each agent's exception-classification and fallback behavior.

## Motivation

Both gate agents hand-roll the same pattern: invoke a Strands `Agent`, parse
JSON, retry once on `LLMJsonParseError` with a stricter prompt, re-raise
transient LLM errors, and handle unexpected failures differently (compliance
fails closed; fact-check raises `FactCheckError`). The shared helper in
`agents.blogging.shared.json_retry` already encodes that policy.

## Decisions (locked)

| Decision | Choice |
|---|---|
| Migration style | Direct call-site replacement (no new gate wrapper) |
| Attempt-1 prompts | Keep identical by folding the always-on JSON instruction into the initial `prompt` arg |
| Retry suffix order | Accept helper's `prompt + strict_json_suffix` order (content preserved; order may differ from today) |
| `max_attempts` | `2` for both (helper default; remove `_MAX_JSON_RETRIES`) |
| Agent reuse | Default `fresh_agent_per_attempt=False` (one `Agent` per `run`) |
| Compliance unexpected / exhausted | `on_exhausted` and `on_unexpected_error` return `_fallback_compliance_report(e).to_dict()` |
| Fact-check exhausted | `on_exhausted` returns the existing FAIL fallback dict shape |
| Fact-check unexpected | Omit `on_unexpected_error`; wrap the raised cause in `FactCheckError` in an outer `try/except` |
| Transient errors | Rely on helper re-raise of `LLMRateLimitError` / `LLMTemporaryError` |
| Helper changes | None — out of scope |
| Other blogging agents | Out of scope |

## Architecture

### Files touched

| Path | Change |
|---|---|
| `backend/agents/blogging/blog_compliance_agent/agent.py` | Replace local retry loop with `call_json_with_retry`; trim unused imports |
| `backend/agents/blogging/blog_fact_check_agent/agent.py` | Same; remove `_MAX_JSON_RETRIES`; keep agent-specific CRITICAL suffix |
| Existing agent tests | Expect no changes unless a test asserts exact retry-prompt text |

### Call-site mapping

**Shared shape**

```text
prompt_for_helper = formatted_prompt + always_on_json_instruction
data = call_json_with_retry(
    agent_factory,          # lambda: Agent(model=..., system_prompt=...)
    prompt_for_helper,
    max_attempts=2,
    strict_json_suffix=<agent-specific>,
    on_exhausted=<...>,
    on_unexpected_error=<compliance only>,
    logger=logger,
)
# then existing dict → report model + optional artifact write
```

**Compliance**

- `strict_json_suffix` = existing `_JSON_RETRY_SUFFIX` (status/violations/required_fixes/notes keys reminder).
- Both failure hooks return `_fallback_compliance_report(e).to_dict()`.
- After the helper returns, always run the existing report-normalization +
  `write_artifact` path (including when the dict came from a fallback hook).
- Drop the unreachable `if not data` defensive branch (helper never returns `None`).

**Fact-check**

- `strict_json_suffix` = the existing CRITICAL invalid-JSON reminder string.
- `on_exhausted` builds the same FAIL `FactCheckReport` field dict used today
  (notes may say "after 2 attempts" instead of interpolating `_MAX_JSON_RETRIES`).
- Outer catch: any non-transient exception from the helper becomes
  `FactCheckError(f"Fact-check failed: {e}", cause=e) from e`. Transient
  errors must not be wrapped (re-raise as-is).

### Intentional non-semantic change

On retry, today's agents append suffixes in a different order than the helper.
Under the locked decision above, attempt-1 prompts stay identical; retry
prompts keep the same instructional content with possible suffix reordering.
No test currently asserts exact retry-prompt text.

## Error handling (preserved)

| Case | Compliance | Fact-check |
|---|---|---|
| JSON parse fails, retries left | Retry with strict suffix | Retry with CRITICAL suffix |
| JSON parse exhausted | FAIL fallback report | FAIL fallback report |
| `LLMRateLimitError` / `LLMTemporaryError` | Re-raise unwrapped | Re-raise unwrapped |
| Other exception | FAIL fallback report | Raise `FactCheckError` |

## Testing

- Keep existing coverage in `test_compliance.py`, `test_fact_check.py`,
  `test_more_agents.py`, and `test_more_coverage.py` green with no behavior
  change unless documented above.
- Run `make lint` clean for touched files.
- Hold the 90% line-coverage floor on modified modules.

## Out of scope

- Implementing or changing `call_json_with_retry` itself.
- Migrating plan-critic, copy-editor, ghost-writer, writer, or publication agents.
- Changing prompt templates, report schemas, or gate orchestration.
