# ADR-008 — Agent Studio v1: run registry agents as free-text LLM personas; defer typed-IO DAG execution

- **Status**: Accepted — the "Follow-up design spike" below is resolved by
  `system_design/adr/ADR-009-typed-io-registry-agent-dag-execution.md`. This ADR's v1-boundary
  decision and its historical context remain accurate and unchanged; read ADR-009 for the spike's
  resolution.
- **Date**: 2026-07-04
- **Owner**: Agentic Team Provisioning / Agent Studio
- **Related**:
  - `docs/design/agent-studio-ux-spec.md` — §5 (registry → roster bridge; "nice-to-have" real
    registry-agent invocation) and §6 (Risks: "Typed-IO registry agents in a free-text DAG").
  - `system_design/adr/ADR-007-founder-agentic-team-adapter-collapse.md` — owns the founder→pipeline
    adapter contract that a typed-IO execution path would extend.
  - `system_design/adr/ADR-009-typed-io-registry-agent-dag-execution.md` — resolves the follow-up
    design spike deferred below.

## Context

Agent Studio lets a roster **mix** two kinds of agent:

- **Registry agents** — real, catalogued `AgentManifest`s
  (`backend/agents/agent_platform/registry/models.py`) that declare **typed** input/output schemas. The types are
  carried as lazy dotted pointers (`IOSchema.schema_ref`, resolved via
  `agent_platform/registry/schema_resolver.py`), not inline schemas.
- **Generated agents** — thin roster refs (`AgenticTeamAgent`: `agent_name`, `source`,
  `manifest_id`) whose persona is stored on an in-process `AgentManifest` registered via
  `register_team_manifests` (LLM ``role`` → manifest ``summary``).

The runtime that executes a team is a **single linear DAG**, not a typed dataflow. The pipeline runner
(`backend/agents/agent_team_studio/agentic_team_provisioning/runtime/pipeline_runner.py`) walks a `ProcessDefinition` in
topological order and threads a **plain `str`** (`prev_output`) from one step to the next; `WAIT` steps
pause for one **free-text** human/persona answer. Nothing in the DAG is schema-aware.

These two contracts do not naturally reconcile: a typed registry agent expects structured input and
produces structured output, but the DAG has only free text to give it and only free text to carry
forward. The spec (§6) names this the **deepest unknown** of the redesign.

The registry → roster **bridge** already ships (spec §5 item 3): `AgenticTeamAgent` carries
`source: "generated" | "registry"` + `manifest_id`, and the `from-registry` / `PUT` / `DELETE` roster
endpoints exist (`backend/agents/agent_team_studio/agentic_team_provisioning/api/main.py`,
`tests/test_registry_roster.py`). But the bridge only lets a registry agent **sit on** a roster — it does
not make the DAG honor its typed IO. Two current-code sites make that concrete:

- **Persona is join-at-read, not stored on roster.** `_roster_agent_from_manifest`
  (`agentic_team_provisioning/api/main.py`) persists only a thin ref (`agent_name`,
  `source`, `manifest_id`). Persona for API responses and validation comes from
  `resolve_persona` / `persona_from_manifest` (`roster_resolve.py`), which maps
  `manifest.tags → skills`, `manifest.cognition.tools → tools`, `manifest.summary → role`,
  etc. Typed `manifest.inputs` / `manifest.outputs` are still not marshalled through the DAG.
- **The runner resolves persona at invoke time.** `_run_agent` (`pipeline_runner.py`) calls
  `resolve_persona(agent_def.manifest_id)` for every roster agent and builds a free-text LLM
  persona — there is **no `source == "registry"` branch** and no schema marshalling. A
  registry-sourced entry executes identically to a generated agent on that path.

So the impedance mismatch is not just a design risk; a lossy free-text projection is the *current*
behavior. The open question is whether — and how — a later phase should make the DAG execute a registry
agent through its declared typed contract.

Two framings were weighed for v1:

- **Ship the free-text persona path** and scope typed-IO execution out of v1.
- **Design and build typed-IO DAG execution now**, so registry agents run through their real schemas from
  the first release.

## Decision

**Scope Agent Studio v1 to Phase-1 LLM-persona execution. Typed-IO registry-agent DAG execution is out
of scope for v1.**

Concretely, for v1:

