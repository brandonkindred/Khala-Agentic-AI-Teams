# Design: Dummy LLM Phase 2 explicit schema routing

Date: 2026-08-06

## Goal

Stop `DummyLLMClient` from choosing Branding Phase 2 structured payloads by
scanning free-text system prompts for substrings such as `messaging_framework`,
`jobs_to_be_done`, or `writing_guidelines`. Route Phase 2 stubs only by an
explicit schema identity supplied by the caller.

## Context

Automated review of an unrelated branding Phase 5 migration flagged pre-existing
brittle text-anchor routing in `backend/agents/llm_service/clients/dummy.py`.
An incidental mention of a field name in instructions or examples can select
the wrong canned payload relative to the agent's `structured_output=` model.

A prior change already added a deterministic fast path:
`complete_json(..., structured_output_model=X)` and Strands tool-name dispatch
call `_branding_phase2_structured_output_stub(model_name)`. The text-anchor
helper `_branding_phase2_text_routed_stub` remains as a fallback and is still
exercised by several tests that pass only `system_prompt`.

Production Phase 2 agents already use `build_agent(structured_output=...)`;
Strands drives them through `chat()`/`stream()` tool naming, not the text
fallback.

## Decisions

| Topic | Choice |
|---|---|
| Scope | Phase 2 Narrative & Messaging stubs only |
| Approach | Delete text-anchor fallback; require explicit model/tool identity |
| Routing identity | `structured_output_model.__name__` or Strands StructuredOutputTool name |
| Missing identity | Fall through like any unrecognized prompt (no Phase 2 substring sniffing) |
| Phases 1 / 3 / 4 / 5 | Unchanged (remain text-anchored for now) |
| Production agents | No factory changes |

## Behavior

### Delete

- `_branding_phase2_text_routed_stub`
- The `complete_json` branch that calls it

### Keep

- `_branding_phase2_structured_output_stub`
- `_PHASE2_STRUCTURED_OUTPUT_MODEL_NAMES`
- Existing `complete_json` fast path when `structured_output_model` is a known
  Phase 2 class
- Existing `chat()` / `stream()` dispatch when the structured-output tool name
  matches a known Phase 2 class

### Postconditions after the change

- A Phase 2–looking system prompt without `structured_output_model` (and
  without a matching tool name on `chat`/`stream`) must **not** return a
  Phase 2 narrative payload.
- Passing a known Phase 2 model class still returns the matching cumulative
  stub, even when the system prompt mentions a different specialist's fields.

## Call-site / test updates

### `llm_service/tests/test_dummy_client.py`

- Retarget Phase 2 cumulative-key, mutability, and voice-principles tests to
  pass `structured_output_model=<Class>` instead of relying on system-prompt
  anchors alone.
- Keep the existing “model wins over misleading prompt” regression.
- Add a regression that a Phase 2–looking system prompt **without**
  `structured_output_model` does not return Phase 2 keys.
- Leave non–Phase-2 text routing tests alone (e.g. architecture stubs).

### `branding_team/tests/test_dummy_stub_alignment.py`

- For Phase 2 factories in `_PHASE1_AND_PHASE2_CASES`, pass
  `structured_output_model=output_model` into `complete_json`.
- Phase 1 cases stay system-prompt routed.

## Error handling

No new exception type. Missing or unrecognized model identity simply does not
select a Phase 2 stub.

## Out of scope

- Extending explicit model-name tables to Phase 1 / 3 / 4 / 5
- Broader dummy-client prompt-matcher redesign
- Changes to real LLM clients

## Acceptance mapping

| Acceptance criterion | How this design satisfies it |
|---|---|
| Explicit schema identifier / model hint | `structured_output_model` or tool name only |
| Tests fail before / pass after | New no-model regression + updated Phase 2 callers |
| Related lint/tests pass | `test_dummy_client` + branding stub alignment suite |
