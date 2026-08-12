# Branding Phase 3 Twin-Pair Collapse Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the four hand-written Phase 3 nested `*Output` classes with `_derive_strict_variant` subclasses of their soft merge-target bases, preserving today's soft and strict validation behavior.

**Architecture:** `_derive_strict_variant` already lives in `models.py` and is the locked Phase 1/2 pattern. Each strict twin is a real `create_model` subclass of the soft base, so Strands `structured_output=` still constructs the `*Output` name directly, and `_merge_structured_output()` keeps validating dumped specialist output against `VisualIdentityOutput`'s soft nested types. Delete each hand-written `class *Output` in the same step as the derive call — a later class definition would overwrite the derived name.

**Tech Stack:** Python 3.10+, Pydantic v2, pytest

**Spec:** `docs/superpowers/specs/2026-08-12-branding-phase3-twin-collapse-design.md`

**Worktree:** `.worktrees/6180-collapse-phase-3-twin-pairs` on branch `6180-collapse-phase-3-twin-pairs`. Run backend commands from `backend/`.

## Global Constraints

- Follow the approved design spec exactly
- Never put GitHub issue numbers in code, comments, commit messages, or docs (PR body only)
- Ruff line-length 120; Python 3.10 target
- Coverage ≥ 90% on new/changed code
- Do not invent a new dual-mode mechanism; use `_derive_strict_variant` only
- Do not collapse Phase 1/2 or Phase 4/5 pairs, and do not collapse Phase 3 wrapper models (`LogoSuiteOutput`, `ColorPaletteSystemOutput`, `TypographySystemOutput`, `VoiceToneOutput`, mood-board / refinement / design-system output models)
- Do not change what any constraint enforces; `VoiceToneEntryOutput.examples` stays `(List[str], Field(min_length=1))`, not `List[NonEmptyStr]`
- Do not edit `orchestrator._merge_structured_output()` or `_merge_named_fragments()`
- Design-by-Contract: `_derive_strict_variant` already has Preconditions/Postconditions; do not add a new public helper

## File map

| File | Role |
|---|---|
| `backend/agents/branding_team/models.py` | **Modify** — derive four Phase 3 `*Output` names from their soft bases; delete the four hand-written classes; bump the `_STRICT_TWIN_DOC_SUFFIX` comment from 8 call sites to 12 |
| `backend/agents/branding_team/tests/test_models.py` | **Modify** — three dual-mode tests per pair; extend the module docstring to mention Phase 3 nested twins |
| `backend/agents/branding_team/orchestrator.py` | Unchanged — Task 5 re-runs existing orchestrator tests as a merge-path regression |

TDD note: the soft-permits and strict-rejects tests are characterization tests and will pass against today's hand-written classes. The `isinstance(output, Soft)` test is the failing test that drives each collapse. Add all three together; expect only the isinstance test to fail until the derive call replaces the hand-written class.

---

### Task 1: `LogoUsageRule` / `LogoUsageRuleOutput`

**Files:**
- Modify: `backend/agents/branding_team/tests/test_models.py`
- Modify: `backend/agents/branding_team/models.py` (soft class at ~727; hand-written Output at ~1222)

**Interfaces:**
- Consumes: `_derive_strict_variant(name, base, *, doc, **field_overrides) -> type[BaseModel]`; `_STRICT_TWIN_DOC_SUFFIX: str`
- Produces: `LogoUsageRuleOutput` as a subclass of `LogoUsageRule` with `variant` / `usage_context` / `minimum_size` / `clear_space` all `(str, Field(min_length=1))`. `LogoSuiteOutput.logo_suite` keeps annotating `List[LogoUsageRuleOutput]`.

- [ ] **Step 1: Write the failing tests**

Add these imports to the existing `from branding_team.models import (` block in `backend/agents/branding_team/tests/test_models.py` (keep the list alphabetized):

```python
    LogoUsageRule,
    LogoUsageRuleOutput,
```

Append these three tests at the end of `backend/agents/branding_team/tests/test_models.py`:

