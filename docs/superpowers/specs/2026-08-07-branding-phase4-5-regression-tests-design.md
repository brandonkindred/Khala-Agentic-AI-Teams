# Design: Branding Phase 4/5 stub-routing regression tests

Date: 2026-08-07

## Goal

Add regression coverage in `test_dummy_client.py` so Phase 4/5 DummyLLMClient
model-class stub routing cannot regress unnoticed — matching the Phase 2
coverage shape already in that file (chat/stream tool-name routing, plus
focused complete_json cases).

## Context

Parent finding (#4909) / test sub-issue (#5559). The production fix (#5558 /
PR #5669) is already on `main`: Phase 4/5 stubs resolve by model class name via
`_branding_structured_output_stub_by_model_name`, with text-anchor fallbacks
retained.

Contract-suite coverage already includes Phase 4/5 in
`_MODEL_ROUTED_CLASS_NAMES` (`test_model_routed_payload_validates_regardless_of_prompt_text`
and the real Strands event-loop test). This change closes the remaining gap:
`llm_service` unit tests still parametrize chat/stream over Phase 2 names only.

## Decisions

| Topic | Choice |
|---|---|
| Approach | Extend existing Phase 2 parametrized fixtures (Approach A) |
| File | `backend/agents/llm_service/tests/test_dummy_client.py` only |
| Production code | Unchanged |
| Contract suite | Unchanged (already green for Phase 4/5) |
| Assertion helper | `_branding_structured_output_stub_by_model_name` (not Phase-2-only) |
| Channel extraction | Dedicated complete_json tests for `ChannelGuidelineOutput` |
| Cross-schema | Dedicated complete_json test: Phase 5 model + Phase 4-looking prompt |

## Behavior

### Shared fixture

Replace `_PHASE2_ROUTED_MODEL_NAMES` with `_MODEL_ROUTED_MODEL_NAMES` containing
all Phase 2 + 4 + 5 class-name strings that production frozensets recognize:

- Phase 2: `BrandStoryOutput`, `BrandArchetypesOutput`, `TaglineOutput`,
  `MessagingFrameworkOutput`, `PersonaProfilesOutput`, `WritingGuidelinesOutput`
- Phase 4: `BrandExperiencePrinciplesOutput`, `ChannelGuidelineOutput`,
  `BrandArchitectureOutput`, `BrandInActionOutput`
- Phase 5: `OwnershipOutput`, `ApprovalWorkflowsOutput`, `AssetWikiOutput`,
  `TrainingOnboardingOutput`, `BrandHealthKPIsOutput`,
  `EvolutionFrameworkOutput`, `BrandGuidelinesOutput`

Do not keep a Phase-2-only alias unless a remaining test truly needs it.

### Widen existing tests

Parametrize over `_MODEL_ROUTED_MODEL_NAMES`:

- `test_chat_routes_structured_output_tool_by_name_despite_misleading_prompt`
- `test_stream_routes_structured_output_tool_by_name_despite_misleading_prompt`

Assert:

```python
args == _branding_structured_output_stub_by_model_name(model_name)
```

(and the stream equivalent). Import the shared resolver; stop importing
`_branding_phase2_structured_output_stub` if unused.

Update docstrings so they no longer say “every Phase 2 class” / “six classes”.

### New complete_json tests

1. **Cross-schema** — `structured_output_model=OwnershipOutput` with a system
   prompt that includes Phase 4 channel-guide anchors
   (`content_types`, `frequency_guidance`, `channel: 'website'`). Assert the
   ownership payload shape (e.g. `"ownership_model" in j`) and that channel-guide
   keys are absent. Validate with `OwnershipOutput.model_validate(j)`.

2. **Channel extraction present** — `ChannelGuidelineOutput` + system prompt
   containing `channel: 'website'`. Assert `j["channel"] == "website"` and
   validate the model.

3. **Channel extraction missing** — `ChannelGuidelineOutput` with a prompt that
   has no `channel: '…'` match. Assert `j["channel"] == "channel"` and validate.

### Unrecognized-name fallback tests

Leave `test_chat_unrecognized_tool_name_falls_back_to_text_scan` and
`test_stream_unrecognized_tool_name_falls_back_to_text_scan` as-is (they already
use a name outside all routed classes). Optionally soften “six Phase 2 classes”
wording in their docstrings if touched; not required.

## Non-goals

- Changing `dummy.py` or branding agent code
- Editing `test_dummy_structured_output_contract.py`
- Phase 3 model-routing tests
- Removing text-anchor fallback behavior or its coverage

## Acceptance

- Widened chat/stream tests pass for every name in `_MODEL_ROUTED_MODEL_NAMES`
- New complete_json tests pass
- Full `agents/llm_service/tests/test_dummy_client.py` suite passes
- Tests would fail if Phase 4/5 were dropped from the shared resolver or from
  chat/stream wiring (where feasible to reason about without reverting)
- No GitHub issue numbers in new test comments or commit messages
