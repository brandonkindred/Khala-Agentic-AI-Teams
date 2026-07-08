# ADR-009 — Typed-IO registry-agent execution in the free-text agentic DAG

- **Status**: Accepted
- **Date**: 2026-07-08
- **Owner**: Agentic Team Provisioning / Agent Studio
- **Related**:
  - `system_design/adr/ADR-008-typed-io-registry-agents-in-free-text-dag.md` — records the v1 scope
    boundary (registry agents run as free-text personas) and defers this spike; this ADR resolves it
    and supersedes ADR-008's "Follow-up design spike" section.
  - `system_design/adr/ADR-007-founder-agentic-team-adapter-collapse.md` — owns the founder→pipeline
    adapter contract this ADR confirms is unaffected.
  - `docs/design/agent-studio-ux-spec.md` — §6 (Risks: "Typed-IO registry agents in a free-text DAG")
    and §7 (deferred decision #2), both updated to point here.

## Context

ADR-008 deferred four open questions that must be resolved before any code adds a
`source == "registry"` execution branch to `pipeline_runner.py`: boundary marshalling, validation &
coercion, schema fidelity, and adapter coupling. This ADR is that resolution. It is a design decision
only — it authorizes no code change to `pipeline_runner.py`, `api/main.py`, or `agent_registry/*` by
itself; a future implementation PR is gated on this ADR existing, per ADR-008's revisit trigger.

Facts this ADR reasons from (confirmed in the current codebase):

- `pipeline_runner.py` threads a single free-text `prev_output: str` between `ProcessStep`s.
  `_handle_action_step` builds one prompt string per step and calls `_run_agent`, which builds every
  roster agent — `source: "generated"` or `"registry"` alike — as a free-text LLM persona via
  `build_agent`/`call_agent`. There is no `source == "registry"` branch today, by design.
- `_roster_agent_from_manifest` (`api/main.py`) projects an `AgentManifest` onto an `AgenticTeamAgent`
  roster entry and discards `manifest.inputs`/`manifest.outputs` entirely.
- `AgentManifest.inputs`/`outputs: IOSchema | None` (`agent_registry/models.py`) carry either a
  `schema_ref` (dotted import path, resolved lazily via `agent_registry/schema_resolver.py`) or an
  `inline_schema` (literal JSON Schema, authoritative when present).
- **The runtime-binding caveat.** Even a Studio-authored agent with an authored `inline_schema` still
  sets `source.entrypoint` to the *shared* generated-agent entrypoint
  (`agentic_team_provisioning.runtime.agent_builder:invoke_generated_agent`, currently a private
  `_GEN_ENTRYPOINT` constant in `agent_studio/registration.py`). That entrypoint's own docstring
  states plainly that invoke "runs through the shared generated-agent entrypoint regardless — runtime
  binding of the authored schema is the separate deferred follow-up." The dispatch call site
  (`shared_agent_invoke/shim.py`) resolves the manifest before invoking but only forwards `body` to
  `invoke_entrypoint(entrypoint, body)` — manifest identity is dropped at that one call, not lost
  system-wide. A **different** class of registry agent — hand-authored specialists with a genuinely
  custom `source.entrypoint` (e.g. `blogging.planner`) — has no such indirection: their advertised
  schema *is* the contract their entrypoint code was written against.
- ADR-007's founder collapsing adapter maps every `WAIT` step to exactly one free-text question with
  empty `options`, and every terminal pipeline status to `PipelineRunStatus`/`error: str`. It never
  inspects step-level typing.

## Decision

A `ProcessStep` gets typed-IO DAG execution only when **all** of the following hold; any one missing
keeps it on the unchanged free-text persona path:

1. Its registry agent's manifest advertises a **custom `source.entrypoint`** (not the shared
   generated-agent entrypoint) — which includes *all* LLM-generated agents and, today, *all*
   Studio-authored agents regardless of whether they carry an authored schema.