```python
def test_logo_usage_rule_permits_blank_and_omitted_content() -> None:
    """``LogoUsageRule`` is the soft merge-target twin: all fields default
    to empty, matching ``VisualIdentityOutput.logo_suite``'s partial-fragment
    merge contract."""
    minimal = LogoUsageRule()
    assert minimal.variant == ""
    assert minimal.usage_context == ""
    assert minimal.minimum_size == ""
    assert minimal.clear_space == ""

    explicit_blank = LogoUsageRule(
        variant="", usage_context="", minimum_size="", clear_space=""
    )
    assert explicit_blank.variant == ""


def test_logo_usage_rule_output_rejects_blank_content() -> None:
    """A blank variant, usage context, minimum size, or clear space must fail."""
    valid_kwargs = dict(
        variant="primary",
        usage_context="Full-color lockup on light backgrounds",
        minimum_size="24px",
        clear_space="0.5x cap-height",
    )

    with pytest.raises(ValidationError):
        LogoUsageRuleOutput(**{**valid_kwargs, "variant": ""})
    with pytest.raises(ValidationError):
        LogoUsageRuleOutput(**{**valid_kwargs, "usage_context": ""})
    with pytest.raises(ValidationError):
        LogoUsageRuleOutput(**{**valid_kwargs, "minimum_size": ""})
    with pytest.raises(ValidationError):
        LogoUsageRuleOutput(**{**valid_kwargs, "clear_space": ""})

    output = LogoUsageRuleOutput(**valid_kwargs)
    assert output.variant == "primary"


def test_logo_usage_rule_output_is_usable_as_a_logo_usage_rule() -> None:
    """The derived strict twin stays a normal, directly constructible
    ``pydantic.BaseModel`` subclass wherever ``LogoUsageRule`` is."""
    output = LogoUsageRuleOutput(
        variant="primary",
        usage_context="Full-color lockup on light backgrounds",
        minimum_size="24px",
        clear_space="0.5x cap-height",
    )
    assert isinstance(output, LogoUsageRule)
```

- [ ] **Step 2: Run tests to verify the isinstance test fails**

Run: `python -m pytest agents/branding_team/tests/test_models.py::test_logo_usage_rule_permits_blank_and_omitted_content agents/branding_team/tests/test_models.py::test_logo_usage_rule_output_rejects_blank_content agents/branding_team/tests/test_models.py::test_logo_usage_rule_output_is_usable_as_a_logo_usage_rule -v`

Expected: first two PASS; `test_logo_usage_rule_output_is_usable_as_a_logo_usage_rule` FAIL with `AssertionError: assert False` (`isinstance(LogoUsageRuleOutput(...), LogoUsageRule)` is false because the hand-written Output is a sibling `BaseModel`, not a subclass).

- [ ] **Step 3: Derive the strict twin and delete the hand-written class**

Immediately below `class LogoUsageRule` in `backend/agents/branding_team/models.py`, add:

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

Delete this later hand-written class (leave `LogoSuiteOutput` in place):

```python
class LogoUsageRuleOutput(BaseModel):
    """Agent-facing logo usage rule; requires non-empty fields."""

    variant: str = Field(min_length=1)
    usage_context: str = Field(min_length=1)
    minimum_size: str = Field(min_length=1)
    clear_space: str = Field(min_length=1)
```

Do both in the same edit. If the hand-written class stays, it redefines `LogoUsageRuleOutput` and the isinstance test still fails.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest agents/branding_team/tests/test_models.py::test_logo_usage_rule_permits_blank_and_omitted_content agents/branding_team/tests/test_models.py::test_logo_usage_rule_output_rejects_blank_content agents/branding_team/tests/test_models.py::test_logo_usage_rule_output_is_usable_as_a_logo_usage_rule -v`

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/agents/branding_team/models.py backend/agents/branding_team/tests/test_models.py
git commit -m "$(cat <<'EOF'
branding_team: derive LogoUsageRuleOutput from LogoUsageRule.

Replace the hand-written strict twin with _derive_strict_variant so Strands still gets required fields while VisualIdentityOutput can merge partial LogoUsageRule fragments.
EOF
)"
```

