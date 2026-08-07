# Design: Branding Phase 4/5 model-class stub routing

Date: 2026-08-07

## Goal

Route DummyLLMClient Phase 4 and Phase 5 branding structured-output stubs by
Pydantic model class name (the same pattern Phase 2 already uses), so a prompt
reword or incidental field-name mention cannot silently return the wrong stub.

Text-anchor helpers remain as a fallback when no model name is available.
For the six channel guides that share `ChannelGuidelineOutput`, class-name
dispatch selects the schema; the system prompt is used only to extract the
`channel` string.

## Context

Parent finding (#4909) / implement sub-issue (#5558): Phase 4
(`_branding_phase4_structured_stub`) and Phase 5
(`_branding_phase5_structured_stub`) still scan lowercased system-prompt
substrings (e.g. `"content_types"` + `"frequency_guidance"`). Phase 2 already
fixed this via `_branding_phase2_structured_output_stub` wired into
`complete_json`, `chat`, and `stream`.

Production path for `build_agent(structured_output=...)` agents is Strands'
tool-calling loop into `chat`/`stream`, where the StructuredOutputTool name is
`model.__name__`. `complete_json(..., structured_output_model=...)` is the
direct / test path.

Regression tests are sibling #5559 and out of scope here.

## Decisions

| Topic | Choice |
|---|---|
| Approach | Mirror Phase 2: model-name stubs + keep text-anchor fallback |
| Phases in scope | Phase 4 and Phase 5 only |
| Phase 3 | Unchanged (still text-routed via `_branding_phase3_structured_stub`) |
| Channel guides | One class `ChannelGuidelineOutput` for all six guides; prompt supplies `channel` only after class-name match |
| Channel extraction | Existing regex `channel:\s*'([a-z_]+)'` on `system_lowered`; fallback `"channel"` |
| Payload ownership | Extract current inline dicts into small helpers shared by model-route and text-route paths |
| Text scanners | Rename to `_branding_phase{4,5}_text_routed_stub`; same anchors as today |
| Tool detection | Extend `_looks_like_structured_output_tool` membership to Phase 4/5 model-name frozensets |
| Tests | Not in this change (#5559) |
| File touch | `backend/agents/llm_service/clients/dummy.py` only (plus this design doc) |

## Dispatch table

### Phase 4

| Model class | Payload helper / stub |
|---|---|
| `BrandExperiencePrinciplesOutput` | experience principles payload |
| `ChannelGuidelineOutput` | channel guide payload (+ channel from prompt) |
| `BrandArchitectureOutput` | architecture payload |
| `BrandInActionOutput` | brand-in-action payload |

### Phase 5

| Model class | Payload helper / stub |
|---|---|
| `OwnershipOutput` | ownership payload |
| `ApprovalWorkflowsOutput` | approval workflows payload |
| `AssetWikiOutput` | asset/wiki payload |
| `TrainingOnboardingOutput` | training payload |
| `BrandHealthKPIsOutput` | KPI payload |
| `EvolutionFrameworkOutput` | evolution payload |
| `BrandGuidelinesOutput` | brand rules payload |

## Behavior

### New helpers

1. `_PHASE4_STRUCTURED_OUTPUT_MODEL_NAMES` / `_PHASE5_STRUCTURED_OUTPUT_MODEL_NAMES`
   frozensets of the class names above.
2. `_branding_phase4_structured_output_stub(model_name, system_lowered="")` and
   `_branding_phase5_structured_output_stub(model_name)` — return the matching
   stub dict or `None`.
3. Payload builders (names illustrative) that own the dict literals currently
   inlined in the text scanners, so model-route and text-route share one
   source of truth per schema.
4. Rename today's `_branding_phase4_structured_stub` /
   `_branding_phase5_structured_stub` to
   `_branding_phase4_text_routed_stub` /
   `_branding_phase5_text_routed_stub`. Update `_branding_structured_stub`
   to call the new names (Phase 3 → Phase 4 text → Phase 5 text). Do not
   keep the old names as aliases.

### Call-site wiring

Deterministic model-name resolution order wherever a model/tool name is known:

1. `_branding_phase2_structured_output_stub(name)`
2. `_branding_phase4_structured_output_stub(name, system_lowered=...)`
3. `_branding_phase5_structured_output_stub(name)`
4. On all `None`: existing text-anchor / generic paths

Apply that chain in:

- `complete_json` when `structured_output_model is not None`
- `chat` when a StructuredOutputTool is detected
- `stream` when a StructuredOutputTool is detected

Pass `system_lowered` into the Phase 4 stub only so `ChannelGuidelineOutput`
can fill `channel` without using prompt text to *select* which stub.

### Contracts

Preconditions / postconditions on the new helpers match Phase 2's style:

- Preconditions: `model_name` is a string; `system_lowered` is already
  lowercased (may be empty).
- Postconditions: returns a fresh stub dict for a recognized name, else
  `None` so callers fall through.

## Non-goals

- Phase 3 model-class routing
- Deleting Phase 4/5 text-anchor fallbacks
- Broader `dummy.py` refactors unrelated to this dispatch
- New or updated regression tests (sibling #5559)
- Changing branding agent prompts or Pydantic models

## Acceptance

- Phase 4/5 stubs resolve by model class name on `complete_json` /
  `chat` / `stream` when a known name is present
- Prompt scanning is not used to choose among Phase 4/5 schemas on that path
- `ChannelGuidelineOutput` still gets a channel value from the prompt when
  present, else `"channel"`
- Text-anchor fallback still works when no model name is supplied
- Lint for touched files passes
- No unrelated refactors