- A roster **may contain** registry agents (via the shipped bridge), but at run time a registry-sourced
  entry executes as a **free-text LLM persona** built from its linked manifest via `resolve_persona`
  — the same path a generated agent takes.
- The manifest's typed `inputs` / `outputs` (`schema_ref`s) are **advertised in the catalog but not
  marshalled through the DAG**. No step validates, coerces, or type-checks the free text it receives or
  emits against a manifest schema.
- This is **deliberate**, not a gap or a bug: the free-text projection and the schema-less runner are the
  sanctioned v1 behavior, and no `source == "registry"` execution branch should be added without first
  resolving the follow-up spike below.

The two boundary sites above carry a short docstring marker pointing at this ADR, so a contributor who
reaches the place a typed-IO branch would go sees the boundary before writing one.

### Follow-up design spike (resolved — see ADR-009)

Before any later phase attempts real typed-IO registry-agent invocation, a design spike must resolve the
contract. It is deferred, not dropped. Questions it must answer:

> **Resolved by `system_design/adr/ADR-009-typed-io-registry-agent-dag-execution.md`.** Summary: typed
> DAG execution is scoped to registry agents whose manifest advertises a custom `source.entrypoint`
> (excluding LLM-generated and Studio-authored agents, which keep running the free-text persona path
> until the separate runtime-binding-caveat follow-up lands); boundary marshalling binds the DAG's one
> free-text channel to a named schema property via new `ProcessStep.input_field`/`output_field`;
> validation/coercion happens in a new runner-owned path and fails the step on error; `WAIT` stays
> free-text-only; and ADR-007's adapter contract is unaffected. The questions below are retained here
> for historical context.

- **Boundary marshalling.** How does structured input reach a registry agent when the only thing the DAG
  has is the previous step's free text? How is the agent's structured output turned back into the `str`
  the next step consumes? What happens at a free-text `WAIT` step feeding a typed agent (and vice versa)?
- **Validation & coercion.** Where do schema validation and coercion happen — at the projection, at step
  entry/exit, or inside a new runner branch — and what is the failure mode when free text cannot be
  coerced to the declared input type (fail the step, fall back to persona mode, surface to the persona as
  a WAIT question)?
- **Schema fidelity.** Today generated and Studio manifests all point `inputs` / `outputs` at the same
  shared generic envelope (`agentic_team_provisioning/models.py:GeneratedAgentInvokeInput` /
  `GeneratedAgentInvokeOutput`), and authored agents' persisted `input_schema` / `output_schema` are
  "advertised but not bound at invoke time" (see the runtime-binding caveat in
  `agent_studio/registration.py`, `manifest_generation.py`, `runtime/agent_builder.py`). Real typed IO
  requires per-agent schemas that are actually bound at invoke time — a prerequisite this spike must scope.
- **Touch-points to change.** The three sites a real implementation must revise: (1) the `from-registry`
  projection that currently discards `inputs` / `outputs`; (2) `pipeline_runner._handle_action_step`'s
  free-text `str` threading; (3) the shared generic invoke schemas + the unresolved runtime-binding caveat.
- **Adapter coupling.** How a typed path interacts with the founder collapsing adapter (ADR-007), whose
  free-text `WAIT`-answer contract assumes untyped steps.

**Revisit trigger**: before any phase attempts a `source == "registry"` execution branch in
`pipeline_runner.py` (i.e. the spec's §5 "nice-to-have" real registry-agent invocation). At that point the
spike above must exist and be resolved first; this ADR is superseded by whatever contract it produces.

## Consequences

- **v1 ships the persona path with no new runtime risk.** Because registry agents already run as LLM
  personas today, honoring this boundary requires **no behavior change** — only that the boundary is
  documented and that no premature typed-IO branch is added.
- **The hardest, least-understood path is not built first.** The explicit scope keeps the redesign's
  deepest unknown from being shipped ahead of its contract, which is the stated goal.
- **A known limitation is accepted and recorded.** A registry agent's declared typed IO is not enforced
  inside a team run in v1; users get persona-quality execution, not schema-checked dataflow. This is the
  documented v1 contract, revisited via the trigger above.
- **The spike gates the later phase.** The follow-up design task (tracked separately) must land before the
  typed-IO execution work, so the contract is designed before it is built.
