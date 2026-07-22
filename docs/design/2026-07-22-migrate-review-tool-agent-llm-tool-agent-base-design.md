# Design: Migrate `ReviewToolAgent` onto `LlmToolAgentBase`

Date: 2026-07-22  
Status: implemented  
Module: `backend/agents/software_engineering_team/shared/tool_agent_base.py`  
Depends on: closed work in `llm_tool_agent_base.py` (skeleton, resolver, invocation, JSON salvage, fallback taxonomy)

## Goal

Rewrite `ReviewToolAgent` / `BaseReviewToolAgent` as a thin specialization of
`LlmToolAgentBase`, selecting the Review recipe already documented on that base:
model resolution (text + optional JSON-mode model), `run_strands_agent`-wrapped
invocation, lenient / text-mode JSON parsing, and exception-safe single-shot
fallback in `review()`. Behavior of every intermediate and concrete subclass
stays unchanged.

## Non-goals

- Any change to `_engine_review()` / `review_via_engine` or its lazy
  `code_review_agent` import.
- Migrating `PlanGeneratorToolAgent` or `JsonGeneratorToolAgent` (separate
  issues).
- Changing `tool_agent_static.py` (static / file-generator bases — not Review
  intermediates).
- Changing `LlmToolAgentBase` unless a tiny gap blocks behavior parity (prefer
  not).
- Rewriting `problem_solve` onto `_call_partial_tolerant` (log wording differs;
  hybrid approach keeps the hand-rolled loop).

## Approach (locked)

**In-place rewrite** of `BaseReviewToolAgent` in `tool_agent_base.py`:

1. Inherit `LlmToolAgentBase`.
2. Set Review recipe class attributes.
3. Delete duplicate `__init__` and `_agent_factory`.
4. Keep `_run_agent` as a thin alias to `_invoke_llm` so intermediates
   (documentation, etc.) remain unmodified.
5. Wire `review()` LLM path to shared helpers; leave `problem_solve` loop as-is.

### Hybrid fallback wiring (locked)

| Method | Shared helpers | Rationale |
|---|---|---|
| `review` (LLM path) | `_fallback_no_model`, `_call_with_single_fallback`, `_invoke_llm`, `_parse_llm_json` | Matches single-shot taxonomy; dynamic summaries preserved by ignoring helper payload text |
| `problem_solve` | `_run_agent` → `_invoke_llm` only | Keep existing per-issue try/except and log format (`"{name} fix for issue {desc} failed"`) |
| `_engine_review` / build-runner | unchanged | Explicitly out of scope |

## Class shape & recipe

```python
class BaseReviewToolAgent(LlmToolAgentBase):
    resolve_models = True
    response_format = "text"
    use_run_strands_agent = True
    json_parse_strategy = "lenient"
    review_parse_mode = "text"  # override LlmToolAgentBase default of "json"
    # uses_json_model remains False by default; subclasses may set True
```

- Resolver path is `llm_service.strands_model` (same module object as the SE
  `shared.strands_model` shim). Do not reintroduce a local SE import for
  resolution/invocation inside `__init__` / `_invoke_llm`.
- Alias: `ReviewToolAgent = BaseReviewToolAgent` remains.
- Module-level helpers `relevant_code_for_issue` and `lenient_json_object` stay
  in `tool_agent_base.py` (`LlmToolAgentBase._parse_llm_json` already imports
  `lenient_json_object` lazily — no import-time cycle).

## `review()` LLM-path wiring

Keep build-runner and `review_via_engine` branches first (byte-for-byte
behavior). For the one-shot LLM path:

1. If `_fallback_no_model(self._model)` is not `None`, return
   `ToolAgentPhaseOutput(summary=f"{review_label} skipped (no LLM).")`
   (discard helper payload summary; keep today’s dynamic label).
2. Empty code → same skip summary as today.
3. Build prompt; select model via `review_model_attr`.
4. `_call_with_single_fallback(lambda: self._invoke_llm(model, prompt),
   log_label=review_label)`. On `"error"`, return
   `ToolAgentPhaseOutput(summary=f"{review_label} failed (LLM error).")`.
5. Before parse, set instance `parse_context=review_label` and
   `parse_on_fail_msg="reporting 0 issues."` so `_parse_llm_json(raw)` matches
   today’s `lenient_json_object` logging. Text mode still uses subclass
   `_parse_review`.
6. Issue mapping loop unchanged.

## `problem_solve`

Unchanged control flow and logging. LLM calls continue through
`_run_agent` → `_invoke_llm`.

## Files

| Path | Change |
|---|---|
| `shared/tool_agent_base.py` | Inherit, recipe attrs, `review` wire, `_run_agent` alias |
| Intermediates / concrete agents | None expected |
| `shared/llm_tool_agent_base.py` | None preferred |
| Tests | Only if needed for ≥90% on newly uncovered touched lines |

## Testing & verification

Must pass unchanged in intent:

- `tests/test_shared_tool_agent_base.py`
- `tests/test_v2_tool_agents_testing_ux.py`
- Broader `test_v2_tool_agents*.py` coverage for representative concrete agents
  (security, performance, accessibility, ux, testing_qa, build_specialist,
  documentation, static) across backend and frontend code-v2 teams
- `tests/test_llm_tool_agent_base.py`

From `backend/`: `make lint`, targeted pytest above, then `make test`. Coverage
floor ≥90% on touched files.

## Acceptance criteria mapping

- [ ] `ReviewToolAgent` derives from `LlmToolAgentBase` with Review recipe attrs.
- [ ] `_engine_review` / `review_via_engine` / lazy import unchanged.
- [ ] Intermediates (`BackendReviewToolAgent`, Testing/QA, build specialist,
  documentation) behavior unchanged; `_run_agent` alias preserves call sites.
- [ ] Concrete subclasses behave identically (existing suites).
- [ ] `test_shared_tool_agent_base.py` and `test_v2_tool_agents_testing_ux.py` pass.
- [ ] `make test` / `make lint` pass; 90% coverage on touched files.
