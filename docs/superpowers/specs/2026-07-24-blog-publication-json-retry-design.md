# Design: Add retry/error handling to blog_publication_agent via shared JSON-retry helper

**Issue:** #2085  
**Branch / worktree:** `refactor/2085-migrate-blog-publication-json-retry`  
**Date:** 2026-07-24

## Problem

`blog_publication_agent` has two LLM JSON call sites that still do a single,
un-retried `extract_json_from_response` with no `LLMJsonParseError` handling:

- `reject()` (~lines 239–243) — follow-up question analysis after rejection feedback
- `run_revision_loop()` (~lines 290–298) — convert free-form rejection text into
  structured `feedback_items`

An unhandled parse failure raises uncaught, unlike every sibling blogging gate
agent (compliance, fact-check, plan-critic, copy-editor, ghost-writer, writer),
which already use `call_json_with_retry()`.

## Goal

Give `blog_publication_agent` the same retry/error-handling robustness as the
other blogging gate agents: both sites use the shared helper, parse failures
retry once with a stricter prompt, and exhausted/unexpected failures soft-fall
back instead of raising uncaught. Transient LLM transport errors still re-raise.

## Non-goals

- The separately-tracked `run_revision_loop` early-break-on-approval bug.
- Any other agent's call sites (sibling sub-issues of #2048).
- Changes to `call_json_with_retry` itself.
- `EventLoopException` unwrap (publication does not currently wrap invokes that
  way; siblings that need it already pass `unwrap_exception`).

## Design

### Approach

Mirror sibling gate agents: call `call_json_with_retry` directly at each site
with `max_attempts=2`, soft JSON instruction on attempt 0, stricter suffix on
retry, and soft fallbacks via `on_exhausted` / `on_unexpected_error`. No private
wrapper method — two sites is not enough to justify another layer.

### File touched (production)

`backend/agents/blogging/blog_publication_agent/agent.py`

1. Drop `from llm_service import extract_json_from_response`.
2. Add `from agents.blogging.shared.json_retry import call_json_with_retry`.
3. Module-level soft instruction and stricter retry suffix constants (keys named
   per call site in the suffix text), matching copy-editor / fact-check style.

### `reject()` wiring

On the non-`force_ready_to_revise` path:

- Build prompt as today (`REJECTION_FOLLOW_UP_PROMPT` + collected/latest feedback).
- Pass an `agent_factory` that returns
  `Agent(model=self._model, system_prompt="You help analyze rejection feedback for blog posts.")`.
- Call `call_json_with_retry(..., max_attempts=2, logger=logger)`.
- Soft JSON instruction is appended to the prompt for attempt 0 (same text as
  today's trailing `"Respond with valid JSON only..."` string, moved into the
  shared soft-instruction constant).
- On exhaustion or unexpected error, return:

  ```python
  {
      "ready_to_revise": True,
      "questions": [],
      "feedback_summary": "\n".join(f"- {f}" for f in meta.rejection_feedback),
  }
  ```

  Rationale: proceed with collected feedback (same outcome as
  `force_ready_to_revise=True`) rather than leaving the author stuck on an
  unparseable follow-up turn.

- Successful-parse post-processing (coerce `questions`, `ready_to_revise`,
  `feedback_summary`) stays unchanged.

### `run_revision_loop()` wiring

- Build convert prompt as today (`CONVERT_FEEDBACK_TO_EDITOR_PROMPT`).
- `agent_factory` returns
  `Agent(model=self._model, system_prompt="You convert rejection feedback into structured editor feedback.")`.
- Same helper parameters (`max_attempts=2`, soft + strict suffixes, logger).
- On exhaustion or unexpected error, return `{"feedback_items": []}`.
- The existing empty-items block already synthesizes a deterministic
  `must_fix` `FeedbackItem` from raw rejection text — no new revision-loop
  semantics.

### Error classification

| Failure | Behavior |
|---|---|
| `LLMJsonParseError` with attempts left | Retry with strict JSON suffix |
| `LLMJsonParseError` exhausted | Soft fallback dict (site-specific, above) |
| Unexpected non-transient exception | Soft fallback via `on_unexpected_error` |
| `LLMRateLimitError` / `LLMTemporaryError` | Re-raise (helper default; no local catch) |

## Testing

**File:** `backend/agents/blogging/tests/test_blog_publication_agent.py`

### Existing test update

`test_revision_loop_stops_after_editor_approval` monkeypatches
`agents.blogging.blog_publication_agent.agent.extract_json_from_response`.
After migration that symbol is gone — retarget the patch to
`call_json_with_retry` (returning `{"feedback_items": []}`) so the convert step
still supplies empty structured items and the mocked editor path is exercised.

### New tests

1. **`reject()` parse exhaustion** — Agent (or helper path) yields unparseable
   text for both attempts → returns `ready_to_revise=True`, empty `questions`,
   rejection feedback still persisted on disk; no uncaught raise.

2. **`run_revision_loop()` convert parse exhaustion** — convert step unparseable
   twice → synthesizes must_fix from raw rejection and completes ≥1 revise
   iteration (same observable outcome as the empty-items synthesis path).

Transient re-raise coverage is optional and out of the minimum AC set.

### Verification

From `backend/` (worktree):

```bash
pytest agents/blogging/tests/test_blog_publication_agent.py -q
make lint
```

90% line coverage floor must hold for touched files.

## Success criteria

1. Both call sites use `call_json_with_retry()`; no direct
   `extract_json_from_response` remains in this agent.
2. JSON-parse failure at either site retries once, then soft-falls back as
   specified — never raises uncaught `LLMJsonParseError`.
3. New tests cover both previously-unhandled parse-failure paths.
4. Existing `blog_publication_agent` tests pass (including the updated monkeypatch).
5. `make lint` clean; 90% line coverage floor holds for touched files.