2. The step **explicitly opts in** via a new `ProcessStep.typed_io: bool = False` field.
3. The manifest's `inputs` and `outputs` are both **present and string-bindable**: each resolves to a
   real JSON Schema (`resolve_schema`/`inline_schema` — neither is `None`), and the schema's bound
   property (named by `input_field`/`output_field`, defaulting to `"input"`/`"output"`) exists, is
   typed `"string"`, and is the *only* required property (any other property must be optional or carry
   a default).

All three conditions are required — see the rationale below for each. A future implementation checks
all three before choosing between the existing persona branch and a new typed branch. Condition 3 is
checked at process-design/save time (extending `roster_validation.py`'s existing depth-check pattern —
the same place that already validates roster completeness), so an author gets immediate feedback that a
step is ineligible rather than a run failing mid-flight the first time it reaches that step.

**Why the opt-in flag is required, not just the entrypoint check (condition 2).** An entrypoint-only
gate would silently reinterpret *already-persisted* processes: any existing `ProcessStep` roster entry
that already references a custom-entrypoint manifest (e.g. `blogging.researcher` →
`ResearchBriefInput.brief: str`, `blogging.publication` → `SubmitDraftInput.draft: str`,
`job_matching.scanner` → `ScannerInvokeRequest.queries: list[str]` — none of which accept a generic
`input` key) would flip from working persona execution to typed execution the moment this ADR's
implementation lands, and immediately fail schema validation under the default `{"input": prev_output}`
binding, because old step documents cannot carry `input_field`/`output_field` (they predate this ADR).
Gating on `typed_io` (default `False`, so every existing `ProcessStep` deserializes to the unchanged
persona behavior) makes typed execution something a process author turns on deliberately, for a step
they've verified is eligible — never something that happens to a persisted process as a side effect of
this ADR landing.

**Why schema presence and string-bindability are required, not just typed_io (condition 3).** Without
it, an author could set `typed_io=True` on a step whose manifest has no schema to resolve at all (e.g.
`blogging.fact_checker`, which declares neither `inputs` nor `outputs`) or only a partial one (e.g.
`blogging.publication`, which has `inputs` but no `outputs`) — the new runner branch (§2) would have
nothing to validate against, an unspecified failure mode condition 1/2 alone don't rule out. And because
the DAG has exactly one free-text `str` channel (§1), a schema whose bound property isn't a plain
`"string"` — e.g. `job_matching.scanner`'s `ScannerInvokeRequest.queries: list[str]`, its only,
required, non-string property — can *never* be satisfied by binding `prev_output` directly: a JSON
Schema validator checks types, it does not coerce a string into a list. Rather than build coercion
machinery for that case, condition 3 makes such a manifest ineligible for `typed_io=True` outright — a
decidable exclusion, not an unspecified crash.

Rejected: entrypoint-plus-opt-in as the complete gate (the ADR's previous revision). It is not
well-formed for every custom-entrypoint manifest — see the concrete failures above — and would leave
"validate with `jsonschema.Draft202012Validator`" (§2) an unfulfillable promise for non-string schemas.
Rejected, separately: building a coercion layer (parsing `prev_output` as JSON, type-casting, etc.) so
non-string schemas could still bind. That reproduces the "generic template/expression mapping language"
this ADR already rejects in §1 as over-engineered for a DAG with exactly one string channel — excluding
the incompatible manifests is simpler and just as decidable.

### 1. Boundary marshalling

`ProcessStep` gains two new optional fields, `input_field: Optional[str]` and
`output_field: Optional[str]` (both default `None`, so every existing step document deserializes
unchanged). They name which top-level property of the target registry agent's resolved input/output
schema the DAG's one free-text channel binds to — a property the Decision's condition 3 guarantees is
typed `"string"` and is the schema's only required property, so binding `prev_output` directly always
satisfies validation; no type coercion is ever needed for a step that passed the gate.

- **Inbound:** the runner builds `{step.input_field or "input": prev_output}` and validates it against
  the manifest's resolved input schema before dispatch (validates only — no coercion is needed or
  attempted; condition 3 guarantees the bound property is already string-typed).