---

### Task 2: `ColorEntry` / `ColorEntryOutput`

**Files:**
- Modify: `backend/agents/branding_team/tests/test_models.py`
- Modify: `backend/agents/branding_team/models.py` (soft class at ~709; hand-written Output at ~1240)

**Interfaces:**
- Consumes: `_derive_strict_variant`; `_STRICT_TWIN_DOC_SUFFIX`
- Produces: `ColorEntryOutput` as a subclass of `ColorEntry` with `name` / `hex_value` / `usage` / `psychological_rationale` all `(str, Field(min_length=1))`. Soft `ColorEntry` still requires `name` and defaults the other three to `""`. `ColorPaletteSystemOutput.color_palette` keeps annotating `List[ColorEntryOutput]`.

- [ ] **Step 1: Write the failing tests**

Add these imports to the existing `from branding_team.models import (` block (keep alphabetized):

```python
    ColorEntry,
    ColorEntryOutput,
```

Append these three tests at the end of `backend/agents/branding_team/tests/test_models.py`:

```python
def test_color_entry_permits_blank_and_omitted_content() -> None:
    """``ColorEntry`` is the soft merge-target twin: only ``name`` is
    required; ``hex_value``/``usage``/``psychological_rationale`` accept
    blank/omitted content, matching ``VisualIdentityOutput.color_palette``'s
    partial-fragment merge contract."""
    minimal = ColorEntry(name="Midnight")
    assert minimal.hex_value == ""
    assert minimal.usage == ""
    assert minimal.psychological_rationale == ""

    explicit_blank = ColorEntry(
        name="Midnight", hex_value="", usage="", psychological_rationale=""
    )
    assert explicit_blank.hex_value == ""


def test_color_entry_output_rejects_blank_content() -> None:
    """A blank name, hex value, usage, or rationale must fail validation."""
    valid_kwargs = dict(
        name="Midnight",
        hex_value="#1a1a2e",
        usage="Primary background",
        psychological_rationale="Conveys depth and authority",
    )

    with pytest.raises(ValidationError):
        ColorEntryOutput(**{**valid_kwargs, "name": ""})
    with pytest.raises(ValidationError):
        ColorEntryOutput(**{**valid_kwargs, "hex_value": ""})
    with pytest.raises(ValidationError):
        ColorEntryOutput(**{**valid_kwargs, "usage": ""})
    with pytest.raises(ValidationError):
        ColorEntryOutput(**{**valid_kwargs, "psychological_rationale": ""})

    output = ColorEntryOutput(**valid_kwargs)
    assert output.name == "Midnight"


def test_color_entry_output_is_usable_as_a_color_entry() -> None:
    """The derived strict twin stays a normal, directly constructible
    ``pydantic.BaseModel`` subclass wherever ``ColorEntry`` is."""
    output = ColorEntryOutput(
        name="Midnight",
        hex_value="#1a1a2e",
        usage="Primary background",
        psychological_rationale="Conveys depth and authority",
    )
    assert isinstance(output, ColorEntry)
```

- [ ] **Step 2: Run tests to verify the isinstance test fails**

Run: `python -m pytest agents/branding_team/tests/test_models.py::test_color_entry_permits_blank_and_omitted_content agents/branding_team/tests/test_models.py::test_color_entry_output_rejects_blank_content agents/branding_team/tests/test_models.py::test_color_entry_output_is_usable_as_a_color_entry -v`

Expected: first two PASS; `test_color_entry_output_is_usable_as_a_color_entry` FAIL with `AssertionError: assert False` (`isinstance(ColorEntryOutput(...), ColorEntry)` is false).

- [ ] **Step 3: Derive the strict twin and delete the hand-written class**

Immediately below `class ColorEntry` in `backend/agents/branding_team/models.py`, add:

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

Delete this later hand-written class (leave `ColorPaletteSystemOutput` in place):

