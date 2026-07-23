# Design: Migrate ghost_writer JSON-retry call sites to shared helper

Date: 2026-07-23

## Goal

Replace the two ad hoc JSON-retry loops in `ghost_writer_agent` with the shared `call_json_with_retry()` helper so parse-retry / fallback policy lives in one place, matching the migrations already done for compliance, fact-check, plan-critic, and copy-editor.

## Scope

**In scope**

- `GhostWriterElicitationAgent._find_gaps_via_llm`
- `GhostWriterElicitationAgent._evaluate_sufficiency`
- Test updates for intentional helper semantics

**Out of scope**

- `_compile_narrative` — plain-text retry loop, not JSON; left unchanged (parent inventory incorrectly grouped it with the JSON sites)
- `_generate_friendly_seeds` — single-shot `extract_json_from_response`, no retry loop
- Shared helper implementation / signature changes
- Any other blogging agent

## Context

`agents.blogging.shared.json_retry.call_json_with_retry` already standardizes: invoke agent → `extract_json_from_response` → retry on `LLMJsonParseError` with a strict-JSON suffix → optional `on_exhausted` / `on_unexpected_error` fallbacks → re-raise transient LLM errors unwrapped for the caller’s retry layer.

Today’s ghost-writer loops duplicate that shape but also sleep+retry once on generic `Exception` before falling back. Sibling migrations adopted helper semantics (no local retry on non-parse errors) and updated tests accordingly.

## Decisions

| Topic | Choice |
|---|---|
| Approach | Direct `call_json_with_retry` at each site (no private wrappers; no helper generalization for arrays) |
| Sites migrated | `_find_gaps_via_llm`, `_evaluate_sufficiency` only |
| `_compile_narrative` | Unchanged |
| `max_attempts` | `2` (unchanged) |
| `strict_json_suffix` | Existing `_JSON_RETRY_SUFFIX` |
| `backoff_seconds` | Omit (parse retries already had no sleep) |
| Unexpected / exhausted errors | `on_exhausted` + `on_unexpected_error` → site-specific fallback |
| Transient LLM errors | Catch at call sites → site fallback (helper still re-raises; planning_stage would otherwise swallow) |
| Array JSON for gaps | Keep post-helper `isinstance(data, list)` check; no helper typing change |

### Intentional behavior deltas vs today

1. Generic non-parse exceptions no longer sleep(2) and retry locally — they fall back immediately via `on_unexpected_error`.
2. Transient LLM errors (`LLMRateLimitError` / `LLMTemporaryError`) are caught at these soft call sites and mapped to the same site fallbacks (`[]` / default dict). The helper still re-raises them; the call sites catch so `planning_stage`'s broad `except Exception` cannot abandon an in-progress story elicitation.

## Call-site design

### `_evaluate_sufficiency`

1. Build conversation text + prompt as today.
2. `agent_factory` → `Agent(model=self._model, system_prompt=system)` with the existing evaluator system string.
3. Invoke the helper inside a transient-error catch that maps to the shared default-dict fallback:

```python
try:
    data = call_json_with_retry(
        _agent_factory,
        prompt,
        max_attempts=2,
        strict_json_suffix=_JSON_RETRY_SUFFIX,
        on_exhausted=_default_sufficiency_fallback,
        on_unexpected_error=_default_sufficiency_fallback,
        logger=logger,
    )
except (LLMRateLimitError, LLMTemporaryError) as e:
    return _default_sufficiency_fallback(e)
if isinstance(data, dict):
    return data
return _default_sufficiency_fallback(ValueError("non-dict sufficiency payload"))
```

`_default_sufficiency_fallback` returns:

```python
{
    "sufficient": False,
    "no_experience": False,
    "story_context": None,
    "missing": None,
}
```

### `_find_gaps_via_llm`

1. Build outline prompt as today.
2. `agent_factory` → `Agent(model=self._model, system_prompt=_FIND_GAPS_SYSTEM)`.
3. Same transient-error catch pattern with the shared empty-list fallback:

```python
try:
    data = call_json_with_retry(
        _agent_factory,
        prompt,
        max_attempts=2,
        strict_json_suffix=_JSON_RETRY_SUFFIX,
        on_exhausted=_empty_list_fallback,
        on_unexpected_error=_empty_list_fallback,
        logger=logger,
    )
except (LLMRateLimitError, LLMTemporaryError) as e:
    return _empty_list_fallback(e)
```

4. If result is not a `list`, log and return `[]`.
5. Else map up to 3 items to `StoryGap` (skip non-dict elements), including the empty-`seed_question` fallback string — unchanged.

Array payloads continue to work through `extract_json_from_response`’s `json.loads` path; the helper’s `Dict` annotation is a type hint only.

### Imports / cleanup

- Add `from agents.blogging.shared.json_retry import call_json_with_retry` (or the package re-export).
- Add `LLMRateLimitError` / `LLMTemporaryError` imports for the call-site transient catches.
- Drop `LLMJsonParseError` if unused after migration; keep `extract_json_from_response` while `_generate_friendly_seeds` still needs it.
- Keep `time` if `_compile_narrative` still uses `time.sleep`.
- Shared module-level fallbacks: `_empty_list_fallback` and `_default_sufficiency_fallback`.

## Testing

File: `backend/agents/blogging/tests/test_ghost_writer_and_more.py`

| Case | Action |
|---|---|
| Sufficiency happy / parse-retry-success / exhausted → default | Keep |
| Sufficiency generic exception → default | Keep assertion; remove `time.sleep` patch |
| Gaps happy / non-array → `[]` / parse exhausted → `[]` | Keep |
| Gaps generic exception then recover on 2nd attempt | Change to expect immediate `[]`; rename accordingly |
| Narrator / follow-up / interview paths | Untouched |

Coverage floor (90%) and `make lint` must hold for touched files. No changes to `test_json_retry.py`.

## Non-goals reminder

Do not expand `call_json_with_retry` to formally support list returns, extract a text-retry helper for `_compile_narrative`, or migrate writer/publication agents in this change.