- **Outbound:** after a successful invoke, the returned object is **first validated against the
  manifest's resolved `outputs` schema** (§2) — an entrypoint that returns something not conforming to
  its own advertised output contract fails the step; it is never passed downstream unchecked. Only once
  validation passes: if the output object has a string property named `step.output_field or "output"`,
  that string becomes the next `prev_output`; otherwise the whole output object is JSON-serialized and
  *that* string becomes `prev_output`. A `str` is always produced, so any downstream consumer — a
  `WAIT` step's display, a persona step's prompt, the pipeline's final result — degrades gracefully to
  reading structured output as text.
- **Typed↔free-text `WAIT` transitions** need no special-casing under this model: a `WAIT` answer and a
  typed step's serialized output are both just free-text producers feeding the same binding path
  described above and in §5.

Rejected: a generic template/expression mapping language (jq-like) between steps — over-engineered for
a DAG with exactly one string channel. A single named-property binding is the minimum mechanism that
makes the boundary decidable without redesigning the DAG's data model.

### 2. Validation & coercion

Both happen in a **new runner-owned code path**, not at projection and not inside the invoked
entrypoint:

- Rejected "at projection" — `_roster_agent_from_manifest` is a static, list-time operation with no
  runtime string to validate against.
- Rejected "inside the invoked entrypoint" — that reproduces the runtime-binding caveat per entrypoint,
  and shared-envelope entrypoints have nothing meaningful to validate against anyway.
- Concretely, a new sibling handler to `_handle_action_step`, dispatched only under this ADR's gate,
  will: resolve the `AgentManifest` via `agent_registry.get_registry()` → resolve its input schema via
  the existing `agent_registry/schema_resolver.py` (the same resolution the catalog's `/schema/input`
  endpoint already performs — no new resolution logic) → validate the bound body with
  `jsonschema.Draft202012Validator` (condition 3 above guarantees this validates, never coerces) → on
  success, dispatch through the **existing sandboxed invoke surface** (`POST /api/agents/{agent_id}/
  invoke`, `backend/unified_api/routes/agents.py`) rather than calling
  `shared_agent_invoke/dispatch.py:invoke_entrypoint` directly — see "Preserve the registry invoke
  boundary" below — poll through any `202` warming response (see below) until a terminal reply arrives,
  then **unwrap the sandbox envelope's `output` key** (the route's success body is
  `{"output": <entrypoint's real return value>, "duration_ms", "trace_id", "logs_tail", ...}` — the
  entrypoint's actual result lives inside `output`, not at the envelope's top level) → resolve
  `outputs` via the same `schema_resolver.py` and validate the **unwrapped** object against it before
  handing it to §1's outbound extraction.

**Handling the invoke route's three response classes.** `POST /api/agents/{agent_id}/invoke` has three
outcomes, not two — the typed branch must treat them distinctly:

- **Terminal success (`200`)** — the envelope described above; unwrap `output`, validate, extract per
  §1.
- **Non-terminal warming (`202`, with a `Retry-After` header)** — returned when the target sandbox is
  cold; the agent has **not** run yet. This is neither success nor failure: the runner must retry after
  the advertised delay (bounded by the same kind of timeout `_handle_wait_step` already applies to its
  own poll loop) until a `200` or an error response arrives. Treating `202` as success would validate
  warming-status metadata as if it were the agent's output and mark the step complete without the agent
  having run at all.
- **Terminal failure (`404`/`409`/`503`/`502`/any other non-2xx)** — the existing 409
  `requires-live-integration` gate lands here (see below); any of these fails the step.

**Failure mode: fail the step.** A validation failure (inbound *or* outbound), a terminal-failure
response from the invoke route, or any other entrypoint/sandbox exception routes to the existing
`try_fail_pipeline_run` CAS path — the same mechanism `_execute`'s blanket exception handler already
uses today.

- Rejected "fall back to persona mode" — silently unenforced typing defeats the purpose of doing typed
  execution at all.
- Rejected "surface as a `WAIT` question" — would make a `StepType.ACTION` step conditionally behave
  like `StepType.WAIT`, breaking the DAG author's declared step-type invariant that ADR-007's adapter
  relies on to detect `WAIT` steps.

