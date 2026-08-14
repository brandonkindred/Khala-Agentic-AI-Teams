# ADR-015 — Persona/state override precedence contract for `invoke_generated_agent`

- **Status**: Proposed — design-note only, no behavior change. Locks the contract the
  runtime-binding implementation story (and its sibling contract-tests story) build
  against.
- **Date**: 2026-08-12
- **Owner**: Agent Team Studio / Agentic Team Provisioning
- **Related**:
  - Epic: "Agent Studio: bind saved manifest persona at generated-agent invoke time" —
    this ADR is that epic's first locked artifact. The contract tests and the runtime
    change itself are separate sibling/child stories this ADR is written to unblock.
  - `backend/agents/agent_team_studio/agentic_team_provisioning/runtime/agent_builder.py:257-331`
    — `invoke_generated_agent` / `_invoke_generated_agent_sync`, the entrypoint this
    contract governs.
  - `backend/agents/agent_team_studio/agentic_team_provisioning/models.py:296-334` —
    `GeneratedAgentInvokeInput` / `GeneratedAgentInvokeOutput`, the request/response
    schema this contract extends.
  - `backend/agents/agent_registry/models.py:171-225` — `AgentStateSpec` /
    `AgentManifest`, the persisted persona/state source this contract binds to.
  - `backend/shared/agent_invoke/shim.py:67-217` and
    `backend/shared/agent_invoke/dispatch.py:28-51` — the sandbox invoke path
    (`POST /_agents/{agent_id}/invoke` → `invoke_entrypoint`) whose manifest
    resolution this contract's identity rule depends on.
  - `backend/agents/agent_team_studio/agentic_team_provisioning/runtime/pipeline_runner.py:268-294`
    (`_run_agent`) and
    `backend/agents/agent_team_studio/agentic_team_provisioning/api/services/testing.py:208-269`
    (`send_test_chat_message`) — the two other invoke paths this ADR scopes in, and
    rules trivially unaffected.
  - `backend/agents/agent_team_studio/agentic_team_provisioning/roster_resolve.py:40-151`
    — `persona_from_manifest`, `resolve_persona`, and
    `llm_persona_lists_explicitly_empty`, whose presence-based "explicit vs. absent"
    pattern this contract reuses.
  - `backend/agents/agent_team_studio/agent_studio/registration.py:1-164` — Studio's
    `build_studio_agent_manifest`, which stamps the same shared entrypoint onto
    saved Studio agents, so they are bound by this same contract, not a separate one.

## Context

A single shared callable, `invoke_generated_agent`, serves every generated
agentic-team agent *and* every saved Agent Studio agent — `registration.py` stamps
the identical dotted entrypoint (`GENERATED_AGENT_ENTRYPOINT`) onto Studio manifests
specifically so a saved Studio agent runs through the same runtime a generated team
agent does. Today that function reconstructs the agent's persona **entirely from the
caller-supplied request body** (`GeneratedAgentInvokeInput.role` /
`.skills` / `.capabilities` / `.expertise`), never consulting the registered
`AgentManifest` at all. This is not an oversight the code is silent about — it is
called out as a tracked, known gap in three separate docstrings today:
`invoke_generated_agent`'s own "Binding caveat" paragraph
(`agent_builder.py:280-288`), `GeneratedAgentInvokeInput`'s "Binding caveat"
paragraph (`models.py:303-313`), and `registration.py`'s module-level
"Runtime-binding caveat" paragraph (`registration.py:9-14`).

