# Phase Dependency Analysis — Upstream Context per Phase

**Scope:** analysis only. No agent prompts or orchestrator code were
changed to produce this document. It answers a single question for each
of the 37 branding-team agent system prompts (`backend/agents/branding_team/agents.py`):
*which upstream phase's output does this agent's system prompt actually
reference?*

**Why:** `_PhaseSpec.context_phases` in `orchestrator.py` (`_PHASE_SPEC`,
`~orchestrator.py:357-396`) is `()` — "no filtering" — for every phase
today. `_phase_task` (`orchestrator.py:727-769`) already knows how to
honor a non-empty `context_phases` tuple (it restricts which prior phase
outputs get JSON-serialized into the downstream prompt,
`orchestrator.py:751-753`), but no phase declares one yet.

**Scoped to which execution path, precisely:** `context_phases` and
`_phase_task` are consulted only by `run_single_phase`
(`orchestrator.py:677-725`), which backs two paths — the isolated
per-phase/`phase_cache` branch of `BrandingTeamOrchestrator.run()`
(`orchestrator.py:572-573`, `_run_phases_with_cache`) and every Temporal
activity (`temporal/activities.py` calls `run_single_phase` directly).
`run()`'s **default** branch (`phase_cache is None`, `orchestrator.py:553-560`)
never calls `_phase_task` at all: it builds one monolithic Strands `Graph`
from only the serialized mission, wired as a strict linear chain — each
phase node has exactly one incoming edge, from its immediate predecessor
only (`graphs/top_level.py:59-93`, `builder.add_edge(last_node, p{n}_node)`).

**Correction — the default path does not forward every upstream phase's
output either; it forwards only one hop.** Strands' own node-input
builder (`_build_node_input` in `strands.multiagent.graph`, the
`strands-agents` dependency pinned in `backend/requirements.txt`)
constructs a node's input from the original task plus **only the results
of edges pointing directly at that node** — i.e. its immediate
predecessor(s), not every previously-completed node. Since the top-level
chain is 1→2→3→4→5 with no other edges, Phase 5's node input is "the
mission" + "Phase 4's agent outputs" only; it does not itself contain
Phase 1–3's raw output. (Phase 4's own outputs may *indirectly* reflect
earlier phases, to the extent Phase 4's agents were themselves grounded in
Phase 3's forwarded content when they generated their text — but that is
not the same as the full Phase 1–3 JSON payloads reaching Phase 5, and it
attenuates further with each hop.) `_PhaseSpec.context_phases` has no hook
into this mechanism at all — it isn't a "no filtering, so everything gets
through" situation; it's a structurally different, non-configurable
single-hop-per-phase propagation that the isolated-phase/Temporal path
(via `_phase_task`, which really does serialize *all* `prior_outputs`
unless filtered) does not share.

**Consequence for #6953:** the evidence in this document (which upstream
phase each agent's prompt references) is directly actionable for the
isolated-phase/Temporal path via `context_phases`. It says nothing about
whether the default monolithic-graph path already gives each phase
enough of the *right* upstream content — that path's one-hop-only
forwarding is a separate, pre-existing behavior `context_phases` cannot
change, and #6953 (or a follow-up) needs to decide whether the default
path also needs an explicit multi-hop context mechanism (mirroring what
`_phase_task` does) rather than relying on whatever survives one hop of
Strands' graph-edge forwarding.

**Method:** every agent's system prompt is fully data-driven —
`AgentPromptSpec.opening` + `fields`/`structured_output`-derived field
lines + optional `closing`, rendered verbatim by `render_agent_prompt`
(`prompt_spec.py:117-141`) with no hidden boilerplate injected elsewhere.
So reading each `_..._PROMPT = AgentPromptSpec(...)` literal in `agents.py`
is reading the complete system prompt text sent to the LLM. A phase is
counted as "referenced" only when an agent's prompt text explicitly names
that phase's output (by concept — "strategic core", "narrative",
"moodboard direction", "writing guidelines", "visual identity" — or by
field, e.g. "positioning, values, audience segments, differentiation").
Same-phase artifacts (e.g. Phase 3's `converge_decider` output referenced
by later Phase 3 agents) don't count as an upstream-phase dependency.

## Phase 1 — Strategic Core (6 agents)

No upstream phase exists. All six factories (`discovery_auditor`,
`purpose_vision_writer`, `values_articulator`, `audience_segmenter`,
`differentiation_mapper`, `positioning_synthesizer`) work only from the
branding mission and, for `positioning_synthesizer`, the other five Phase 1
fragments (same phase, not upstream).

**Recommended `context_phases`:** `()`

## Phase 2 — Narrative & Messaging (6 agents)

