# Reconcile JSON-salvage engines in shared/llm_tool_agent_base.py

**Date:** 2026-07-22  
**Status:** Approved for implementation planning  
**Issue:** #2038 (parent #1984; depends on #2031)

## Goal

Extend `LlmToolAgentBase` with a class-attribute-parameterized JSON-parsing
step that can reproduce both of today's tool-agent salvage engines — including
ReviewToolAgent's non-JSON `"text"` parse mode — without migrating any
consumer classes and without unifying their failure-return semantics.

## Motivation

Three parallel tool-agent bases parse model output differently:

| Specialization | Engine today | Failure sentinel |
|---|---|---|
| `ReviewToolAgent` | `lenient_json_object()` when `review_parse_mode == "json"`; else `_parse_review(raw)` ("text" mode) | `{}` (lenient); text mode returns whatever the hook returns |
| `PlanGeneratorToolAgent` / `JsonGeneratorToolAgent` | `shared.llm_recovery.extract_json_object(raw)` | `None` (callers then do `if data is None: data = {}`) |

These must not be silently merged: callers branch on `{}` vs `None`, and
Review's `"text"` path is not JSON salvage at all.

## Decisions (locked)

| Decision | Choice |
|---|---|
| Parameterization style | Class attributes (matches model-resolution / invocation siblings) |
| Strategy attr | `json_parse_strategy: str = "lenient"` — `"lenient"` \| `"extract"` |
| Text / json mode | Separate `review_parse_mode: str = "json"` — `"json"` \| `"text"`; **only consulted when strategy is `"lenient"`** |
| Method shape | Always-available `_parse_llm_json(self, raw: str)` (like `_invoke_llm`); class attrs select the path |
| Lenient logging | Class attrs `parse_context` / `parse_on_fail_msg`; logger = `logging.getLogger(type(self).__module__)` |
| Lenient engine import | Lazy-import `lenient_json_object` from `software_engineering_team.shared.tool_agent_base` inside the lenient+json branch only |
| Extract engine import | Lazy-import `extract_json_object` from `shared.llm_recovery` inside the extract branch only |
| Failure sentinels | Lenient → `{}`; extract → `None`; no unification |
| Text mode | Call `self._parse_review(raw)`; precondition: `_parse_review` is not `None` when mode is `"text"` |
| Consumer migration | None in this change — the three existing bases stay untouched |
| Errors from engines | Propagate unchanged (engines themselves catch JSON errors) |

## Architecture

### Files touched

| Path | Change |
|---|---|
| `shared/llm_tool_agent_base.py` | Add class attrs + `_parse_llm_json`; update module/class docs |
| `tests/test_llm_tool_agent_base.py` | Cover both strategies' success/failure paths + text mode; keep purity / factory / resolution / invocation tests |

### Class attributes

| Attr | Default | Role |
|---|---|---|
| `json_parse_strategy` | `"lenient"` | `"lenient"` or `"extract"` |
| `review_parse_mode` | `"json"` | `"json"` or `"text"`; ignored unless strategy is `"lenient"` |
| `parse_context` | `""` | Passed to `lenient_json_object` as `context` |
| `parse_on_fail_msg` | `"reporting empty result."` | Passed to `lenient_json_object` as `on_fail_msg` |
| `_parse_review` | `None` | Required hook when lenient + `"text"` |

### Parse flow

```text
LlmToolAgentBase._parse_llm_json(raw)
  → if json_parse_strategy == "extract":
       lazy-import extract_json_object
       return extract_json_object(raw)   # Dict | None
  → # strategy == "lenient"
    if review_parse_mode == "text":
       assert _parse_review is not None
       return self._parse_review(raw)
    lazy-import lenient_json_object
    return lenient_json_object(
        raw,
        logger=logging.getLogger(type(self).__module__),
        context=self.parse_context,
        on_fail_msg=self.parse_on_fail_msg,
    )   # always Dict; {} on failure
```

Unknown `json_parse_strategy` (or unknown `review_parse_mode` under lenient)
values are a precondition violation — raise `AssertionError` at the boundary.

### Dependency purity

Importing `llm_tool_agent_base` must still never pull in `code_review_agent`.
Lazy-importing `tool_agent_base` only inside the lenient+json branch preserves
that: extract-only subclasses never load `tool_agent_base`. Review-like
consumers that use lenient already depend on `tool_agent_base` today.

### Recipes (documented for tests and future migration)

**Review-like (JSON)**

```python
class ReviewJsonLike(LlmToolAgentBase):
    json_parse_strategy = "lenient"
    review_parse_mode = "json"
    parse_context = "Security review"  # etc.
    parse_on_fail_msg = "reporting 0 issues."
```

**Review-like (text template)**

```python
class ReviewTextLike(LlmToolAgentBase):
    json_parse_strategy = "lenient"
    review_parse_mode = "text"
    _parse_review = staticmethod(parse_review_template)
```

**Plan/Json-like**

```python
class PlanJsonLike(LlmToolAgentBase):
    json_parse_strategy = "extract"
    # review_parse_mode unused; failure returns None
```

## Testing

Extend `tests/test_llm_tool_agent_base.py`:

1. Lenient + json success — valid / prose-wrapped JSON → dict; engines may be real or stubbed
2. Lenient + json failure — unparseable → `{}` (not `None`)
3. Lenient + text — `_parse_review` called with `raw`; return value forwarded; JSON engines not imported/called
4. Extract success — returns parsed dict
5. Extract failure — returns `None` (not `{}`)
6. Keep existing constructor, `_agent_factory`, model-resolution, invocation, and `code_review_agent` import-purity tests

Prefer stubbing the two engine functions at their import sites when asserting
dispatch; include at least one path that exercises real `lenient_json_object` /
`extract_json_object` failure sentinels so the `{}` vs `None` contract is not
only asserted against mocks.

## Out of scope

- Model resolution, LLM invocation, or fallback taxonomy (siblings B/C/E)
- Migrating `ReviewToolAgent` / `PlanGeneratorToolAgent` / `JsonGeneratorToolAgent` (F/G/H)
- Merging the two engines or unifying failure-return semantics
- Moving `lenient_json_object` out of `tool_agent_base` into a lighter module

## Acceptance criteria

- [ ] `LlmToolAgentBase` provides `_parse_llm_json` parameterized by `json_parse_strategy` and (for lenient) `review_parse_mode`, preserving `{}` vs `None` failure sentinels
- [ ] Unit tests cover both strategies' success and failure paths, including text mode
- [ ] No changes to `ReviewToolAgent`, `PlanGeneratorToolAgent`, or `JsonGeneratorToolAgent`
- [ ] Module import still does not transitively import `code_review_agent`
- [ ] `make test` and `make lint` pass from `backend/`; 90% coverage floor holds for touched files
