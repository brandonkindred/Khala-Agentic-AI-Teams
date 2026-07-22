# Design: Unified fallback taxonomy for `LlmToolAgentBase`

Date: 2026-07-22  
Status: implemented  
Module: `backend/agents/software_engineering_team/shared/llm_tool_agent_base.py`

## Goal

Add an opt-in, call-site fallback-handling step on `LlmToolAgentBase` that is a strict superset of `PlanGeneratorToolAgent`'s three-tier taxonomy (no-model / call-error / empty-parse), while also covering `ReviewToolAgent`'s exception-safe single-result pattern and partial-failure-tolerant multi-item pattern. Close the capability gap `JsonGeneratorToolAgent` currently has (no no-model guard, no call-exception handling) by making those guards available to adopt later.

This change builds and documents the capability only. It does **not** migrate or modify `ReviewToolAgent`, `PlanGeneratorToolAgent`, or `JsonGeneratorToolAgent`.

## Non-goals

- Model resolution, LLM invocation wrappers, or JSON salvage (sibling work).
- Wiring helpers into the three existing base classes (consumer migration).
- Importing team-specific output models (`ToolAgentPhaseOutput`, `ToolAgentOutput`) or `code_review_agent`.

## Design choices (locked)

1. **Class-attr vocabulary + helper methods** — same specialization style as existing plan-phase fallbacks and as `resolve_models` / `get_strands_model_fn`.
2. **Generic payload return type** — helpers return a small dataclass; callers wrap into their own output types.
3. **Execute + catch for call tiers** — call helpers run the callable and catch; no-model / empty-parse are pure builders / guards.

## Class-attribute vocabulary

Baseline shape matches `PlanGeneratorToolAgent`. Defaults are empty so subclasses that never call the helpers are unaffected.

| Attribute | Type | Default | Tier |
|---|---|---|---|
| `no_model_recommendations` | `list[str]` | `[]` | no-model |
| `no_model_summary` | `str` | `""` | no-model |
| `llm_error_recommendations` | `list[str]` | `[]` | call-error |
| `llm_error_summary` | `str` | `""` | call-error |
| `empty_recommendations` | `list[str]` | `[]` | empty-parse |
| `default_summary` | `str` | `""` | empty-parse |
| `empty_summary_override` | `str \| None` | `None` | empty-parse |

Reading attributes: prefer class-level access patterns that avoid binding unbound callables (same lesson as `get_strands_model_fn`). List values returned to callers are always copied via `list(...)`.

## `FallbackPayload`

```python
@dataclass(frozen=True)
class FallbackPayload:
    tier: Literal["no_model", "call_error", "empty_parse"]
    recommendations: list[str]
    summary: str
```

Frozen dataclass; `recommendations` is a fresh list at construction time.

## Helpers

All live on `LlmToolAgentBase`. Documented as available capability; nothing in `__init__` auto-enables them.

### 1. `_fallback_no_model(model) -> FallbackPayload | None`

- Preconditions: none beyond being called on an instance.
- If `model` is falsy, return `FallbackPayload(tier="no_model", recommendations=list(no_model_recommendations), summary=no_model_summary)`.
- Otherwise return `None`.
- No logging (callers decide whether absence of a model is noteworthy).

### 2. `_call_with_single_fallback(fn, *, log_label: str = "") -> tuple[Literal["ok"], Any] | tuple[Literal["error"], FallbackPayload]`

- Preconditions: `fn` is a zero-arg callable.
- Postconditions: on success return `("ok", fn())`; on any `Exception`, log a warning on `logging.getLogger(type(self).__module__)` including `log_label` and the exception, then return `("error", FallbackPayload(tier="call_error", ...llm_error attrs...))`.
- Covers Plan `plan` and Review `review` single-shot failure shapes.

### 3. `_call_partial_tolerant(items, fn, *, log_label: str = "") -> list[Any]`

- Preconditions: `items` is iterable; `fn(item)` is the per-item work.
- Postconditions: for each item, append `fn(item)` on success; on `Exception`, log a warning (label + truncated item context if useful + exception) and continue. Return the list of successes only.
- Does **not** synthesize a `FallbackPayload` — matches Review `problem_solve` (partial progress, no single fallback object).

### 4. `_fallback_empty_parse(*, recommendations: Sequence[str] | None = None, summary: str | None = None) -> FallbackPayload`

- Pure message application for the empty-parse tier.
- If `recommendations` is `None` or empty after normalization, use `list(empty_recommendations)`.
- Summary resolution:
  - start from `summary if summary is not None else default_summary`
  - if the resulting summary is falsy and `empty_summary_override is not None`, use `empty_summary_override`
- Return `FallbackPayload(tier="empty_parse", ...)`.
- No logging (parse-step logging belongs with JSON salvage / invocation siblings).

## Mapping to existing consumers (documentation only — not implemented here)

| Consumer pattern | Helpers to use later |
|---|---|
| Plan `plan` | `_fallback_no_model` → `_call_with_single_fallback` → (parse elsewhere) → `_fallback_empty_parse` when derived recommendations empty |
| Review `review` | `_fallback_no_model` → `_call_with_single_fallback` (summary-only attrs; recommendations stay `[]`) |
| Review `problem_solve` | `_fallback_no_model` → `_call_partial_tolerant` over issues |
| Json `run` | adopt `_fallback_no_model` + `_call_with_single_fallback` (currently missing both) + empty-parse via `_fallback_empty_parse` or equivalent |

## Testing

Extend `tests/test_llm_tool_agent_base.py` with unit coverage for each tier independently:

1. No-model: falsy model → payload; truthy model → `None`.
2. Single-call: success → `("ok", value)`; raising `fn` → `("error", call_error payload)` with class-attr messages; logger warned.
3. Partial-tolerant: mixed success/failure → only successes returned; failures logged, not raised.
4. Empty-parse: applies `empty_recommendations`; `default_summary`; `empty_summary_override` when summary empty; preserves explicit non-empty summary.
5. Existing dependency-purity subprocess test still passes.

Coverage floor ≥ 90% on touched files. `make test` / `make lint` from `backend/` pass.

## Implementation notes

- Keep the module dependency-light: stdlib only beyond what the file already imports (`importlib`, typing). Add `dataclasses` / `logging` as needed.
- Update the module docstring to state that fallback handling is now in scope as opt-in helpers (model resolution remains; invocation/JSON salvage still out of scope).
- Do not change behavior of any existing subclass in this change set.