| Agent | Prompt text (verbatim excerpt) | Upstream phase referenced |
|---|---|---|
| `Storyteller` | "Using the strategic core output and branding mission" | STRATEGIC_CORE |
| `ArchetypeAnalyst` | "Using the branding mission and strategic core output as context" | STRATEGIC_CORE |
| `TaglineWriter` | "Using the branding mission and strategic core output as context" | STRATEGIC_CORE |
| `MessageMapper` | "Using the branding mission and strategic core output (positioning, values, audience segments, differentiation) as context" | STRATEGIC_CORE |
| `PersonaBuilder` | "Using the branding mission and the strategic core's audience segments as context" | STRATEGIC_CORE |
| `VoicePrinciplesDrafter` | "Using the branding mission's desired_voice and the strategic core output as context" | STRATEGIC_CORE |

All six Phase 2 agents reference Phase 1 explicitly; none reference each
other's fragments (they run in a parallel fan-out) or any phase further
upstream (there is none).

**Recommended `context_phases`:** `(STRATEGIC_CORE,)` — matches the shape
proposed in #6953.

## Phase 3 — Visual & Expressive Identity (9 distinct factories, 11 graph nodes)

| Agent | Prompt text (verbatim excerpt) | Upstream phase(s) referenced |
|---|---|---|
| `MoodBoardConceptualist_{variant}` (×3) | "Given a brand's strategic core and narrative, create a moodboard concept" | STRATEGIC_CORE, NARRATIVE_MESSAGING |
| `converge_decider` | "You receive moodboard candidates from the diverge phase plus the brand's strategic core and values" | STRATEGIC_CORE (moodboard candidates are same-phase) |
| `logo_specifier` | "Based on the winning moodboard direction, define a logo suite" | *none explicit* — only the same-phase `converge_decider` winner |
| `color_system_builder` | "Based on the winning moodboard direction, define {N} colors" | *none explicit* — same-phase only |
| `typography_builder` | "Based on the winning moodboard direction, define a typography system" | *none explicit* — same-phase only |
| `iconography_director` | "Based on the winning moodboard, define:" | *none explicit* — same-phase only |
| `photography_video_director` | "Based on the winning moodboard, define:" | *none explicit* — same-phase only |
| `voice_tone_builder` | "Using the brand narrative's writing guidelines and the moodboard direction, define:" | NARRATIVE_MESSAGING (+ same-phase moodboard) |
| `design_system_codifier` | "Based on the full visual identity work, produce:" | *none explicit* — same-phase only |

6 of 9 Phase 3 factories reference no upstream phase at all in their
prompt text — they only build on the same-phase `converge_decider` output.
Only `MoodBoardConceptualist` (STRATEGIC_CORE + NARRATIVE_MESSAGING),
`converge_decider` (STRATEGIC_CORE), and `voice_tone_builder`
(NARRATIVE_MESSAGING) explicitly need upstream context. Since the phase
receives one shared task string, the phase-level set must satisfy the
neediest agents.

**Recommended `context_phases`:** `(STRATEGIC_CORE, NARRATIVE_MESSAGING)` —
matches the shape proposed in #6953.

## Phase 4 — Experience & Channel Activation (9 factories)

| Agent | Prompt text (verbatim excerpt) | Upstream phase(s) referenced |
|---|---|---|
| `brand_experience_principler` | "You are a Brand Experience Architect. Define:" | *none* |
| `{channel}_guide` ×6 (website/social/email/events/partnerships/internal) | "Define guidelines for the {channel} channel:" … "Context: {static channel description}" | *none* — "Context" is a hardcoded one-line channel description (e.g. "Company website, landing pages, product pages."), not upstream phase output |
| `brand_architecture_builder` | "You are a Brand Architecture Specialist. Define:" | *none* |
| `brand_in_action_illustrator` | "Create {N} applied examples showing correct vs incorrect brand usage:" | *none* |

**Finding — disagrees with #6953's assumed default.** None of the 9 Phase 4
system prompts mention Phase 1, 2, or 3 output by name or concept (no
"strategic core", "positioning", "narrative", "tagline", "voice",
"moodboard", "logo", "color", "typography", or "visual identity" anywhere
in `agents.py:785-1037`). Every Phase 4 agent's prompt is self-contained:
it describes only its own channel/topic and asks the model to "Define:".
Per the acceptance criteria of #6953 ("Phase 4 task prompt contains
`strategic_core` + `narrative_messaging` + `visual_identity` context"),
that default would inject three full upstream JSON payloads into prompts
whose own text never references any of that content. This may still be a
deliberate product choice (e.g. giving the model brand context it isn't
explicitly told to use, so channel guides stay on-brand even without an
explicit instruction to consult it) — but it is **not evidence-based** the
way Phases 1–3, 5 are. Flagging for a decision rather than silently
recommending an empty tuple, since an LLM channel-guide agent producing
guidance with zero grounding in the brand's actual identity is a real
regression risk even though today's prompts don't ask for that grounding
explicitly.