```python
class ColorEntryOutput(BaseModel):
    """Agent-facing color entry; requires non-empty fields."""

    name: str = Field(min_length=1)
    hex_value: str = Field(min_length=1)
    usage: str = Field(min_length=1)
    psychological_rationale: str = Field(min_length=1)
```

Do both in the same edit so the later class cannot overwrite the derived name.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest agents/branding_team/tests/test_models.py::test_color_entry_permits_blank_and_omitted_content agents/branding_team/tests/test_models.py::test_color_entry_output_rejects_blank_content agents/branding_team/tests/test_models.py::test_color_entry_output_is_usable_as_a_color_entry -v`

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/agents/branding_team/models.py backend/agents/branding_team/tests/test_models.py
git commit -m "$(cat <<'EOF'
branding_team: derive ColorEntryOutput from ColorEntry.

Replace the hand-written strict twin with _derive_strict_variant so color-system structured output stays required while VisualIdentityOutput can still merge a name-only ColorEntry.
EOF
)"
```

---

### Task 3: `TypographySpec` / `TypographySpecOutput`

**Files:**
- Modify: `backend/agents/branding_team/tests/test_models.py`
- Modify: `backend/agents/branding_team/models.py` (soft class at ~718; hand-written Output at ~1259)

**Interfaces:**
- Consumes: `_derive_strict_variant`; `_STRICT_TWIN_DOC_SUFFIX`
- Produces: `TypographySpecOutput` as a subclass of `TypographySpec` with `role` / `font_family` / `weight_range` / `usage_notes` all `(str, Field(min_length=1))`. Soft `TypographySpec()` still constructs with all four fields `""`. `TypographySystemOutput.typography_system` keeps annotating `List[TypographySpecOutput]`.

- [ ] **Step 1: Write the failing tests**

Add these imports to the existing `from branding_team.models import (` block (keep alphabetized):

```python
    TypographySpec,
    TypographySpecOutput,
```

Append these three tests at the end of `backend/agents/branding_team/tests/test_models.py`:

```python
def test_typography_spec_permits_blank_and_omitted_content() -> None:
    """``TypographySpec`` is the soft merge-target twin: all fields default
    to empty, matching ``VisualIdentityOutput.typography_system``'s
    partial-fragment merge contract."""
    minimal = TypographySpec()
    assert minimal.role == ""
    assert minimal.font_family == ""
    assert minimal.weight_range == ""
    assert minimal.usage_notes == ""

    explicit_blank = TypographySpec(
        role="", font_family="", weight_range="", usage_notes=""
    )
    assert explicit_blank.role == ""


def test_typography_spec_output_rejects_blank_content() -> None:
    """A blank role, font family, weight range, or usage notes must fail."""
    valid_kwargs = dict(
        role="display",
        font_family="Inter",
        weight_range="600-800",
        usage_notes="Headlines and hero type only",
    )

    with pytest.raises(ValidationError):
        TypographySpecOutput(**{**valid_kwargs, "role": ""})
    with pytest.raises(ValidationError):
        TypographySpecOutput(**{**valid_kwargs, "font_family": ""})
    with pytest.raises(ValidationError):
        TypographySpecOutput(**{**valid_kwargs, "weight_range": ""})
    with pytest.raises(ValidationError):
        TypographySpecOutput(**{**valid_kwargs, "usage_notes": ""})

    output = TypographySpecOutput(**valid_kwargs)
    assert output.role == "display"


def test_typography_spec_output_is_usable_as_a_typography_spec() -> None:
    """The derived strict twin stays a normal, directly constructible
    ``pydantic.BaseModel`` subclass wherever ``TypographySpec`` is."""
    output = TypographySpecOutput(
        role="display",
        font_family="Inter",
        weight_range="600-800",
        usage_notes="Headlines and hero type only",
    )
    assert isinstance(output, TypographySpec)
```

- [ ] **Step 2: Run tests to verify the isinstance test fails**