**Preserve the registry invoke boundary.** The typed branch must not call
`shared_agent_invoke/dispatch.py:invoke_entrypoint` directly in-process. That primitive is the sandbox
shim's internal dispatch call (`shared_agent_invoke/README.md`: "the shim does not run inside production
team services; it lives only inside the sandbox container") and carries no guardrail logic of its own —
the `requires-live-integration` 409 gate and the ephemeral-sandbox-acquire lifecycle live solely in the
HTTP route layer (`backend/unified_api/routes/agents.py`), which is mounted directly on the **Unified
API's own process** (`unified_api/main.py`). Calling `invoke_entrypoint` directly would execute a
manifest like `job_matching.scanner` or `blogging.publication` (both tagged `requires-live-integration`)
in the wrong process, with none of the guardrails that route enforces today, and would silently swap
that manifest's documented "409 — invoke through your team's production API instead" contract for an
unguarded direct call.

**This is a real network call, not an in-process one.** `agentic_team_provisioning` — where
`pipeline_runner.py` runs — is a normally-proxied team in `unified_api/config.py`'s `TEAM_CONFIGS`
(`in_process` is not set, unlike e.g. `agent_studio`/`product_delivery`), so it runs as its own service,
separate from the Unified API process the invoke route is mounted on. The typed branch must therefore
make a genuine HTTP call to the invoke route, using the same base-URL/HTTP-client pattern an existing
cross-team caller already establishes (`planning_team/adapters/market_research.py`'s
`UNIFIED_API_BASE_URL`-configured `httpx.Client` call to another team's route) — not assume a same-process
function call is available. Routing dispatch through the existing route this way still preserves the
guardrail contract unchanged: a `requires-live-integration` manifest still 409s over that HTTP call and
the typed step still fails per the policy above, exactly as a direct Agent Console invoke does today.

Rejected: calling `invoke_entrypoint` directly, as an earlier revision of this ADR proposed. It
reproduces exactly the guardrail bypass this section exists to prevent. Also rejected (a mistake in a
later revision of this ADR, now corrected): describing the invoke-route call as "in-process" — the two
services do not share a process, so this would have left the typed branch with no way to actually reach
the route as specified.

### 3. Schema fidelity (the runtime-binding-caveat prerequisite)

The entrypoint half of the gate above — restricting typed execution to manifests with a custom
`source.entrypoint` — is how this ADR resolves schema fidelity **without** first fixing the
runtime-binding caveat (the `typed_io` opt-in half is a separate, backward-compatibility concern — see
Decision). A
custom-entrypoint manifest's advertised schema is exactly what its entrypoint code was written
against, so validation can happen entirely at the DAG/dispatch boundary (§2) with **no change** to
`shared_agent_invoke/dispatch.py`'s `invoke_entrypoint(entrypoint, body)` calling convention.

This converts the caveat from a blocking prerequisite into a scoped-out revisit trigger: **if/when
Studio-authored (shared-entrypoint) agents need typed DAG execution, the binding-caveat fix — giving
those agents' persisted schema real invoke-time binding — must land first, as its own follow-up
ticket/ADR, and this ADR's exclusion gate must be revisited to admit them.** That fix is out of scope
here; it benefits Agent Console / general catalog invocation too, not just the DAG, and has independent
design questions (dispatch-signature change vs. per-agent entrypoint synthesis) that don't belong in a
DAG-execution-focused decision.

Rejected: treating the binding-caveat fix as a blocking prerequisite of any DAG typed-IO work. That
needlessly widens this ADR's scope and delays a decidable, self-contained win (typed execution for
hand-authored specialist agents) behind an unrelated, larger invoke-path redesign.

### 4. Adapter coupling (ADR-007)

**No change to `AgenticTeamAdapter` or its `WAIT`-answer contract.** A typed step's failure becomes
`status="failed"` + `error: str`, exactly like today's blanket exception path (already mapped by the
adapter's terminal-status handling); a typed step's success still produces a `prev_output: str` per §1.
The adapter never observes step-level typing — only the unchanged run-level surface (`TestPipelineRun`,
`PipelineRunStatus`, the three provisioning routes).

