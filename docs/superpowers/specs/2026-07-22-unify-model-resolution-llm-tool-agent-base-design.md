# Unify model resolution in shared/llm_tool_agent_base.py

**Date:** 2026-07-22  
**Status:** Approved for implementation planning

## Goal

Extend `LlmToolAgentBase` with an opt-in, class-attribute-parameterized model
resolution step that can reproduce both of today's tool-agent resolver call
shapes — without migrating any consumer classes yet.

## Motivation

Three parallel tool-agent bases resolve their Strands model differently at
construct time:

| Specialization | Call shape today |
|---|---|
| `ReviewToolAgent` | `resolve_strands_model(llm, response_format="text")`, plus a second call with `response_format="json"` when `uses_json_model` is set |
| `PlanGeneratorToolAgent` / `JsonGeneratorToolAgent` | `resolve_strands_model(llm, get_strands_model_fn=get_strands_model)` (default `response_format="json"`) |

Import paths look different (`software_engineering_team.shared.strands_model` vs
`llm_service.strands_model`), but the SE shared module is already a shim onto
`llm_service.strands_model` — the functions are the same. The behavioral fork is
the **kwargs** (and the optional second JSON-mode model). A shared core must
parameterize those kwargs, not silently collapse to one call shape.

## Decisions (locked)

| Decision | Choice |
|---|---|
| Parameterization style | Class attributes (not a strategy enum/object) |
| When resolution runs | Opt-in only via `resolve_models: bool = False` |
| Opt-in signal | Explicit flag — not inferred from other attrs |
| Default attrs when opted in | Review-like: `response_format="text"`, `uses_json_model=False`, `get_strands_model_fn=None` |
| Plan/Json recipe | Subclass sets `resolve_models=True`, `response_format="json"`, `get_strands_model_fn=<callable>` |
| Resolver import | Lazy-import `llm_service.strands_model.resolve_strands_model` inside the resolve step |
| `get_strands_model_fn` forwarding | Include the kwarg only when the class attr is not `None` |
| Second JSON model | When `uses_json_model`, always resolve `_model_json` with `response_format="json"` (same fn-forwarding rule) |
| Consumer migration | None in this change — `ReviewToolAgent` / `PlanGeneratorToolAgent` / `JsonGeneratorToolAgent` untouched |
| Errors | Propagate; no new try/except around resolution |

## Architecture

### Files touched

| Path | Change |
|---|---|
| `shared/llm_tool_agent_base.py` | Add class attrs + opt-in resolve step in `__init__` |
| `tests/test_llm_tool_agent_base.py` | Cover both resolver recipes + opt-out; keep purity / factory tests |

### Class attributes

| Attr | Default | Role |
|---|---|---|
| `resolve_models` | `False` | Opt-in; if false, `__init__` only sets `self.llm` |
| `response_format` | `"text"` | Primary `_model` resolution format |
| `uses_json_model` | `False` | If true, also set `_model_json` via a second `"json"` resolve |
| `get_strands_model_fn` | `None` | Forwarded only when not `None` |

### Resolution flow

```text
LlmToolAgentBase.__init__(llm)
  → self.llm = llm
  → if not resolve_models: return
  → lazy-import resolve_strands_model from llm_service.strands_model
  → kwargs = {response_format: self.response_format}
       + {get_strands_model_fn: fn} if fn is not None
  → self._model = resolve_strands_model(llm, **kwargs)
  → if uses_json_model:
       self._model_json = resolve_strands_model(
           llm, response_format="json", **{same fn rule})
```

When `resolve_models` is false, `_model` / `_model_json` are not set.

### Recipes (documented for tests and future migration)

**Review-like**

```python
class ReviewLike(LlmToolAgentBase):
    resolve_models = True
    # defaults: response_format="text", get_strands_model_fn=None
    # optional: uses_json_model = True
```

**Plan/Json-like**

```python
class PlanJsonLike(LlmToolAgentBase):
    resolve_models = True
    response_format = "json"
    get_strands_model_fn = get_strands_model  # or any injectable callable
```

## Testing

Extend `tests/test_llm_tool_agent_base.py` (mock `resolve_strands_model`; do not
hit a live LLM):

1. Opt-out — `resolve_models=False` → only `llm`; no `_model`
2. Review path — one call with `response_format="text"` and no `get_strands_model_fn`; result on `_model`
3. Review + JSON — second call with `response_format="json"`; result on `_model_json`
4. Plan/Json path — one call with `response_format="json"` and `get_strands_model_fn=sentinel`
5. Keep existing constructor, `_agent_factory`, and `code_review_agent` import-purity tests

## Out of scope

- LLM invocation, JSON parsing, or fallback logic (sibling work)
- Migrating the three existing base classes onto `LlmToolAgentBase`
- Choosing one resolver import path as canonical / deprecating the other
- Changing `resolve_strands_model` itself

## Acceptance criteria

- [ ] `LlmToolAgentBase` accepts class-attr parameterization for both call shapes, including optional `_model_json`
- [ ] Unit tests exercise Review-like and Plan/Json-like paths independently, plus opt-out
- [ ] No changes to `ReviewToolAgent`, `PlanGeneratorToolAgent`, or `JsonGeneratorToolAgent`
- [ ] Module still does not transitively import `code_review_agent`
- [ ] `make test` and `make lint` pass from `backend/`; 90% coverage floor holds for touched files