Run: `python -m pytest agents/branding_team/tests/test_models.py::test_typography_spec_permits_blank_and_omitted_content agents/branding_team/tests/test_models.py::test_typography_spec_output_rejects_blank_content agents/branding_team/tests/test_models.py::test_typography_spec_output_is_usable_as_a_typography_spec -v`

Expected: first two PASS; `test_typography_spec_output_is_usable_as_a_typography_spec` FAIL with `AssertionError: assert False` (`isinstance(TypographySpecOutput(...), TypographySpec)` is false).

- [ ] **Step 3: Derive the strict twin and delete the hand-written class**

Immediately below `class TypographySpec` in `backend/agents/branding_team/models.py`, add:

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

Delete this later hand-written class (leave `TypographySystemOutput` in place):

```python
class TypographySpecOutput(BaseModel):
    """Agent-facing typography spec; requires non-empty fields."""

    role: str = Field(min_length=1)
    font_family: str = Field(min_length=1)
    weight_range: str = Field(min_length=1)
    usage_notes: str = Field(min_length=1)
```

Do both in the same edit so the later class cannot overwrite the derived name.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest agents/branding_team/tests/test_models.py::test_typography_spec_permits_blank_and_omitted_content agents/branding_team/tests/test_models.py::test_typography_spec_output_rejects_blank_content agents/branding_team/tests/test_models.py::test_typography_spec_output_is_usable_as_a_typography_spec -v`

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/agents/branding_team/models.py backend/agents/branding_team/tests/test_models.py
git commit -m "$(cat <<'EOF'
branding_team: derive TypographySpecOutput from TypographySpec.

Replace the hand-written strict twin with _derive_strict_variant so type-system structured output stays required while VisualIdentityOutput can still merge a partial TypographySpec.
EOF
)"
```

---

### Task 4: `VoiceToneEntry` / `VoiceToneEntryOutput`

**Files:**
- Modify: `backend/agents/branding_team/tests/test_models.py`
- Modify: `backend/agents/branding_team/models.py` (soft class at ~736; hand-written Output at ~1295)

**Interfaces:**
- Consumes: `_derive_strict_variant`; `_STRICT_TWIN_DOC_SUFFIX`
- Produces: `VoiceToneEntryOutput` as a subclass of `VoiceToneEntry` with `context` / `tone` as `(str, Field(min_length=1))` and `examples` as `(List[str], Field(min_length=1))` — container non-empty only; blank items remain valid. Do not use `List[NonEmptyStr]`. `VoiceToneOutput.voice_tone_spectrum` keeps annotating `List[VoiceToneEntryOutput]`.

- [ ] **Step 1: Write the failing tests**

Add these imports to the existing `from branding_team.models import (` block (keep alphabetized):

```python
    VoiceToneEntry,
    VoiceToneEntryOutput,
```

Append these three tests at the end of `backend/agents/branding_team/tests/test_models.py`. The Output test asserts `[""]` is accepted on `examples` so a later `NonEmptyStr` tightening cannot land unnoticed. Do not add a test that expects `[""]` to fail.

```python
def test_voice_tone_entry_permits_blank_and_omitted_content() -> None:
    """``VoiceToneEntry`` is the soft merge-target twin: all fields default
    empty, matching ``VisualIdentityOutput.voice_tone_spectrum``'s
    partial-fragment merge contract."""
    minimal = VoiceToneEntry()
    assert minimal.context == ""
    assert minimal.tone == ""
    assert minimal.examples == []

    explicit_blank = VoiceToneEntry(context="", tone="", examples=[])
    assert explicit_blank.examples == []

    blank_item = VoiceToneEntry(examples=[""])
    assert blank_item.examples == [""]


def test_voice_tone_entry_output_rejects_blank_content() -> None:
    """A blank context, tone, or empty examples list must fail; a list of
    blank strings is still valid because ``examples`` is ``List[str]`` with
    container ``min_length=1``, not ``List[NonEmptyStr]``."""
    valid_kwargs = dict(
        context="marketing",
        tone="confident and warm",
        examples=["Let's ship the brand, not the buzzwords."],
    )

    with pytest.raises(ValidationError):
        VoiceToneEntryOutput(**{**valid_kwargs, "context": ""})
    with pytest.raises(ValidationError):
        VoiceToneEntryOutput(**{**valid_kwargs, "tone": ""})
    with pytest.raises(ValidationError):
        VoiceToneEntryOutput(**{**valid_kwargs, "examples": []})

    output = VoiceToneEntryOutput(**valid_kwargs)
    assert output.context == "marketing"

    blank_item = VoiceToneEntryOutput(
        context="marketing", tone="confident and warm", examples=[""]
    )
    assert blank_item.examples == [""]


def test_voice_tone_entry_output_is_usable_as_a_voice_tone_entry() -> None:
    """The derived strict twin stays a normal, directly constructible
    ``pydantic.BaseModel`` subclass wherever ``VoiceToneEntry`` is."""
    output = VoiceToneEntryOutput(
        context="marketing",
        tone="confident and warm",
        examples=["Let's ship the brand, not the buzzwords."],
    )
    assert isinstance(output, VoiceToneEntry)
```