**Correction:** the empty tuple cannot represent "inject nothing" as
`_PhaseSpec.context_phases` and `_phase_task` are implemented today
(`orchestrator.py:750-753`): `if spec.context_phases:` treats an empty
tuple as "no filtering configured," which falls through to including
*every* `prior_outputs` entry — the same all-upstream-context behavior as
never touching `_PHASE_SPEC` at all. So `context_phases = ()` for Phase 4
would not produce the zero-grounding behavior the evidence above supports;
it is operationally identical to
`(STRATEGIC_CORE, NARRATIVE_MESSAGING, VISUAL_IDENTITY)`. Representing
"deliberately no upstream context" requires either a distinct sentinel
(e.g. a dedicated "explicitly empty" marker distinguishable from "unset")
or inverting the default so unset means "no context" and phases opt in —
both are code changes in #6953's scope, not something this evidence-only
analysis can resolve by picking a tuple value.

**Recommendation:** #6953 must decide, as a product/mechanism question,
between (a) extending `_PhaseSpec`/`_phase_task` with a way to express
"no upstream context" and setting it for Phase 4, or (b) keeping
`(STRATEGIC_CORE, NARRATIVE_MESSAGING, VISUAL_IDENTITY)` for Phase 4 as a
deliberate choice to ground channel-guide agents in brand identity even
though no current prompt text asks for it. Either way, don't set Phase 4's
`context_phases` to `()` expecting it to mean "none" — today it means "all."

## Phase 5 — Governance & Evolution (7 agents)

| Agent | Prompt text (verbatim excerpt) | Upstream phase(s) referenced |
|---|---|---|
| `ownership_definer` | "You are a Brand Ownership Definer. Define:" | *none* |
| `approval_workflow_designer` | "You are an Approval Workflow Designer. Define:" | *none* |
| `asset_wiki_planner` | "You are an Asset & Wiki Planner. Define:" | *none* |
| `training_planner` | "You are a Training Planner. Define:" | *none* |
| `kpi_designer` | "You are a Brand KPI Designer. Define:" | *none* |
| `evolution_framer` | "You are a Brand Evolution Framer. Define:" | *none* |
| `brand_rules_codifier` | "Using the full brand context (positioning, promise, values, narrative, visual identity), produce:" | STRATEGIC_CORE (positioning, promise, values), **NARRATIVE_MESSAGING** (narrative), VISUAL_IDENTITY |

Only `brand_rules_codifier` references upstream context explicitly, and it
names all three of STRATEGIC_CORE, NARRATIVE_MESSAGING, and
VISUAL_IDENTITY — not just STRATEGIC_CORE + VISUAL_IDENTITY.

**Finding — disagrees with #6953's assumed default.** #6953 proposes
`(STRATEGIC_CORE, VISUAL_IDENTITY)` for Phase 5, omitting
NARRATIVE_MESSAGING. But `brand_rules_codifier`'s prompt literally lists
"narrative" alongside "positioning, promise, values" and "visual identity"
as part of "the full brand context" it's told to use. Dropping
NARRATIVE_MESSAGING would contradict this agent's own prompt text. No
Phase 5 agent references CHANNEL_ACTIVATION, so excluding it (per #6953's
acceptance criteria) is correctly evidence-based.

**Recommended `context_phases`:** `(STRATEGIC_CORE, NARRATIVE_MESSAGING, VISUAL_IDENTITY)`

## Summary table

| Phase | #6953's proposed `context_phases` | Evidence-based `context_phases` | Agreement |
|---|---|---|---|
| STRATEGIC_CORE | `()` | `()` | ✅ |
| NARRATIVE_MESSAGING | `(STRATEGIC_CORE,)` | `(STRATEGIC_CORE,)` | ✅ |
| VISUAL_IDENTITY | `(STRATEGIC_CORE, NARRATIVE_MESSAGING)` | `(STRATEGIC_CORE, NARRATIVE_MESSAGING)` | ✅ |
| CHANNEL_ACTIVATION | `(STRATEGIC_CORE, NARRATIVE_MESSAGING, VISUAL_IDENTITY)` | no prompt evidence for any upstream phase, **but `()` cannot express that** — see Phase 4 section | ⚠️ mechanism gap, needs a #6953 decision |
| GOVERNANCE | `(STRATEGIC_CORE, VISUAL_IDENTITY)` | `(STRATEGIC_CORE, NARRATIVE_MESSAGING, VISUAL_IDENTITY)` | ⚠️ missing NARRATIVE_MESSAGING |

Three of five phases match the proposed shape exactly. The two mismatches
(Phase 4, Phase 5) are flagged above with the specific prompt lines that
justify each recommendation, for #6953 to resolve before finalizing
`_PHASE_SPEC`. Note the STRATEGIC_CORE row's `()` is not the same kind of
value as Phase 4's: Phase 1 has no upstream phases to filter (`prior_outputs`
is empty regardless of `context_phases`), so `()` there is inert rather than
meaning "include everything."