Rejected: extending the founder Protocol/adapter so a persona can supply a structured `WAIT` answer to
a typed step. This would re-open the Protocol-generalization question ADR-007 explicitly closed
("revisit only if a second single-phase target team appears"), and it is moot given §5 — `WAIT` stays
free-text-only, so there is never a structured `WAIT` answer to deliver.

### 5. WAIT steps stay free-text-only

No typed-`WAIT` variant is introduced. `StepType.WAIT` and `_handle_wait_step` are unchanged. A typed
registry agent that needs human input still receives the `WAIT`'s raw `human_input: str`, bound and
validated through the same input-field-binding path (§1/§2) as any other free-text producer feeding a
typed step; a validation failure fails the run per §2's policy rather than silently degrading.

Rejected: a typed `WAIT` variant. The entire `WAIT` stack is string-shaped end-to-end today —
`TestPipelineRun.human_prompt: Optional[str]`, the `human_input` DB column, `SubmitPipelineInputRequest.
input: str`, and ADR-007's one-free-text-question adapter mapping. A typed `WAIT` would touch the store,
the `/input` DTO, the human-input UI, and ADR-007's adapter contract simultaneously, for a case already
fully covered by the general input-field-binding mechanism in §1/§2.

## Contract boundary

A future implementation must satisfy exactly this surface. A drift-tripwire test (in the style of
ADR-007's `test_adapter_agentic_team_contract_drift.py`) should assert these shapes:

- `AgenticTeamAgent.source` / `manifest_id` (`agentic_team_provisioning/models.py`) — **unchanged**;
  `manifest_id` remains the sole join key from a roster row to its `AgentManifest`.
- `AgentManifest.inputs` / `outputs: IOSchema | None`, `AgentManifest.source.entrypoint: str`
  (`agent_registry/models.py`) — the runner's gate compares `source.entrypoint` against the shared
  generated-agent entrypoint constant; both `inputs` and `outputs` must be present and string-bindable
  per Decision condition 3, and are resolved and validated against (inbound before dispatch, outbound
  — after envelope unwrapping — before extraction), per §1/§2.
- The shared generated-agent entrypoint, currently a private `_GEN_ENTRYPOINT` in
  `agent_studio/registration.py`, **must be exported** from a shared, importable location (e.g.
  `agent_registry/models.py` or a small new module). Two producers currently define this same literal
  string independently and must both be migrated to the shared constant: `agent_studio/registration.py`
  (`_GEN_ENTRYPOINT`) and `agentic_team_provisioning/manifest_generation.py` (`_ENTRYPOINT`, used when
  stamping every generated team manifest's `source.entrypoint`) — leaving either on its own literal
  risks the two drifting apart, which would silently make a generated agent's manifest pass the typed
  branch's entrypoint check.
- `agent_registry/schema_resolver.py:resolve_schema` — the only sanctioned way to turn
  `IOSchema.schema_ref` into JSON Schema for validation; `IOSchema.inline_schema` (already validated at
  model-construction time via `Draft202012Validator.check_schema`) is used verbatim when present, per
  existing precedence rules.
- `POST /api/agents/{agent_id}/invoke` (`backend/unified_api/routes/agents.py`, mounted on the Unified
  API process) — the sanctioned dispatch surface the new typed branch calls **over a real HTTP
  request** (`agentic_team_provisioning` is a separately-proxied team, not `in_process`, so this is a
  network call using the same base-URL/client pattern as the existing
  `planning_team/adapters/market_research.py` cross-team call — not a same-process function call); never
  `shared_agent_invoke/dispatch.py:invoke_entrypoint` directly (see "Preserve the registry invoke
  boundary" in §2). Three response classes, handled distinctly: `200` (success envelope —
  `{"output": ..., "duration_ms": ..., "trace_id": ..., "logs_tail": ..., ...}`; unwrap `output` before
  validating against the resolved `outputs` schema), `202` (sandbox warming, `Retry-After` header — not
  terminal, poll until `200` or an error arrives, never treated as success), and 404/409
  (`requires-live-integration`)/503/502 (terminal failure). Any terminal-failure response or an
  output-schema validation failure is the typed-step failure signal, routed to `try_fail_pipeline_run`.
- New `ProcessStep.input_field: Optional[str]`, `output_field: Optional[str]`,
  `typed_io: bool = False` (`agentic_team_provisioning/models.py`) — the only new persisted step
  fields; all default to values (`None`/`False`) under which every existing `ProcessStep` document
  deserializes to, and keeps running as, the unchanged persona path.
- `PipelineRunStatus`, `TestPipelineRun.status` / `current_step_id` / `human_prompt` / `error`
  (`agentic_team_provisioning/models.py`) — **unchanged**; this is exactly the surface ADR-007's
  existing drift tripwire already protects, and this ADR asserts no member/field here changes.
- `PipelineStepResult` gets one new optional field,
  `agent_kind: Literal["persona", "registry_typed"] = "persona"`, so a run's audit trail can
  distinguish which execution mode a step ran under without changing `status`/`output`'s existing
  meaning.
- `_roster_agent_from_manifest` (`api/main.py`) and `_run_agent` (`pipeline_runner.py`) currently carry
  docstring markers pointing at ADR-008 and asserting no `source == "registry"` branch exists. A future
  implementation must update those markers to point at this ADR and describe the exclusion gate, rather
  than a blanket "never."

## Consequences

- **The spike is closed, not further deferred.** Each of ADR-008's four questions has a decisive
  answer; the one piece that could not be resolved outright (the runtime-binding caveat) is converted
  into an explicit, falsifiable scope exclusion with its own stated revisit trigger, rather than left
  open-ended.
- **A real typed-IO execution path becomes buildable**, but only for hand-authored specialist agents
  with a custom `source.entrypoint`, a schema that is present and string-bindable (Decision condition
  3), and only for `ProcessStep`s that explicitly opt in via `typed_io=True`. LLM-generated and
  Studio-authored agents are explicitly excluded until the runtime-binding caveat has its own follow-up
  fix; manifests with no schema or a non-string-bindable schema (e.g. `blogging.fact_checker`,
  `job_matching.scanner`) are excluded outright, with no coercion path planned. These are known,
  accepted limitations of this decision, not oversights.
- **No existing persisted process changes behavior.** Because typed execution requires an explicit
  per-step opt-in (`typed_io=True`) plus a design-time schema-compatibility check (condition 3), no
  `ProcessStep` written before this ADR's implementation lands can be silently reinterpreted from
  persona to typed execution — adopting typed execution for an existing roster entry is a deliberate,
  validated migration, not an automatic side effect.
- **Registry invoke guardrails are preserved, over a real network call.** Dispatch goes through the
  existing `POST /api/agents/{agent_id}/invoke` route as a genuine HTTP call — `agentic_team_provisioning`
  and the Unified API are separate proxied processes, not one in-process app — so a manifest tagged
  `requires-live-integration` (e.g. `job_matching.scanner`, `blogging.publication`) still 409s instead of
  running unguarded inside the DAG runner's process, and a cold-sandbox `202` is polled rather than
  mistaken for a completed step.
- **The DAG's data model gains three small, additive fields** (`ProcessStep.input_field`/`output_field`/
  `typed_io`) and one small, additive result field (`PipelineStepResult.agent_kind`) — no breaking
  change to any persisted document or existing contract.
- **ADR-007's adapter contract is confirmed unaffected** — no follow-up work is required there beyond
  an optional test case exercising a typed-step failure round-tripping through the adapter's terminal-
  status mapping, to make the "no adapter change" claim falsifiable.
- **This ADR does not itself implement anything.** A future implementation PR must still: add the three
  `ProcessStep` fields, export the shared entrypoint constant (updating both `agent_studio/registration.py`
  and `agentic_team_provisioning/manifest_generation.py`), extend `roster_validation.py` with the
  condition-3 compatibility check, add the new runner branch (a real HTTP call to the existing invoke
  route, handling its three response classes and unwrapping its envelope, validating both `inputs` and
  `outputs`), and update the two ADR-008 docstring markers accordingly.