- [ ] **Step 2: Run tests to verify the isinstance test fails**

Run: `python -m pytest agents/branding_team/tests/test_models.py::test_voice_tone_entry_permits_blank_and_omitted_content agents/branding_team/tests/test_models.py::test_voice_tone_entry_output_rejects_blank_content agents/branding_team/tests/test_models.py::test_voice_tone_entry_output_is_usable_as_a_voice_tone_entry -v`

Expected: first two PASS (including the `[""]` acceptance assertion against the hand-written class); `test_voice_tone_entry_output_is_usable_as_a_voice_tone_entry` FAIL with `AssertionError: assert False` (`isinstance(VoiceToneEntryOutput(...), VoiceToneEntry)` is false).

- [ ] **Step 3: Derive the strict twin and delete the hand-written class**

Immediately below `class VoiceToneEntry` in `backend/agents/branding_team/models.py`, add:

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

`examples` must be `List[str]`, not `List[NonEmptyStr]`.

Delete this later hand-written class (leave `VoiceToneOutput` in place):

```python
class VoiceToneEntryOutput(BaseModel):
    """Agent-facing voice/tone entry; requires non-empty fields."""

    context: str = Field(min_length=1)
    tone: str = Field(min_length=1)
    examples: List[str] = Field(min_length=1)
```

Do both in the same edit so the later class cannot overwrite the derived name.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest agents/branding_team/tests/test_models.py::test_voice_tone_entry_permits_blank_and_omitted_content agents/branding_team/tests/test_models.py::test_voice_tone_entry_output_rejects_blank_content agents/branding_team/tests/test_models.py::test_voice_tone_entry_output_is_usable_as_a_voice_tone_entry -v`

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/agents/branding_team/models.py backend/agents/branding_team/tests/test_models.py
git commit -m "$(cat <<'EOF'
branding_team: derive VoiceToneEntryOutput from VoiceToneEntry.

Replace the hand-written strict twin with _derive_strict_variant, keeping examples as List[str] with container min_length so blank items stay valid.
EOF
)"
```

---

### Task 5: Call-site comment, test module docstring, merge-path regression

**Files:**
- Modify: `backend/agents/branding_team/models.py` (`_STRICT_TWIN_DOC_SUFFIX` comment at ~95)
- Modify: `backend/agents/branding_team/tests/test_models.py` (module docstring)
- Test: `backend/agents/branding_team/tests/test_models.py`
- Test: `backend/agents/branding_team/tests/test_orchestrator.py` (unchanged fixtures; must keep passing)

**Interfaces:**
- Consumes: the four derived `*Output` names from Tasks 1–4
- Produces: comment text that matches the 12 `_derive_strict_variant` call sites; module docstring that names the Phase 3 nested twins. No orchestrator API changes.

- [ ] **Step 1: Update the call-site comment**

In `backend/agents/branding_team/models.py`, change:

```python
# Common closing sentence for every strict-twin ``doc=`` below — factored out
# so the 8 call sites don't each hand-duplicate the same boilerplate tail.
```

