# Branding Phase 3: collapse soft/strict twin pairs

**Date:** 2026-08-12
**Status:** Approved for implementation planning
**Scope:** Four nested Phase 3 pairs in `backend/agents/branding_team/models.py`, plus dual-mode coverage in `backend/agents/branding_team/tests/test_models.py`

## Problem

`models.py` keeps a soft merge-target model and a strict agent-output twin for several nested types. Phase 1 and Phase 2 already generate the strict twin from the soft base via `_derive_strict_variant`. Phase 3 still hand-duplicates four pairs:

- `LogoUsageRule` / `LogoUsageRuleOutput`
- `ColorEntry` / `ColorEntryOutput`
- `TypographySpec` / `TypographySpecOutput`
- `VoiceToneEntry` / `VoiceToneEntryOutput`

The field lists are identical; only requiredness and `min_length` differ. That duplication is the same drift risk Phase 1/2 already removed.

## Goal

Each of the four pairs is a single inheritance relationship: the soft class stays the merge target, and the `*Output` name is a `_derive_strict_variant` subclass that preserves today's strict constraints. Soft and strict validation behavior do not change. Orchestrator merge helpers keep working without edits.

## Non-goals

- Do **not** invent a new dual-mode mechanism. `_derive_strict_variant` is the locked pattern.
- Do **not** collapse Phase 1/2 pairs (already done) or Phase 4/5 pairs (sibling work).
- Do **not** collapse other Phase 3 agent-facing wrappers (`LogoSuiteOutput`, `ColorPaletteSystemOutput`, `TypographySystemOutput`, `VoiceToneOutput`, mood-board / refinement / design-system output models).
- Do **not** change what any constraint enforces. In particular, do **not** upgrade `VoiceToneEntryOutput.examples` from `List[str]` to `List[NonEmptyStr]`.
- Do **not** edit `orchestrator._merge_structured_output()` or `_merge_named_fragments()`.

## Decisions (locked)

| Decision | Choice |
|---|---|
| Mechanism | `_derive_strict_variant` already in `models.py` (same as Phase 1/2) |
| Placement | Call the helper immediately below each soft class, then delete the hand-written `*Output` class from the later "Phase 3 agent-facing structured_output schemas" section |
| Wrapper models | Keep `LogoSuiteOutput`, `ColorPaletteSystemOutput`, `TypographySystemOutput`, and `VoiceToneOutput` as hand-written classes; they continue to annotate lists of the `*Output` names |
| `VoiceToneEntryOutput.examples` | `(List[str], Field(min_length=1))` — container non-empty only; blank items still valid, matching today's hand-written class |
| Subclass contract | Each `*Output` remains a real subclass of its soft base (`isinstance` / `issubclass` hold) so dumped strict instances validate as the soft type used on `VisualIdentityOutput` |
| Tests | Three tests per pair, matching the Phase 1/2 pattern in `test_models.py` |
| Merge helpers | Unchanged; Phase 3 extraction already validates dumped specialist output against `VisualIdentityOutput`, whose nested fields are the soft types |

## Architecture

No new modules. The helper already documents why a derived subclass is used instead of a `context=`-gated validator: Strands `structured_output=` constructs instances directly and does not thread validation context.

```
soft class (merge target, defaults)
    → _derive_strict_variant(name, soft, doc=..., **field_overrides)
    → *Output subclass (Strands structured_output schema)
```

`VisualIdentityOutput` keeps listing the soft types (`List[LogoUsageRule]`, `List[ColorEntry]`, …). Agent wrapper models keep listing the strict types (`List[LogoUsageRuleOutput]`, …). Because the strict class subclasses the soft class, `model_dump()` of a strict instance still validates as the soft field type.

### Files

- Modify: `backend/agents/branding_team/models.py` — replace the four hand-written Output classes with helper calls; bump the `_STRICT_TWIN_DOC_SUFFIX` comment from "8 call sites" to "12".
- Modify: `backend/agents/branding_team/tests/test_models.py` — add dual-mode tests for each pair; extend the module docstring to mention Phase 3 nested twins.
- Unchanged: `orchestrator.py`, graph builders, agent factories, Phase 3 wrapper output models.

## Locked field overrides

Each block is the exact `_derive_strict_variant` call to add. Soft class bodies stay as they are today.

### `LogoUsageRuleOutput`

```python
LogoUsageRuleOutput = _derive_strict_variant(
    "LogoUsageRuleOutput",
    LogoUsageRule,
    doc=(
        "Agent-facing logo usage rule; requires non-empty fields.\n\n"
        "Field-for-field twin of ``LogoUsageRule`` with required content — "
        "``LogoUsageRule`` itself must stay soft (all-default) since it also "
        "backs ``VisualIdentityOutput.logo_suite``'s merge target. "
    )
    + _STRICT_TWIN_DOC_SUFFIX,
    variant=(str, Field(min_length=1)),
    usage_context=(str, Field(min_length=1)),
    minimum_size=(str, Field(min_length=1)),
    clear_space=(str, Field(min_length=1)),
)
```

