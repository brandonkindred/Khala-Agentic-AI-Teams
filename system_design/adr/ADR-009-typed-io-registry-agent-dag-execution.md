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

Registry agents whose manifest advertises a **custom `source.entrypoint`** (not the shared
generated-agent entrypoint) get real typed-IO DAG execution. Registry agents that still resolve to the
shared generated-agent entrypoint — which includes *all* LLM-generated agents and, today, *all*
Studio-authored agents regardless of whether they carry an authored schema — continue to execute
through the unchanged free-text persona path. This is the single gate a future implementation checks
before choosing between the existing persona branch and a new typed branch.

### 1. Boundary marshalling

`ProcessStep` gains two new optional fields, `input_field: Optional[str]` and
`output_field: Optional[str]` (both default `None`, so every existing step document deserializes
unchanged). They name which top-level property of the target registry agent's resolved input/output
schema the DAG's one free-text channel binds to.

- **Inbound:** the runner builds `{step.input_field or "input": prev_output}` and validates/coerces it
  against the manifest's resolved input schema before dispatch.
- **Outbound:** after a successful invoke, if the output object has a string property named
  `step.output_field or "output"`, that string becomes the next `prev_output`; otherwise the whole
  output object is JSON-serialized and *that* string becomes `prev_output`. A `str` is always produced,
  so any downstream consumer — a `WAIT` step's display, a persona step's prompt, the pipeline's final
  result — degrades gracefully to reading structured output as text.
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
  `jsonschema.Draft202012Validator` → on success, dispatch via
  `shared_agent_invoke/dispatch.py:invoke_entrypoint` in-process.

**Failure mode: fail the step.** A validation failure or entrypoint exception routes to the existing
`try_fail_pipeline_run` CAS path — the same mechanism `_execute`'s blanket exception handler already
uses today.

- Rejected "fall back to persona mode" — silently unenforced typing defeats the purpose of doing typed
  execution at all.
- Rejected "surface as a `WAIT` question" — would make a `StepType.ACTION` step conditionally behave
  like `StepType.WAIT`, breaking the DAG author's declared step-type invariant that ADR-007's adapter
  relies on to detect `WAIT` steps.

### 3. Schema fidelity (the runtime-binding-caveat prerequisite)

The exclusion gate above — restricting typed execution to manifests with a custom `source.entrypoint`
— is how this ADR resolves schema fidelity **without** first fixing the runtime-binding caveat. A
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
registry agent that needs human input still receives the `WAIT`'s raw `human_input: str`, coerced
through the same input-field-binding/validation path (§1/§2) as any other free-text producer feeding a
typed step; a coercion failure fails the run per §2's policy rather than silently degrading.

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
  generated-agent entrypoint constant.
- The shared generated-agent entrypoint, currently a private `_GEN_ENTRYPOINT` in
  `agent_studio/registration.py`, **must be exported** from a shared, importable location (e.g.
  `agent_registry/models.py` or a small new module) so `agent_studio` and the pipeline runner reference
  one source of truth instead of a duplicated magic string.
- `agent_registry/schema_resolver.py:resolve_schema` — the only sanctioned way to turn
  `IOSchema.schema_ref` into JSON Schema for validation; `IOSchema.inline_schema` (already validated at
  model-construction time via `Draft202012Validator.check_schema`) is used verbatim when present, per
  existing precedence rules.
- `shared_agent_invoke/dispatch.py:invoke_entrypoint(entrypoint: str, body: Any) -> Any` — the
  sanctioned in-process dispatch primitive the new typed branch calls directly; `AgentNotRunnableError`
  (or a validation failure) is the typed-step failure signal, routed to `try_fail_pipeline_run`.
- New `ProcessStep.input_field: Optional[str]`, `output_field: Optional[str]`
  (`agentic_team_provisioning/models.py`) — the only new persisted step fields; both default `None`, so
  every existing `ProcessStep` document deserializes unchanged.
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
  with a custom `source.entrypoint`. LLM-generated and Studio-authored agents are explicitly excluded
  until the runtime-binding caveat has its own follow-up fix — this is a known, accepted limitation of
  this decision, not an oversight.
- **The DAG's data model gains two small, additive fields** (`ProcessStep.input_field`/`output_field`)
  and one small, additive result field (`PipelineStepResult.agent_kind`) — no breaking change to any
  persisted document or existing contract.
- **ADR-007's adapter contract is confirmed unaffected** — no follow-up work is required there beyond
  an optional test case exercising a typed-step failure round-tripping through the adapter's terminal-
  status mapping, to make the "no adapter change" claim falsifiable.
- **This ADR does not itself implement anything.** A future implementation PR must still: add the two
  `ProcessStep` fields, export the shared entrypoint constant, add the new runner branch and validation
  path, and update the two ADR-008 docstring markers accordingly.
