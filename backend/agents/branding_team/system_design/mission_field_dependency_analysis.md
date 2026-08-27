# Mission Field Dependency Analysis — BrandingMission Fields per Phase

**Scope:** analysis only. No agent prompts, models, or orchestrator code
were changed to produce this document. Today `phase_input_hash` and
`_phase_task` hash and inject the *entire* serialized `BrandingMission`
for every phase, so any mission field edit invalidates and re-runs every
phase regardless of relevance. This document is the evidence base for
narrowing that: for each of the 5 branding-team phases, which
`BrandingMission` input fields do that phase's own agent prompts
(`backend/agents/branding_team/agents.py`) actually reference? This is
the mission-field counterpart to the existing
[`phase_dependency_analysis.md`](./phase_dependency_analysis.md), which
answers the same "which agent's prompt references it" question for
*upstream phase outputs* instead of *mission input fields*. That document
is the precedent for this one's method and citation style; this document
does not repeat its findings.

## Field-catalog scope correction

The originating request describes the target as "`BrandingMission` fields
(`models.py`'s `BrandingMissionFields`)". That parenthetical is imprecise
relative to what the hashing/injection mechanism actually operates on,
and this document scopes itself to the more precise target instead of
the literally-named class:

- `BrandingMissionFields` (`models.py:297-328`) declares 8 fields:
  `company_name`, `company_description`, `target_audience`, `values`,
  `differentiators`, `desired_voice`, `existing_brand_material`,
  `wiki_path`.
- `BrandingMission` (`models.py:331-347`) is a subclass that adds 6
  visual-identity fields: `color_inspiration`, `color_palettes`,
  `selected_palette_index`, `visual_style`, `typography_preference`,
  `interface_density` — **14 fields total**.
- `BrandingMission`, not `BrandingMissionFields`, is the class actually
  hashed and injected into every phase today:
  - `shared/memoization.py:97-106` — `phase_input_hash`'s payload includes
    `"mission": mission.model_dump(mode="json")` with no `include`/
    `exclude`, so it dumps every field the runtime instance carries.
    `phase_input_hash` is type-annotated `mission: BrandingMission`.
  - `graphs/shared.py:238-240` — `serialize_mission(mission)` calls
    `mission.model_dump_json()`, again with no `include`/`exclude`.
  - `orchestrator.py:795-798` (`_phase_task`) calls
    `serialize_mission(mission)` unconditionally for every phase, and
    `orchestrator.py:583-586` does the same for the default monolithic-graph
    path.
  - `BrandingMissionFields.mission_fields()` (`models.py:318-328`, which
    returns only the 8 base fields via
    `self.model_dump(include=set(BrandingMissionFields.model_fields))`)
    has exactly one production call site, `api/state.py:171`
    (`BrandingMission(**payload.mission_fields())`), where it strips
    API-DTO extras before *constructing* a full `BrandingMission` — it
    never scopes what gets hashed or serialized downstream.

Every table below therefore covers all 14 `BrandingMission` fields, not
just the 8 declared directly on `BrandingMissionFields`.

## Method

Every agent's system prompt is fully data-driven —
`AgentPromptSpec.opening` + `fields`/`structured_output`-derived field
lines + optional `closing`, rendered verbatim by `render_agent_prompt`
(`prompt_spec.py:117-141`) with no hidden boilerplate injected elsewhere —
the same rendering-completeness guarantee the sibling document relies on.
So reading each `_..._PROMPT = AgentPromptSpec(...)` literal in
`agents.py`, including every bound `structured_output` model's
`Field(description=...)` text, is reading the complete system prompt text
sent to the LLM.

A mission field is counted as **explicit** only when the prompt text
names the literal field identifier (e.g. `desired_voice`) or an
unambiguous single-field paraphrase (e.g. "the company description",
matching only `company_description`). It is counted as **ambiguous** when
the prompt text could plausibly refer to either a mission field or a
same-phase/upstream-phase *output* field of the same name, and there is
no way to disambiguate from the text alone. It is counted as **none**
when no plausible textual reference exists at all.

The recurring ambiguous case in this codebase is the word "values" (and,
once, "positioning"): several Phase 2, 3, and 5 prompts say things like
"the brand's strategic core and values" or "the full brand context
(positioning, promise, values, narrative, visual identity)" — each time
immediately adjacent to "strategic core" or "positioning"/"promise",
which are exclusively Phase 1 *output* concepts
(`StrategicCoreOutput.core_values`, `.positioning_statement`,
`.brand_promise`), not mission inputs. The sibling document already
established this same disambiguation for its own "which upstream phase"
question (e.g. its Phase 5 section reading `brand_rules_codifier`'s
"values" as a `StrategicCoreOutput` reference). This document applies the
identical standard in the opposite direction: those same "values"
mentions are **not** counted as `BrandingMission.values` references here,
for consistency with how the sibling document already classified them.

## Limitation — generic mission references and today's unconditional injection

Many of the 32 `AgentPromptSpec`s open with an unqualified phrase like
"Given a branding mission" or "Using the branding mission ... as context"
— naming no specific field. Per the confidence rule above, this document
counts those as **none** for every individual field, since the phrase
itself is not textual evidence that any *particular* field matters (it is
boilerplate framing present across most of `agents.py`, not a selective
citation). That classification answers "which fields does the prompt text
explicitly ask this agent to use" — it does **not** answer "which fields
have zero influence on this agent's output."

The distinction matters because `_phase_task` (`orchestrator.py:795-798`)
unconditionally injects the *complete*, field-labelled mission JSON into
every phase's task string today, regardless of what that phase's own
agent prompts ask for. So an agent whose system prompt says only "the
branding mission" — e.g. `Storyteller`, `TaglineWriter`, `PersonaBuilder`
in Phase 2, or most of Phase 1's agents — currently receives every
mission field in its context, including ones its own prompt text never
names, and nothing in this document's method can rule out the LLM having
used one of them (e.g. `company_name` or `company_description`) to
produce its actual output, even without an explicit instruction to. This
is the same "over-hashing only lowers hit rate; under-hashing could make
it return a stale hit" asymmetry `phase_input_hash`'s own docstring
already warns about (`shared/memoization.py:31-39`) — the risk this
document's recommendations could pose is exactly that under-hashing
failure mode, not a new one.

This document's recommended allowlists are therefore a **necessary but
unproven-sufficient** starting hypothesis, built from the only static
evidence available (prompt text), not a guarantee that narrowing the hash
to exactly these fields is safe. Before any allowlist from this document
is implemented, its effect on actual output quality should be validated
empirically — e.g. by extending
`backend/agents/branding_team/scripts/eval_selective_context.py`
(which already A/B-compares full-context vs. selective-context pipeline
runs via an LLM judge, today scoped to `context_phases`/upstream-phase
filtering) to also compare full-mission vs. allowlisted-mission runs per
phase, rather than treating this document's tables alone as sufficient
sign-off.

## Phase 1 — Strategic Core (6 agents, `agents.py:104-273`)

No upstream phase exists, so every mission-field reference here is a
first-order one.

| Agent | Prompt text (verbatim excerpt) | Field(s) | Confidence |
|---|---|---|---|
| `discovery_auditor` (104-111) | closing: "Be specific and grounded in the company description and target audience provided." | `company_description`, `target_audience` | explicit |
| `purpose_vision_writer` (133-137) | closing: "Be concise, inspiring, and specific to the company." | `company_name` and/or `company_description` | ambiguous — "the company" doesn't disambiguate which field |
| `values_articulator` (159-165) | opening: "Given a branding mission with optional seed values, produce a list of 3-5 core values." | `values` | explicit — "optional seed values" is this agent's literal input framing for the mission's `values` list |
| `audience_segmenter` (187-194) | opening: "...identify 1-3 target audience segments." / closing: "Ground your analysis in the company description and stated target audience." | `company_description`, `target_audience` | explicit |
| `differentiation_mapper` (216-223) | opening: "Given a branding mission with optional differentiators, produce 2-4 differentiation pillars." | `differentiators` | explicit |
| `positioning_synthesizer` (246-253) | opening names only same-phase sibling agents ("discovery auditor, purpose/vision writer, values articulator, audience segmenter, differentiation mapper") — no direct opening/closing mission-field text | — | none in prompt text itself; see indirect note below |

**Indirect note (`positioning_synthesizer`):** its `structured_output`,
`PositioningOutput.positioning_statement`, carries a `Field(description=...)`
template (`models.py:562-567`): *"a single sentence following the format:
'For [audience] who need [need], [company] is the [differentiator] that
delivers [value] because [proof].'"* The bracketed tokens `[audience]`,
`[company]`, `[differentiator]`, `[value]` are generic placeholder nouns
echoing `target_audience`, `company_name`, `differentiators`, and `values`
respectively — not prose naming those fields the way `discovery_auditor`'s
closing does. This is weaker evidence than a hard citation (a template
variable, not a sentence about the mission), so it is flagged here rather
than merged into the table's `explicit` column, and it does not change
`company_name`'s "ambiguous" status above — it is one more data point for
the same field, not a stronger one.

**Recommended field allowlist:** `(company_description, target_audience,
values, differentiators)`, with `company_name` flagged separately as
ambiguous — referenced only via generic "the company" paraphrase and an
output-template placeholder, never named as a field in prose.

## Phase 2 — Narrative & Messaging (6 agents, `agents.py:282-480`)

| Agent | Prompt text (verbatim excerpt) | Field(s) | Confidence |
|---|---|---|---|
| `Storyteller` (282-285) | "Using the strategic core output and branding mission, craft:" | — | none — "branding mission" is a generic, non-specific reference; no field named |
| `ArchetypeAnalyst` (306-320) | "...fit the brand's positioning and values, and add:" | `values` | ambiguous — see Method: "values" adjacent to "strategic core output"/"positioning" reads as `StrategicCoreOutput.core_values`, not `BrandingMission.values` |
| `TaglineWriter` (344-358) | "Using the branding mission and strategic core output as context, add:" | — | none — generic reference only |
| `MessageMapper` (381-398) | "...strategic core output (positioning, values, audience segments, differentiation) as context, add:" | `values` | ambiguous — same pattern; the parenthetical explicitly frames all four terms as "strategic core output," i.e. Phase 1 output fields, not raw mission fields |
| `PersonaBuilder` (421-434) | "Using the branding mission and the strategic core's audience segments as context, create:" | — | none — "the strategic core's audience segments" is an explicit Phase 1 *output*-field reference (`StrategicCoreOutput.target_audience_segments`), not the mission's `target_audience` |
| `VoicePrinciplesDrafter` (457-477) | "Using the branding mission's **desired_voice** and the strategic core output as context, produce writing_guidelines:" | `desired_voice` | **explicit — the only literal field-identifier match found anywhere in `agents.py`** |

**Recommended field allowlist:** `(desired_voice,)`. The two "values"
mentions above are flagged as ambiguous per the Method section but are
not counted, consistent with the sibling document's own treatment of the
identical phrasing pattern.

## Phase 3 — Visual & Expressive Identity (9 factories, `agents.py:510-761`)

**Zero mission-field references found anywhere** — opening, fields,
closing, and every bound `structured_output` model's
`Field(description=...)` text were all checked.

| Agent | Prompt text (verbatim excerpt) | Field(s) | Confidence |
|---|---|---|---|
| `MoodBoardConceptualist_{variant}` ×3 (510-528) | "Given a brand's strategic core and narrative, create a moodboard concept with:" | — | none |
| `converge_decider` (559-570) | "...plus the brand's strategic core and values. Score each candidate on:..." | `values` | ambiguous — same "values"-adjacent-to-"strategic core" pattern as Phase 2 |
| `logo_specifier` (592-598) | "Based on the winning moodboard direction, define a logo suite..." | — | none |
| `color_system_builder` (620-627) | "Based on the winning moodboard direction, define {N} colors..." | — | none |
| `typography_builder` (649-656) | "Based on the winning moodboard direction, define a typography system..." | — | none |
| `iconography_director` (678-681) | "Based on the winning moodboard, define:" | — | none |
| `photography_video_director` (702-705) | "Based on the winning moodboard, define:" | — | none |
| `voice_tone_builder` (727-733) | "Using the brand narrative's writing guidelines and the moodboard direction, define:" | — | none |
| `design_system_codifier` (755-758) | "Based on the full visual identity work, produce:" | — | none |

**Finding — the mission's visual-identity fields ground exactly this
phase's subject matter and are not referenced anywhere in it.**
`color_system_builder`, `typography_builder`, and
`MoodBoardConceptualist` are the three agents whose job most plausibly
overlaps `color_inspiration`, `color_palettes`, `selected_palette_index`,
`visual_style`, and `typography_preference` — a guided-palette-selection
mission field feeding directly into a color-system-building agent's
prompt is exactly the kind of grounding one would expect. None of the
nine Phase 3 prompts reference any of these five fields, or
`interface_density`, by name, paraphrase, or `structured_output`
description. Every reference in this phase is instead to same-phase
artifacts ("the winning moodboard direction") or Phase 1/2 outputs
("strategic core and narrative"). This may be a deliberate product choice
(the moodboard/color/typography agents are meant to synthesize a fresh
direction rather than be constrained by the user's raw color/typography
preferences) — but as with the sibling document's Phase 4 finding, it is
not evidence-based today, and is flagged for a product decision rather
than silently assumed.

**Recommended field allowlist:** `()` — no `BrandingMission` field is
referenced by any Phase 3 agent prompt today. The sibling document's
original `context_phases` empty-tuple ambiguity (where `()` and "not
configured" were indistinguishable and both meant "no filtering") has
since been fixed in `orchestrator.py`/`shared/memoization.py`:
`_phase_task` now filters `prior_outputs` whenever `context_phases is not
None` (`orchestrator.py:791-793`), so an explicit `()` means "filter to
this empty set — include nothing," and only `None` (the default) means
"not configured, include everything." No mission-field allowlist
mechanism exists in code today — this epic is what introduces one — and
it should adopt the same `None`-vs-explicit-tuple convention for
consistency rather than reintroduce the ambiguity that was just fixed for
`context_phases`. Under that convention, this document's recommended `()`
for this phase means exactly what it says: filter to zero mission
fields.

## Phase 4 — Experience & Channel Activation (9 factories via 4 `AgentPromptSpec` sites, `agents.py:787-1023`)

**Zero mission-field references found anywhere.**

| Agent | Prompt text (verbatim excerpt) | Field(s) | Confidence |
|---|---|---|---|
| `brand_experience_principler` (787-793) | "You are a Brand Experience Architect. Define:" | — | none |
| `{channel}_guide` ×6 (823-856, shared `_channel_guide_prompt`) | "Define guidelines for the {channel} channel:" / closing "Context: {description}" | — | none — `{description}` is a hardcoded static string from `CHANNEL_SPECS` (`agents.py:811-818`, e.g. `"Company website, landing pages, product pages."`), not derived from any mission field |
| `brand_architecture_builder` (990-993) | "You are a Brand Architecture Specialist. Define:" | — | none |
| `brand_in_action_illustrator` (1014-1020) | "Create {N} applied examples showing correct vs incorrect brand usage:" | — | none |

**Recommended field allowlist:** `()`, same rationale as Phase 3.

## Phase 5 — Governance & Evolution (7 agents, `agents.py:1049-1205`)

**Zero mission-field references found anywhere.**

| Agent | Prompt text (verbatim excerpt) | Field(s) | Confidence |
|---|---|---|---|
| `ownership_definer` (1049-1052) | "You are a Brand Ownership Definer. Define:" | — | none |
| `approval_workflow_designer` (1073-1076) | "You are an Approval Workflow Designer. Define:" | — | none |
| `asset_wiki_planner` (1097-1100) | `wiki_backlog` field description covers "Brand North Star, Voice Playbook, Design System, Brand Review Intake, Channel Playbook, Governance Charter" (`models.py:1391-1396`) | — | none — these are upstream *phase* concepts (already documented in the sibling document), not mission fields |
| `training_planner` (1121-1124) | "You are a Training Planner. Define:" | — | none |
| `kpi_designer` (1144-1147) | "You are a Brand KPI Designer. Define:" | — | none |
| `evolution_framer` (1168-1171) | "You are a Brand Evolution Framer. Define:" | — | none |
| `brand_rules_codifier` (1192-1202) | "Using the full brand context (positioning, promise, values, narrative, visual identity), produce:" | `values` | ambiguous — same "values"-adjacent-to-"positioning, promise" pattern as Phases 2-3 |

**Recommended field allowlist:** `()`, same rationale as Phases 3-4.

## Findings

1. **One field has a literal field-identifier citation; five fields total
   are marked `explicit` (`E`) in the summary matrix.** These are two
   different counts and should not be conflated: `desired_voice` (Phase
   2, `VoicePrinciplesDrafter`) is the *only* field named by its literal
   Python identifier anywhere in `agents.py`. `values` and
   `differentiators` are named by explicit paraphrase ("optional seed
   values" / "optional differentiators"), and `company_description` and
   `target_audience` by explicit generic paraphrase — all four only in
   Phase 1. Per the Method section, both a literal identifier and an
   unambiguous single-field paraphrase count as `explicit`, so the
   summary matrix correctly marks all five (`desired_voice`, `values`,
   `differentiators`, `company_description`, `target_audience`) `E` — one
   of them by the strongest possible evidence, the other four by
   paraphrase.

2. **`company_name` is referenced only ambiguously or indirectly,
   everywhere.** No prompt says "company name" in prose; the closest
   evidence is `purpose_vision_writer`'s generic "specific to the
   company" and `PositioningOutput`'s `[company]` template placeholder.

3. **`existing_brand_material` and `wiki_path` are never referenced,
   anywhere, even ambiguously** — a distinct category from `company_name`
   (which at least gets indirect echoes). Neither field's name, nor any
   plausible paraphrase of it, appears in any agent prompt or
   `structured_output` description across all 5 phases.

4. **All 6 visual-identity fields
   (`color_inspiration`, `color_palettes`, `selected_palette_index`,
   `visual_style`, `typography_preference`, `interface_density`) are
   never referenced anywhere**, including in Phase 3 — the phase whose
   subject matter overlaps them most directly. See the Phase 3 Finding
   above.

5. **Phases 3, 4, and 5 have zero explicit `BrandingMission` field
   references in any agent prompt.** Every reference found in these three
   phases is either to same-phase artifacts or to upstream phase
   *outputs* — never to the raw mission. This is flagged as a
   significant finding requiring a product decision before the
   allowlist-implementation issue proceeds: it would mean these three
   phases' hashes/task strings could, in principle, become fully
   mission-field-agnostic, even though `_phase_task` and
   `phase_input_hash` today unconditionally include the full mission for
   every phase regardless of what its prompts ask for.

6. **`BrandComplianceAgent` is out of scope for this document's
   per-phase LLM-prompt allowlists.** `agents.py:1240-1309` directly and
   literally accesses `mission.values`, `mission.differentiators`,
   `mission.company_name`, and `mission.target_audience`
   (`agents.py:1268-1272`) — a stronger, more direct reference than
   anything found in any `AgentPromptSpec`. However, its own docstring
   and the codebase's own commentary confirm it is a plain-Python,
   non-LLM, non-Strands class that "runs outside the graph" via keyword
   matching (`graphs/shared.py:47`, `orchestrator.py:503`,
   `tests/test_full_pipeline_first_run_benchmark.py:33`). It is not built
   from an `AgentPromptSpec`, is not one of the graph's 37 agent-role
   factories, and is not consulted by `_phase_task` or
   `phase_input_hash` — those two functions scope only the LLM graph
   nodes' task/hash payloads. `BrandComplianceAgent`'s direct mission
   access is real but orthogonal to this document's question and to the
   epic's hashing/injection mechanism; it is called out here so a future
   reader is not confused by its presence into thinking it contradicts
   the per-phase findings above.

7. **Generic "the branding mission" references cannot be read as "no
   fields needed."** See the dedicated Limitation section above. Every
   `none`/`empty` cell in the summary matrix below reflects an absence of
   *textual* evidence, not a proof of *behavioral* independence — this is
   most consequential for Phase 1 (5 of 6 agents open with an unqualified
   "given a branding mission") and Phase 2 (`Storyteller`, `TaglineWriter`,
   `PersonaBuilder` all cite "the branding mission" with no field named).
   Implementers should treat this document's recommended allowlists as
   hypotheses to validate, not settled facts, before narrowing what
   `phase_input_hash`/`_phase_task` include.

## Summary matrix — field × phase

`E` = explicit, `A` = ambiguous (not counted in the recommended
allowlist), blank = none.

| Field | Phase 1 | Phase 2 | Phase 3 | Phase 4 | Phase 5 |
|---|:---:|:---:|:---:|:---:|:---:|
| `company_name` | A | | | | |
| `company_description` | E | | | | |
| `target_audience` | E | | | | |
| `values` | E | A | A | | A |
| `differentiators` | E | | | | |
| `desired_voice` | | E | | | |
| `existing_brand_material` | | | | | |
| `wiki_path` | | | | | |
| `color_inspiration` | | | | | |
| `color_palettes` | | | | | |
| `selected_palette_index` | | | | | |
| `visual_style` | | | | | |
| `typography_preference` | | | | | |
| `interface_density` | | | | | |

## Recommended field allowlists (submitted for review)

| Phase | Recommended allowlist |
|---|---|
| STRATEGIC_CORE | `(company_description, target_audience, values, differentiators)` — `company_name` flagged ambiguous, not included |
| NARRATIVE_MESSAGING | `(desired_voice,)` |
| VISUAL_IDENTITY | `()` — see Finding 5 |
| CHANNEL_ACTIVATION | `()` — see Finding 5 |
| GOVERNANCE | `()` — see Finding 5 |

These are conclusions submitted for review, not accepted findings. The
field-allowlist conclusions above must be reviewed and explicitly
accepted before any dependent implementation work (the code changes that
introduce and populate the allowlist in `phase_input_hash`/`_phase_task`)
begins — this document does not self-certify that review by existing or
being merged. Per the Limitation section above, that review should
include empirical validation (e.g. via an extended
`backend/agents/branding_team/scripts/eval_selective_context.py`) before
an allowlist derived from
these tables is treated as safe to ship, particularly for phases where an
agent's only mission reference is the generic, unqualified phrase "the
branding mission."

### Post-review addendum: four allowlists widened beyond this document's tables

Code review on the implementation PR flagged conclusions in this
document's tables as carrying a real correctness risk this document's
prompt-text-only method could not see, and the shipped allowlists were
widened accordingly rather than following the tables above verbatim:

- **STRATEGIC_CORE now also includes `company_name`.** This document
  correctly found no *prompt-text* citation of it (Finding 2), but
  excluding it from the cache key means two missions that differ only in
  `company_name` (e.g. an existing brand renamed, or two distinct brands
  with otherwise-identical mission data) hash identically and share a
  cache entry — the later run silently reuses strategic/downstream output
  generated for a different company. That is a cache-correctness risk,
  not a prompt-grounding question, and the prompt-citation method this
  document uses cannot rule it out either way — only widening the
  allowlist can.
- **STRATEGIC_CORE now also includes `existing_brand_material`.** Finding
  3 correctly found it "never referenced, anywhere, even ambiguously," but
  that finding is about prompt-text citation, not about whether the field
  ever reached an agent — before this document's mechanism existed,
  `_phase_task` injected the entire mission into every phase's task string
  regardless of citation, so `existing_brand_material` already reached
  every Phase 1 agent as raw context, including `discovery_auditor`, whose
  entire job is auditing *current* brand perception, market position, and
  SWOT — the closest match anywhere in the pipeline for content field
  literally named "existing brand material." Shipping the recommended
  exclusion would have been the first time this field stopped reaching
  that agent at all, the same category of functional regression the
  VISUAL_IDENTITY entry below already establishes precedent for. `wiki_path`
  (Finding 3's other never-referenced field) is not included anywhere: it
  is a path, not prose content an agent's text prompt can act on, so unlike
  `existing_brand_material` there is no plausible grounding story for it.
- **NARRATIVE_MESSAGING now also includes `company_name`.** A first pass
  reasoned that STRATEGIC_CORE's `company_name` inclusion above was
  sufficient, since Phase 2 sees STRATEGIC_CORE's output as upstream
  context — but `StrategicCoreOutput` (`models.py`) has no field that
  echoes the raw company name back; nothing guarantees a rename actually
  changes what STRATEGIC_CORE's compositor writes, or that any writer/
  tagline-style agent downstream ever sees the company's actual name.
  Without `company_name` directly in this phase's own allowlist, a rename
  can leave Phase 2's cache entry unaffected *and* its narrative/tagline
  agents (`Storyteller`, `TaglineWriter`, `MessageMapper`, ...) with no
  company identity anywhere in their prompt. This generalizes past this
  one instance: a downstream phase's `mission_fields` allowlist should not
  assume a field's presence in an *upstream phase's own* allowlist
  substitutes for that field reaching the downstream phase — only a field
  actually present in a phase's own mission-field allowlist, or echoed by
  name in an upstream output model, is guaranteed to reach it.
- **VISUAL_IDENTITY now also includes all six visual-identity-only
  fields** (`color_inspiration`, `color_palettes`,
  `selected_palette_index`, `visual_style`, `typography_preference`,
  `interface_density`). Finding 4/the Phase 3 Finding already flagged
  that these are the mission's only user-supplied visual-preference
  input and that excluding them "requires a product decision" rather
  than being silently assumed. Before this document's mechanism existed,
  `_phase_task` injected the *entire* mission into every phase's task
  string regardless of prompt-text citation, so these fields already
  reached the Phase 3 agents as raw context; shipping the recommended
  `()` would have been the first time they stopped reaching those agents
  at all — a functional regression a `dummy`-provider test suite cannot
  catch. Absent the empirical validation this document already
  recommends before narrowing further, the safer default is to keep
  grounding Phase 3 in the user's actual color/typography selections.

Every other phase's allowlist (`CHANNEL_ACTIVATION`, `GOVERNANCE`) ships
exactly as recommended above. This addendum documents the deviation for
future readers of this document; it does not change any table or finding
above, which remain the underlying evidence record.