Soft behavior to preserve: all four fields default to `""`; `LogoUsageRule()` constructs.

### `ColorEntryOutput`

```python
ColorEntryOutput = _derive_strict_variant(
    "ColorEntryOutput",
    ColorEntry,
    doc=(
        "Agent-facing color entry; requires non-empty fields.\n\n"
        "Field-for-field twin of ``ColorEntry`` with required content — "
        "``ColorEntry`` itself must stay soft (only ``name`` required) since "
        "it also backs ``VisualIdentityOutput.color_palette``'s merge target. "
    )
    + _STRICT_TWIN_DOC_SUFFIX,
    name=(str, Field(min_length=1)),
    hex_value=(str, Field(min_length=1)),
    usage=(str, Field(min_length=1)),
    psychological_rationale=(str, Field(min_length=1)),
)
```

Soft behavior to preserve: only `name` is required; `hex_value` / `usage` / `psychological_rationale` default to `""`.

### `TypographySpecOutput`

```python
TypographySpecOutput = _derive_strict_variant(
    "TypographySpecOutput",
    TypographySpec,
    doc=(
        "Agent-facing typography spec; requires non-empty fields.\n\n"
        "Field-for-field twin of ``TypographySpec`` with required content — "
        "``TypographySpec`` itself must stay soft (all-default) since it also "
        "backs ``VisualIdentityOutput.typography_system``'s merge target. "
    )
    + _STRICT_TWIN_DOC_SUFFIX,
    role=(str, Field(min_length=1)),
    font_family=(str, Field(min_length=1)),
    weight_range=(str, Field(min_length=1)),
    usage_notes=(str, Field(min_length=1)),
)
```

Soft behavior to preserve: all four fields default to `""`; `TypographySpec()` constructs. Existing orchestrator fixtures that omit `usage_notes` keep working.

### `VoiceToneEntryOutput`

```python
VoiceToneEntryOutput = _derive_strict_variant(
    "VoiceToneEntryOutput",
    VoiceToneEntry,
    doc=(
        "Agent-facing voice/tone entry; requires non-empty fields.\n\n"
        "Field-for-field twin of ``VoiceToneEntry`` with required content — "
        "``VoiceToneEntry`` itself must stay soft (all-default) since it also "
        "backs ``VisualIdentityOutput.voice_tone_spectrum``'s merge target. "
    )
    + _STRICT_TWIN_DOC_SUFFIX,
    context=(str, Field(min_length=1)),
    tone=(str, Field(min_length=1)),
    examples=(List[str], Field(min_length=1)),
)
```

Soft behavior to preserve: `context` / `tone` default to `""`; `examples` defaults to `[]` and accepts blank items.

Strict `examples` is container-length only (`min_length=1` on `List[str]`). A list of `[""]` remains valid on the Output class. Do not switch this field to `List[NonEmptyStr]`.

## Testing

`tests/test_models.py` currently covers Phase 1, Phase 2, and Phase 4 wrappers. It has no dual-mode tests for these four Phase 3 pairs. Add three tests per pair, copied from the Phase 1/2 shape:

1. **`test_<soft>_permits_blank_and_omitted_content`** — construct the soft class with omitted and explicit-blank fields; assert defaults and that blank list items are accepted where the soft type has a list field.
2. **`test_<output>_rejects_blank_content`** — empty string on every string field fails; empty `examples` fails on `VoiceToneEntryOutput`; a successful full payload round-trips.
3. **`test_<output>_is_usable_as_a_<soft>`** — a valid Output instance `isinstance` the soft class.

Do not add a blank-item rejection test for `VoiceToneEntryOutput.examples`. Today's strict class accepts `[""]`; preserving that is required.

Existing `test_orchestrator.py` fixtures already construct `VisualIdentityOutput` with partial soft `ColorEntry` / `TypographySpec` instances. Those tests must keep passing with no fixture edits.

## Error handling

`_derive_strict_variant` does not add new error paths. Invalid strict payloads still raise `pydantic.ValidationError`. Merge helpers still catch `ValidationError` and return `None`. No new exception types.

## Self-review

- No placeholders. Field overrides are listed in full.
- Constraints match the current hand-written Output classes, including the `List[str]` (not `NonEmptyStr`) choice on `examples`.
- Scope is the four named pairs plus their tests; wrappers, orchestrator, and other phases are out of scope.
- "Collapse into one model" means one inheritance pair per type, not deleting the `*Output` name. Strands schemas and wrapper list annotations keep using the Output name.
