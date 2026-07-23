# Design: Migrate blog_writer_agent JSON fallbacks to shared helper

Date: 2026-07-23

## Goal

Consolidate the three revise-path JSON fallbacks in `blog_writer_agent` onto
the shared `call_json_with_retry()` helper, without replacing the primary
`---DRAFT---` text+marker path.

Parent: extract shared JSON-retry helper for blogging agents. The helper and
compliance/fact-check migrations already exist on `main`. Sibling agent
migrations (plan-critic, copy-editor, ghost-writer, publication) are out of
scope.

## Context

`backend/agents/blogging/blog_writer_agent/agent.py` has three sites that share
the same shape:

1. `_revise_single_item` (~879–908)
2. Batch revise inside `revise` (~989–1011)
3. `revise_from_user_feedback` (~1224–1246)

Each site:

1. Calls `_call_text` and parses the hybrid `---DRAFT---` marker format.
2. Retries on generic exceptions with local sleep/backoff.
3. Falls back once to `_call_agent_json` for a `{"draft": "..."}` object; on
   failure keeps the original draft.

These are **not** pure JSON-retry loops (unlike compliance / plan-critic). The
shared helper applies only to step 3. Draft generation (`run`), revision-plan
JSON, and guideline-analysis JSON stay on `_call_agent_json`.

`backend/agents/blogging/shared/json_retry.py` already provides
`call_json_with_retry()` with `max_attempts`, `strict_json_suffix`,
`on_exhausted`, `on_unexpected_error`, and transient-error re-raise.

## Decisions

| Topic | Choice |
|---|---|
| Scope of helper use | JSON fallback only; keep text+marker primary path |
| Structure | One private `_fallback_draft_via_json(prompt) -> Optional[str]` on `BlogWriterAgent` |
| Attempt count | `max_attempts=2` (align with other blogging agents; intentional extra strict retry) |
| Soft JSON instruction | Bake into the base prompt passed to the helper |
| Strict suffix | Ask for a single JSON object with a string `"draft"` key |
| Agent factory | JSON-mode `Agent(model=self._model, system_prompt=WRITING_SYSTEM_PROMPT)` |
| Exhausted / unexpected | Return `{}` from hooks → method returns `None` → caller keeps original draft |
| Transient LLM errors | Propagate via helper defaults (`LLMRateLimitError` / `LLMTemporaryError`) |
| Helper module changes | None — out of scope |
| Other `_call_agent_json` callers | Out of scope |

## Architecture

### New method

```python
def _fallback_draft_via_json(self, prompt: str) -> Optional[str]:
    """Parse a revised draft via call_json_with_retry when the text path fails.

    Preconditions:
        - ``prompt`` is a non-empty string (same prompt used for the text path).
    Postconditions:
        - Returns a non-empty stripped draft string on success.
        - Returns ``None`` when JSON cannot yield a usable draft (caller keeps
          the prior draft).
        - Transient LLM transport errors propagate unwrapped.
    """
```

Implementation sketch:

1. Soft suffix on base prompt: respond with valid JSON only (no markdown fences).
2. `strict_json_suffix`: single JSON object only; key `"draft"` (string, full Markdown body).
3. `call_json_with_retry(factory, prompt + soft, max_attempts=2, strict_json_suffix=..., on_exhausted=lambda e: {}, on_unexpected_error=lambda e: {}, logger=logger)`.
4. If `data.get("draft")` is a non-empty string, return `strip()`; else `None`.

### Call-site change

Replace each site’s:

```python
try:
    data = self._call_agent_json(prompt)
    raw_draft = data.get("draft") if data else None
    if isinstance(raw_draft, str) and raw_draft.strip():
        ...
except (LLMJsonParseError, TypeError, ValueError):
    pass
```

with:

```python
fallback = self._fallback_draft_via_json(prompt)
if fallback:
    ...  # return or assign current_draft
```

Text-path loops, prompts, and logging around total failure stay as they are.

## Error handling & data flow

```
text attempts (_call_text + ---DRAFT---)
        │
        ├─ success → use revised draft
        │
        └─ no usable draft
                │
                ▼
        _fallback_draft_via_json
                │
                ├─ valid draft string → use it
                ├─ empty / bad shape / exhausted / unexpected → None → keep original
                └─ transient LLM error → raise
```

### Intentional behavior changes

1. JSON fallback performs up to two parse attempts (base + strict suffix) instead of one.
2. Transient LLM errors on the JSON fallback path propagate instead of being swallowed by a broad except that treated them as “no draft.”

## Testing

- Update existing revise/user-feedback fallback tests that monkeypatch
  `_call_agent_json` so they exercise `_fallback_draft_via_json` (or patch that
  method / `call_json_with_retry` as appropriate).
- Add focused tests for `_fallback_draft_via_json`:
  - success returns stripped draft
  - parse exhaustion → `None`
  - unexpected error → `None`
  - helper invoked with `max_attempts=2`
- Do not change draft-`run` / plan / guideline tests that still patch
  `_call_agent_json` for out-of-scope paths.
- Acceptance: existing `blog_writer_agent` tests pass; `make lint` clean; 90%
  line coverage floor holds for touched files.

## Out of scope

- Changing `call_json_with_retry` itself.
- Migrating ghost-writer, publication, or other agents.
- Replacing the text+marker primary path with pure JSON.
- Prompt-assembly changes.
- Migrating `_call_agent_json` usages in `run`, `_generate_revision_plan`, or
  `analyze_user_feedback_for_guideline_updates`.