Meanwhile `AgentManifest.states` (`agent_registry/models.py:219-224`) — one
`AgentStateSpec` per operating-state persona (planning/executing/researching), each
carrying its own `system_prompt` — is persisted on every manifest but is explicitly
documented as **inert**: "nothing reads it at invoke time"
(`AgentStateSpec`'s own docstring, `agent_registry/models.py:171-183`). Studio's
top-level `AgentDefinition.system_prompt` quick-edit field is folded into the
`executing`-keyed state on save (`registration.py`'s `_manifest_states`,
lines 93-114) specifically so that a saved agent's authored prompt has somewhere
durable to live — but nothing at invoke time ever reads it back out.

This produces two asymmetric problems that a runtime-binding change must reconcile
at once:

1. **Sandbox invoke** (`POST /api/agents/{agent_id}/invoke`, the path
   `invoke_generated_agent` serves): 100% request-body, 0% manifest. A caller who
   omits persona fields gets a bare, near-empty prompt; a Studio-authored
   `system_prompt`/`states` never binds regardless of what was saved.
2. **Pipeline runner** and **Studio test-chat**: 100% manifest
   (`resolve_persona(agent_def.manifest_id)` in both `pipeline_runner.py:285` and
   `testing.py:246`), 0% request body — but by construction, not by any precedence
   decision: neither `PipelineRunner._run_agent`'s `prompt` parameter nor
   `SendTestChatMessageRequest` carries persona fields at all. There is nothing to
   override on those two paths today.

Fixing (1) to make the manifest authoritative must not silently strip existing
callers of the sandbox path of their ability to override a field for one invoke —
some callers legitimately supply an ad hoc `role`/`skills` today, and there is no
sibling issue proposing to remove that capability. The fix must therefore be a
genuine **precedence** rule (manifest as default, explicit request field wins), not
a flag flip to "manifest always" or "body always." This ADR fixes that rule.

## Decision

### Scope

This contract governs `invoke_generated_agent` and its request schema
`GeneratedAgentInvokeInput` — the only invoke path in this system that has both a
resolvable `AgentManifest` *and* a request body capable of carrying persona
overrides. Because Studio-saved agents share the identical entrypoint, they are
covered by this same rule, not a separate Studio-specific one — "Studio invoke" and
"sandbox invoke" are, at the entrypoint level, the same invoke.

The pipeline runner and Studio test-chat paths are explicitly in scope for this
ADR's decision, but the decision for both is trivial and requires no code change:
since neither carries a request-body persona surface, their precedence is, and
remains, "manifest always." A future change that *adds* a persona-override surface
to either path must re-derive its precedence from this same per-field table, not
invent a new one.

### Per-field precedence

For each field, the resolved `AgentManifest` supplies the default value; an
explicitly-present request field overrides it for that single invoke only (never
written back to the manifest):

| Body field | Manifest default source | Notes |
|---|---|---|
| `role` | `persona_from_manifest(manifest).role` — `manifest.summary`, falling back to `manifest.name` | Existing mapping, unchanged (`roster_resolve.py:53-56`). |
| `skills` | non-marker `manifest.tags`, via `skill_tags_from_manifest` | Existing mapping, unchanged. |
| `capabilities` | `[]` | The manifest has no capabilities concept today; the manifest "default" is simply empty unless the request supplies it. |
| `expertise` | `[manifest.team]` when non-empty, else `[]` | Existing mapping, unchanged. |
| `system_prompt` *(new field on `GeneratedAgentInvokeInput`, added by the implementation story)* | `manifest.states[key == state].system_prompt` | **Full replacement**, not a merge, when the request supplies it — the request's `system_prompt` entirely stands in for the composed prompt that would otherwise be built. |
| `state` *(new field, default `"executing"`)* | Selects which `AgentStateSpec` backs `system_prompt`'s manifest default | Meaningful only when the manifest carries a matching, non-blank state; otherwise falls through per the backward-compatibility rule below. |
| `tools` | **Out of scope** — permanently `cognition.tools`-governed | Unchanged by this contract; see "Explicitly out of scope" below. |

`build_system_prompt(agent_name, role, skills, capabilities, tools, expertise)`
remains the generic composer used whenever no manifest `system_prompt` applies (no
matching state, blank state prompt, or no manifest at all) — this ADR does not
replace it, only adds a higher-precedence source ahead of it.

### Explicit-vs-omitted test

"Explicit" means the field's key is present in the *raw* JSON request body — not
merely "not equal to the Pydantic default after validation." `GeneratedAgentInvokeInput`
defaults every persona field to an empty string/list, so a Pydantic-level presence
check cannot distinguish "the caller omitted `skills`" from "the caller explicitly
sent `skills: []` to clear it." The implementation must check raw-body key presence
before validation, mirroring the presence-based pattern this repo already uses for
the identical absent-vs-empty distinction:
`llm_persona_lists_explicitly_empty` (`roster_resolve.py:128-151`) does exactly this
for LLM-authored roster saves. An explicitly empty list or blank string in the body
is a caller clearing that field for this invoke (request wins); an omitted key
inherits the manifest default.

This presence check is purely a **key-detection** step, not a validation bypass:
every value — whether request-supplied or manifest-defaulted — still flows through
`GeneratedAgentInvokeInput.model_validate(...)` exactly as today
(`agent_builder.py:314`). The raw dict is inspected only to decide, per field,
*which* value (request vs. manifest) gets handed to that same validation call; it
never replaces or skips it.

### Manifest resolution identity

Binding must resolve the manifest through the same trusted lookup the sandbox shim
already performs from the URL path — `manifest = get_registry().get(agent_id)` at
`shim.py:80`, where `agent_id` is the *route* parameter, not any field inside the
body. `GeneratedAgentInvokeInput.agent_id` (defaulting to `agent_name`) must **not**
be used to re-resolve which manifest's persona binds — it remains solely a
cognition-writeback identity (`call_agent_with_cognition`'s `agent_id` parameter),
exactly as it is used today. A caller cannot claim to be a different registered
agent than the one the URL already committed to.

This is also where today's actual plumbing gap lives: `shim._invoke_and_drive`
(`shim.py:219-248`) calls `dispatch.invoke_entrypoint(manifest.source.entrypoint,
agent_body)`, and `invoke_entrypoint` (`dispatch.py:28-36`) calls
`callable_obj(body)` — the resolved `manifest` itself, or even its id, is never
forwarded past the shim today. Closing that gap (by threading the manifest or its
id through `dispatch.invoke_entrypoint` down to `invoke_generated_agent`) is the
implementation story's job; this ADR only fixes the identity the eventual plumbing
must carry.

**Once resolved, the manifest-to-persona mapping itself must go through the
existing `persona_from_manifest(manifest)` / `resolve_persona(manifest_id)` helpers**
(`roster_resolve.py:40-78`) — the same functions `pipeline_runner.py:285` and
`testing.py:246` already call. `invoke_generated_agent` must not grow a second,
parallel mapping from `AgentManifest` to `role`/`skills`/`capabilities`/`expertise`;
all three invoke paths reuse one resolver, differing only in (a) how they acquire
the manifest identity — a roster row's `manifest_id` for pipeline/test-chat, the
shim's URL-resolved `agent_id` for sandbox invoke — and (b) whether an explicit
request field is allowed to override the resolved value per-field afterward (only
sandbox invoke has a body to override with). This is what keeps the three paths'
*resolution logic* identical even though only one of them exposes an *override*
surface; see Rejected alternatives below for the parallel-resolver approach this
rules out.

### No-manifest fallback

If no manifest is resolvable for the invoke (the shim already 404s before dispatch
when `get_registry().get(agent_id)` returns `None`, so this only applies to a
caller that invokes `invoke_generated_agent` directly, bypassing the shim — e.g. a
test, or any future non-sandboxed caller), every field falls back to pure
request-body values, identical to today's current behavior. This keeps existing
direct callers of the function working unmodified and gives the implementation a
well-defined degraded path rather than a hard failure.

### Backward compatibility

A manifest with an empty `states` list (any manifest authored before the field
existed) has nothing to bind for `system_prompt` — falls through to the existing
generic `build_system_prompt(...)` composer, exactly as every invoke behaves today.
Binding is additive: an agent that never authored states or a `system_prompt` sees
no behavior change at all.

### Explicitly out of scope

- **`tools` field precedence.** Stays permanently `cognition.tools`-governed; the
  request's `tools` field remains inert regardless of presence or absence. This is
  an existing, separate escalation-prevention decision (`agent_builder.py`'s "no
  silent code-exec/network fallback" comment, lines 246-248) unrelated to persona
  binding — this contract does not touch it.
- **Contract tests.** Encoding this table as executable, possibly-red tests is a
  separate sibling story.
- **The runtime implementation itself** — including exactly how the manifest/id is
  threaded from the shim through `dispatch.invoke_entrypoint` down to
  `invoke_generated_agent`, and where the raw-body presence check is performed —
  is a separate implementation story building against this contract.
- **UI cutover, a Drafts API, or sandbox pool lifecycle changes.**
- **Pipeline runner / Studio test-chat request-level overrides** — moot today (see
  Scope above); only relevant if a future change adds a persona-override surface to
  either path, at which point it must reuse this same per-field table.
- **Multi-turn chat session state carry-over semantics.**
- **Studio-UI authoring/validation of `states` content.**
- **Error-handling policy for an invalid/unknown `state` key** (e.g. a request
  supplying a `state` the manifest doesn't carry) — left to the implementation
  story to decide (reject vs. silently fall through to the generic composer).
- **Consolidating `GeneratedAgentInvokeInput`'s persona fields with
  `RosterPersonaView`/`AgentManifest`.** `role` / `skills` / `capabilities` /
  `expertise` already exist on both shapes today — this structural duplication
  predates this ADR (it is what makes an override even expressible: the request
  schema mirrors the manifest-derived view field-for-field so a caller can name
  exactly which field it is overriding). Collapsing that duplication into a single
  shared shape is a schema refactor this contract does not require and does not
  block; it is a separate follow-up if pursued.

## Rejected alternatives

- **Manifest always wins (no request override at all).** Rejected: the sandbox
  invoke path has existing callers that legitimately supply an ad hoc persona for a
  single invoke (there is no sibling story proposing to remove that capability),
  and silently discarding a caller-supplied field would be a breaking behavior
  change disguised as a binding fix.
- **Request body always wins (manifest only fills fields the body omits, using
  Pydantic-default detection rather than raw-body presence).** Rejected: Pydantic's
  post-validation defaults are indistinguishable from an explicit empty value for
  every field on `GeneratedAgentInvokeInput` (`role: str = ""`,
  `skills: list[str] = []`, …), so this approach cannot tell "omitted" from
  "explicitly cleared" and would either always defer to the manifest (breaking
  legitimate overrides) or never defer to it (reintroducing today's bug) depending
  on which way the ambiguity is resolved.
- **Merging the manifest's `system_prompt` with a request-supplied one** (e.g.
  request text appended to the manifest prompt) instead of full replacement.
  Rejected: an implicit merge makes the effective prompt unpredictable to the
  caller and impossible to specify precisely enough for the contract tests this ADR
  exists to unblock; full replacement keeps the rule identical in shape to every
  other field in the table.
- **Resolving the manifest from a body-supplied `agent_id`/`agent_name` instead of
  the shim's URL-resolved one.** Rejected: the body is caller-controlled, so this
  would let a request claim a different agent's persona than the one the URL path
  (and any surrounding authorization) already committed to — a correctness and
  trust-boundary problem, not just a style choice.
- **A second, `invoke_generated_agent`-local function that re-derives
  `role`/`skills`/`capabilities`/`expertise` from `AgentManifest`, parallel to
  `persona_from_manifest`.** Rejected: this is the one alternative that would
  actually reintroduce cross-path inconsistency — two independent, potentially
  drifting mappings from the same `AgentManifest` fields, one used by
  pipeline/test-chat and a second used by sandbox invoke. Reusing
  `persona_from_manifest`/`resolve_persona` (see Manifest resolution identity
  above) is mandatory precisely to avoid this.

## Risks and tradeoffs

- **Two new request fields** (`system_prompt`, `state`) widen
  `GeneratedAgentInvokeInput`'s surface. This is accepted as the minimum needed to
  let a caller override the manifest's bound prompt or pick a non-default operating
  state for one invoke — without them, `system_prompt` binding would have no
  override path at all, re-creating the "manifest always wins" problem this ADR
  rejects above.
- **Raw-body presence checking adds a small amount of pre-validation plumbing**
  (reading the dict before/alongside `GeneratedAgentInvokeInput.model_validate`)
  that the current implementation doesn't need. This mirrors an already-established
  pattern elsewhere in this package (`llm_persona_lists_explicitly_empty`), so it is
  not a new kind of complexity for this codebase, just a new call site for it.
- **The manifest-plumbing gap** (shim → dispatch → entrypoint never forwarding the
  resolved manifest today) means this contract cannot be fully implemented without
  also touching `dispatch.invoke_entrypoint`'s call shape — a small blast-radius
  increase beyond `agent_builder.py` alone, acknowledged here so the implementation
  story doesn't discover it as a surprise.

## Studio manifests are not a distinct structure

Studio-saved agents raise no separate compatibility question because there is only
one `AgentManifest` Pydantic model in this codebase, not a Studio variant and a
generated-team variant. `build_studio_agent_manifest`
(`registration.py:117-164`) constructs a plain `AgentManifest` — same `states:
list[AgentStateSpec]`, same `cognition`, same `source.entrypoint` field types a
generated team manifest has — and the function's own last line,
`return revalidate(manifest)`, re-validates it through that identical model before
returning. `_manifest_states` (`registration.py:93-114`) is what populates
`states` for a Studio agent (folding the top-level `system_prompt` into the
`executing` key), but the *shape* it produces is exactly the `list[AgentStateSpec]`
the generic Per-field precedence and Backward compatibility rules above already
handle — including the empty/legacy case. No guard or conditional is needed in
`invoke_generated_agent` to distinguish a Studio-authored manifest from a
generated-team one; both are read through the same `AgentManifest.states` field by
the same `persona_from_manifest`-based resolution this contract mandates.

## Reconciliation with existing SoT documentation

`agentic_team_provisioning/README.md`'s "Roster identity: thin refs, Manifest SoT"
section and `AGENTIC_TEAM_ARCHITECTURE.md` both already assert the `AgentManifest`
is the sole writable source of truth for persona, and that the roster `PUT` route
rejects a body carrying persona fields with `400`. This ADR does not weaken that
claim: the roster (`AgenticTeamAgent`) remains write-protected exactly as today —
nothing in this contract makes the *roster* writable via invoke-time fields. What
this ADR adds is narrower and invoke-scoped: a **per-invoke, non-persisted**
override of the *runtime persona view* built from that manifest, never written back
to the manifest or the roster. "Source of truth" and "default value with a
per-call override" are compatible claims; this ADR is careful to keep the override
explicitly ephemeral so the two documents do not contradict each other.

## Contract boundary

A future implementation must satisfy exactly this surface:

- `GeneratedAgentInvokeInput` gains `system_prompt: str = ""` and
  `state: str = "executing"` fields alongside the existing `role` / `skills` /
  `capabilities` / `expertise` / `tools` / `agent_id`.
- `invoke_generated_agent` (or its sync core) resolves the manifest via the same
  trusted identity the shim already uses (the route's `agent_id`, threaded through
  `dispatch.invoke_entrypoint` rather than re-derived from the body), maps it to
  persona defaults via the existing `persona_from_manifest`/`resolve_persona`
  helpers (`roster_resolve.py`) — not a new parallel mapping — and, for each of
  `role` / `skills` / `capabilities` / `expertise` / `system_prompt`, uses that
  manifest-derived default unless the raw request body explicitly carries that key
  — per the table and presence test above.
- `system_prompt`, when manifest-sourced, comes from `manifest.states[key ==
  state].system_prompt`; when request-sourced, fully replaces the composed prompt
  rather than merging with it.
- `tools` remains untouched by this change — still always `cognition.tools`, still
  ignoring the request field entirely.
- No manifest resolvable → every field behaves exactly as `invoke_generated_agent`
  does today (pure request-body values).
- Empty/absent `manifest.states` → falls through to `build_system_prompt(...)`
  exactly as today; no new failure mode for pre-existing manifests.
- Pipeline runner and Studio test-chat require no code change under this contract.

## Consequences

- **The precedence question is closed, not deferred.** Every field
  `invoke_generated_agent` touches has an explicit default source, an explicit
  override test, and an explicit fallback — sufficient for the sibling contract-test
  story to write concrete, unambiguous assertions against.
- **No behavior changes as a result of this ADR.** No code ships in this issue;
  `invoke_generated_agent` continues taking every field from the request body
  exactly as it does today until the implementation story lands.
- **The implementation story's job is narrowed to plumbing and tests.** It adds the
  two new request fields, threads manifest identity through the dispatch call
  shape, and implements the raw-body presence check — all against the contract
  fixed here, not one it still needs to design. The three existing "tracked
  follow-up" docstrings (`agent_builder.py`, `models.py`, `registration.py`) and
  the two SoT READMEs are updated alongside this ADR to point at it instead of an
  open-ended caveat.
