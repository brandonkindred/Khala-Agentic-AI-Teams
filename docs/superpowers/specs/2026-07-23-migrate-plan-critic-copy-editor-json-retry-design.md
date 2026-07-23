# Design: Migrate plan-critic / copy-editor to shared JSON-retry helper

Date: 2026-07-23

## Goal

Migrate `blog_plan_critic_agent` and `blog_copy_editor_agent` onto the shared
`call_json_with_retry()` helper so both stop owning duplicate JSON-retry loops.
Preserve copy-editor’s `EventLoopException` unwrapping. Align plan-critic
transient-error handling with the helper (re-raise) rather than the previous
swallow-and-FAIL-fallback path.

Parent: extract shared JSON-retry helper for blogging agents. Sibling helper
implementation and compliance/fact-check migrations are already on `main`.

## Context

`backend/agents/blogging/shared/json_retry.py` already provides
`call_json_with_retry()` with the knobs these two agents need:

- `max_attempts`, `strict_json_suffix`, `fresh_agent_per_attempt`
- `unwrap_exception` (for strands `EventLoopException`)
- `on_exhausted` / `on_unexpected_error` fallbacks
- re-raise of `LLMRateLimitError` / `LLMTemporaryError` after unwrap

Compliance and fact-check already call this helper. Plan-critic and copy-editor
still hand-roll the loop (`agent.py` ~112–138 and ~262–314 respectively).

## Decisions

| Topic | Choice |
|---|---|
| Migration style | Drop-in like compliance/fact-check (Approach 1) |
| Soft JSON instruction | Bake into the base prompt passed to the helper |
| Retry suffix | Agent-specific `strict_json_suffix` (critic keys / editor keys) |
| Plan-critic agent lifetime | `fresh_agent_per_attempt=True` (matches today’s new Agent per attempt) |
| Copy-editor agent lifetime | Default (`False`) — reuse one Agent across attempts |
| Plan-critic transient errors | **Re-raise** via helper defaults (intentional behavior change) |
| Copy-editor unwrap | Pass `unwrap_exception` that returns `EventLoopException.original_exception` when present |
| Helper changes | None — out of scope |
| Other blogging agents | Out of scope (sibling issues) |

## Plan critic

File: `backend/agents/blogging/blog_plan_critic_agent/agent.py`

### Remove

- `_MAX_CRITIC_LLM_ATTEMPTS`
- `_JSON_RETRY_SUFFIX` module constant (inline the same text as `strict_json_suffix` at the call site, or a single local string used only as that argument)

### Replace the hand-rolled loop with

```python
soft = "\n\nRespond with valid JSON only, no markdown fences."
strict = (
    "\n\nRespond with a single JSON object only (no markdown, no code fences). "
    'Keys: "status", "approved", "violations", "notes", "rubric_version".'
)

def _agent_factory():
    return Agent(model=self._model, system_prompt=PLAN_CRITIC_SYSTEM)

def _fallback_dict(exc: Exception) -> dict[str, Any]:
    return _fallback_report(str(exc)).model_dump(mode="json")

data = call_json_with_retry(
    _agent_factory,
    user_prompt + soft,
    max_attempts=2,
    strict_json_suffix=strict,
    fresh_agent_per_attempt=True,
    on_exhausted=_fallback_dict,
    on_unexpected_error=_fallback_dict,
    logger=logger,
)
```

Post-conditions unchanged after a successful parse: `_coerce_report(data)`, then
enforce `approved iff status == PASS and must_fix_count() == 0`, then optional
artifact write.

### Behavior change (explicit)

Previously, any non-`LLMJsonParseError` (including transient LLM errors) was
logged and eventually became a FAIL `_fallback_report`. After migration,
transient errors propagate so Temporal / the job runner owns retry — consistent
with compliance, fact-check, and copy-editor.

## Copy editor

File: `backend/agents/blogging/blog_copy_editor_agent/agent.py`

### Remove

- `_MAX_JSON_PARSE_ATTEMPTS` (only used by the local loop)

### Replace `_invoke_editor_llm` body loop with

```python
def _unwrap(e: Exception) -> Exception:
    return e.original_exception if isinstance(e, EventLoopException) else e

soft = "\n\nRespond with valid JSON only, no markdown fences."
strict = (
    "\n\nRespond with a single JSON object only (no markdown, no code fence). "
    "Keys: approved (boolean), summary (string), feedback_items (array of objects with "
    "category, severity, location?, issue, suggestion?)."
)

def _agent_factory():
    return Agent(model=self._model, system_prompt=COPY_EDITOR_PROMPT)

data = call_json_with_retry(
    _agent_factory,
    prompt + soft,
    max_attempts=2,
    strict_json_suffix=strict,
    unwrap_exception=_unwrap,
    on_exhausted=lambda e: _fallback_editor_data(
        "Copy editor could not parse the model response. Please review the draft manually."
    ),
    on_unexpected_error=lambda e: _fallback_editor_data(
        "Copy editor could not complete review. Please review the draft manually."
    ),
    logger=logger,
)
```

Keep the existing docstring contract: transient errors re-raise unwrapped;
JSON exhaustion / unexpected errors return the advisory fallback dict
(`approved=True`, empty feedback).

`EventLoopException` import stays; direct `LLMJsonParseError` handling and the
manual for-loop go away. Unused imports cleaned up as needed.

## Tests

### Must keep passing

- `backend/agents/blogging/tests/test_blog_plan_critic_agent.py`
- `backend/agents/blogging/tests/test_blog_copy_editor_agent.py`
- Related helpers (`test_copy_editor_helpers.py`, `test_copy_editor_length.py`) if touched indirectly

### Add

1. **Copy-editor `EventLoopException` unwrap** (acceptance criterion): agent raises
   `EventLoopException` whose `original_exception` is `LLMRateLimitError` (or
   `LLMTemporaryError`); `run` / `_invoke_editor_llm` must re-raise the **unwrapped**
   cause, not the wrapper.
2. **Plan-critic transient re-raise** (locks decision A): direct
   `LLMRateLimitError` / `LLMTemporaryError` from the fake Agent propagates;
   no FAIL fallback.

### Lint / coverage

- `make lint` clean for touched files
- 90% line-coverage floor holds for the two agent modules

## Out of scope

- Changes to `call_json_with_retry()` itself
- Ghost writer, blog writer, publication agent migrations
- Prompt / schema / coerce-logic rewrites beyond wiring the helper

## Acceptance checklist

- [ ] Both call sites use `call_json_with_retry()`
- [ ] Plan-critic local retry constant / suffix constant removed
- [ ] Copy-editor `EventLoopException` unwrap preserved and covered by a test
- [ ] Existing tests for both agents pass
- [ ] Plan-critic intentionally re-raises transient LLM errors
- [ ] `make lint` clean; coverage floor holds