to:

```python
# Common closing sentence for every strict-twin ``doc=`` below — factored out
# so the 12 call sites don't each hand-duplicate the same boilerplate tail.
```

- [ ] **Step 2: Extend the test module docstring**

Replace the module docstring of `backend/agents/branding_team/tests/test_models.py` with:

```python
"""Validation tests for the Phase 1, Phase 2, Phase 3, and Phase 4 structured-output models.

These agent-facing models (``BrandDiscoveryAuditOutput``, ``PurposeVisionOutput``,
``CoreValuesOutput``, ``AudienceSegmentsOutput``, ``DifferentiationPillarsOutput``,
``PositioningOutput``, plus Phase 2's ``BrandStoryOutput``,
``BrandArchetypesOutput``, ``TaglineOutput``, ``MessagingFrameworkOutput``
(and its nested ``MessagingPillarOutput``/``AudienceMessageMapOutput``),
``PersonaProfilesOutput``, ``WritingGuidelinesOutput``, plus Phase 3's nested
twins (``LogoUsageRuleOutput``, ``ColorEntryOutput``, ``TypographySpecOutput``,
``VoiceToneEntryOutput``), plus Phase 4's ``ChannelGuidelineOutput``,
``BrandArchitectureOutput``, and ``BrandExperiencePrinciplesOutput``) must
reject empty/omitted content so Strands' structured-output tool retries the LLM
instead of silently accepting a blank or under-cardinality response (see
``structured_output_tool.py``: a ``ValidationError`` becomes a tool error
the model is asked to fix). Dual-mode tests also pin the soft merge-target
twins and the ``isinstance(strict, soft)`` subclass contract produced by
``_derive_strict_variant``.
"""
```

- [ ] **Step 3: Run model tests and orchestrator merge-path tests**

Run: `python -m pytest agents/branding_team/tests/test_models.py agents/branding_team/tests/test_orchestrator.py -v`

Expected: all passed. `test_orchestrator.py` constructs `VisualIdentityOutput` with partial soft `ColorEntry` / `TypographySpec` instances and must not need fixture edits.

- [ ] **Step 4: Commit**

```bash
git add backend/agents/branding_team/models.py backend/agents/branding_team/tests/test_models.py
git commit -m "$(cat <<'EOF'
branding_team: document Phase 3 twin-collapse call sites and coverage.

Bump the shared strict-twin docstring suffix to 12 call sites and extend test_models.py so the Phase 3 nested pairs are named alongside the existing dual-mode suite.
EOF
)"
```

---

## Self-review

**Spec coverage**

| Spec requirement | Task |
|---|---|
| Collapse `LogoUsageRule` / `LogoUsageRuleOutput` via `_derive_strict_variant` | Task 1 |
| Collapse `ColorEntry` / `ColorEntryOutput` | Task 2 |
| Collapse `TypographySpec` / `TypographySpecOutput` | Task 3 |
| Collapse `VoiceToneEntry` / `VoiceToneEntryOutput` with `examples=(List[str], Field(min_length=1))` | Task 4 |
| Three tests per pair (soft permits, strict rejects, isinstance) | Tasks 1–4 |
| Do not add a blank-item *rejection* test for `examples`; `[""]` stays valid | Task 4 positive assertion |
| Wrapper models unchanged | Tasks 1–4 leave `LogoSuiteOutput` / `ColorPaletteSystemOutput` / `TypographySystemOutput` / `VoiceToneOutput` in place |
| Orchestrator merge helpers unchanged | Global constraint; Task 5 re-runs `test_orchestrator.py` |
| Bump "8 call sites" → "12" | Task 5 |
| Extend `test_models.py` module docstring | Task 5 |

**Placeholder scan:** no TBD / TODO / "similar to Task N" / "add tests for the above".

**Type consistency:** every derive call uses the spec's exact `field_overrides`. `VoiceToneEntryOutput.examples` is `List[str]` in the derive call, the deleted class, and the tests.
