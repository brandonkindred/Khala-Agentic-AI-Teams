# Migrate Remaining Blogging Tests to `make_writer_agent()`

**Status:** Approved 2026-07-24  
**Date:** 2026-07-24  
**Type:** Test refactor (behavior-preserving)  
**Issue:** #2098  
**Branch / worktree:** `refactor/2098-migrate-make-writer-agent` / `.worktrees/refactor-2098-make-writer-agent`

## Problem

`backend/agents/blogging/tests/conftest.py` already provides `make_writer_agent()` as the shared factory for `BlogWriterAgent` test construction. Several files still construct `BlogWriterAgent(...)` directly with the same `DummyLLMClient` + style/brand pattern. That leaves two construction paths and makes future fixture changes harder to apply consistently.

## Goals

1. Make `make_writer_agent()` the default path for constructing a `BlogWriterAgent` in the blogging test suite (outside out-of-scope files).
2. Preserve existing test behavior (same LLM client, same style/brand content, same assertions).
3. Keep intentional direct construction only where the factory cannot express the case, with a one-line comment.

## Non-goals

- `test_blog_writer_agent.py` (already migrated / tracked separately).
- Local stub-class replacement in `test_run_pipeline_minimal.py` / `test_agent_workflow.py`.
- `_api_test_utils.py` / `_content_plan_test_utils.py` consolidation.
- Extending `make_writer_agent()` with new parameters (not needed for the remaining call sites).
- Production code changes.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Approach | Drop-in migrate + one intentional exception | Factory already covers all non-validation cases via existing kwargs |
| Factory API | No signature change | Current `llm_client`, `writing_style_guide_content`, `brand_spec_content` suffice |
| `llm_client=None` validation test | Keep direct `BlogWriterAgent(llm_client=None)` + comment | Factory treats `None` as “new DummyLLMClient()”, so it cannot express this case |
| Empty guidelines | `make_writer_agent(writing_style_guide_content="", brand_spec_content="")` | Matches constructor defaults used by several helper tests |
| Import style | `from .conftest import make_writer_agent` | Matches already-migrated files (`test_writer_run.py`, etc.) |

## Inventory (implementation-time)

Enumerated via `BlogWriterAgent(` under `backend/agents/blogging/tests/`, excluding `conftest.py` (factory body) and out-of-scope `test_blog_writer_agent.py`:

| File | Direct calls | Migration |
|---|---|---|
| `test_writer_plan_content.py` | 1 (in `_agent_with_guidelines`) | Replace helper body with `make_writer_agent()` |
| `test_planning_loop_parity.py` | 1 | `make_writer_agent(writing_style_guide_content=..., brand_spec_content=...)` |
| `test_blog_publication_agent.py` | 2 | Same, with each site’s existing style/brand strings |
| `test_blog_writer_agent_revise.py` | 1 | `make_writer_agent(llm_client=llm, ...)` |
| `test_blog_writer_agent_integration.py` | 1 | `make_writer_agent(llm_client=client, ...)` |
| `test_writer_and_v2_helpers.py` | 5 | Four → factory (incl. empty guidelines / custom style); one `llm_client=None` stays direct with comment |

## Design

### Factory (unchanged)

```python
def make_writer_agent(
    *,
    llm_client: Any | None = None,
    writing_style_guide_content: str = "Style",
    brand_spec_content: str = "Brand",
) -> Any:
    ...
```

Call sites that previously passed only `DummyLLMClient()` and relied on empty constructor defaults must pass empty strings explicitly when empty guidelines matter for the assertion. Sites that do not care about guidelines may use factory defaults (`"Style"` / `"Brand"`) only when that does not change asserted behavior; when in doubt, pass empty strings to preserve prior construction.

### Intentional direct construction

In `test_writer_and_v2_helpers.py` (`test_writer_agent_assertion_on_none_llm`):

```python
# Direct construction intentional: make_writer_agent() replaces None with DummyLLMClient.
BlogWriterAgent(llm_client=None)
```

### Error handling / testing

- No new tests; this is a mechanical migration of construction sites.
- Acceptance: full blogging test suite passes; coverage at or above current baseline.
- Post-migration grep: every remaining `BlogWriterAgent(` under `tests/` is either the factory body, the intentional `None` case, or out-of-scope `test_blog_writer_agent.py` (already on the factory).

## Acceptance Criteria

- [ ] Every remaining direct `BlogWriterAgent(...)` construction across the blogging test suite is migrated to `make_writer_agent()`, or has a one-line comment explaining why direct construction is intentional
- [ ] `make_writer_agent()` is not extended unless a call site proves an optional override is required (expected: no extension)
- [ ] Full blogging test suite passes with coverage at or above the current baseline
