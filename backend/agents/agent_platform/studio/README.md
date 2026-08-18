# Agent Studio

Conversational Stage-1 build flow for authoring a single agent: an LLM-assisted
conversation co-authors an `AgentDefinition` draft, which can be saved (registered
into the catalog) or, when refining an existing agent, seeded by cloning a
registered agent back into an editable draft.

## API

- **Unified API prefix:** `/api/agent-studio`
- Conversations: `POST /conversations`, `POST /conversations/{id}/messages`
- Clone: `POST /agents/from-registry/{agent_id}`
- Save: `POST /agents`
- Drafts (separate, opaque-payload CRUD; not `AgentDefinition`-typed): `/drafts`

The router is included from `unified_api/main.py` at import time when
`TEAM_CONFIGS["agent_studio"]` is enabled — catalog:
[`docs/UNIFIED_API_LIFESPAN.md`](../../../../docs/UNIFIED_API_LIFESPAN.md).

### Dispatch (direct, in-process)

**Direct dispatch is the supported path for authoring CRUD** (start conversation /
send message / clone / save): it runs entirely in-process via
`agent_platform.studio.temporal.dispatch`, with no Temporal workflow and no
dependency on a Temporal cluster or the `agent-studio-queue` worker. Each route
handler's call runs on a small fixed pool of daemon worker threads (bounded by
`AUTHORING_TIMEOUT_S`, matching the former Temporal activity timeout) that call the
process-wide `AgentStudioService` singleton (`runtime.get_studio_service()`)
directly. These are request/response operations, not durable or long-running work,
so they do **not** start Temporal workflows — the former 1-activity authoring
workflows are gone. The service's native `ValueError`/`LookupError` propagate
unchanged; the routes map them to 400/404. `shutdown_authoring_executor()` is part
of unified-API lifespan teardown.

The `temporal/` package name and `agent-studio-queue` task queue constant are kept for
now (empty `WORKFLOWS`/`ACTIVITIES`, a no-op worker starter) rather than removed
outright — see `docs/ENV_VARS.md`'s `UNIFIED_API_AGENT_STUDIO_TEMPORAL_WORKER` entry.

This is not Temporal being optional for the platform. Temporal remains a required
dependency for the durable, long-running subsystems (provisioning, sandbox lifecycle,
pipeline runs, persona founder); only Studio authoring CRUD — which never needed
durability — runs outside it.

## Identity: `AgentDefinition` view-model vs. `AgentManifest` SoT

`agent_platform.registry.models.AgentManifest` is the only persisted, writable **catalog**
identity for an authored agent (dynamic Postgres overlay when `POSTGRES_HOST` is
set, in-process otherwise — see `agent_platform.registry/README.md`). `AgentDefinition`
(`models.py`) is an **ephemeral authoring view-model**: it lives only as the
in-progress `definition_json` on an Agent Studio conversation row
(`agent_studio_conversations`), never as a second catalog identity. Saving a
conversation's definition, or cloning a registered agent into a new refine
conversation, always crosses this boundary through exactly one pair of pure
projection functions in `registration.py` (they delegate construction and
field projection to [`shared.manifests`](../../../../shared/manifests/README.md);
Studio-owned id, state-fold, and refine-draft rules stay in this package):

- **`build_studio_agent_manifest(definition) -> AgentManifest`** — the save/register
  path (`service.py::save_agent`). Called on every `POST /agents`.
- **`clone_from_manifest(manifest) -> AgentDefinition`** — the clone path
  (`service.py::start_conversation` in `refine` mode, and
  `service.py::clone_from_registry`).

No other code constructs a persisted agent identity from an `AgentDefinition`, and
nothing reads `AgentDefinition` back out of the registry — the registry only ever
sees the `AgentManifest` these functions produce.

### Field mapping

| `AgentDefinition` (view) | `AgentManifest` (SoT) | Notes |
|---|---|---|
| `name` | `name` (+ derives `id` via `studio_agent_id()`) | re-saving the same name updates the same manifest id |
| `role` | `summary` | falls back to `"Studio agent {name}"` when blank |
| `description` | `description` | |
| `tags` | `tags` | unioned with `"studio"` on save; plumbing tags (`studio`, `generated`, `agentic_team_provisioning`) stripped on clone |
| `tools` | `cognition.tools` | |
| `system_prompt` | `states[key="executing"].system_prompt` | the top-level field is the assistant's quick-edit channel; on save a non-blank value overrides the `executing` state's prompt, a blank value leaves it untouched; on clone it is restored from the `executing` state |
| `states[]` (`planning`/`executing`/`researching`) | `states[]` | full 3-key round-trip, matched by `key`; a manifest missing or carrying unsupported keys is backfilled to the canonical 3 on clone |
| `input_schema` / `output_schema` | `inputs.inline_schema` / `outputs.inline_schema` | an omitted (`None`) schema falls back to the shared generated-agent entrypoint's generic `schema_ref` instead |
| `mode`, `cloned_from` | *(not persisted)* | authoring provenance only — describes how the draft came to be, irrelevant to the saved manifest |
| *(none)* | `id`, `team`, `schema_version`, `invoke`, `sandbox`, `source` | registry/runtime plumbing; always stamped by `build_studio_agent_manifest`, never editable via Studio |

**Runtime binding (shared with generated agents):** the generated-agent runtime
binds a saved agent's persisted persona from the stored manifest when the invoke
request omits those fields — its `role` (manifest `summary`) and the `system_prompt`
folded into the `executing` state (see `build_studio_agent_manifest`) bind at invoke
time. An explicitly-present request field (a raw-body presence test, not
truthiness — so an explicitly-cleared empty list/blank string is an override too)
wins over the manifest default for that invoke only, never written back; a
request-supplied `system_prompt` fully replaces the base prompt instead of composing
with the persona fields. This is the same entrypoint and the same precedence rule a
generated team agent's invoke uses — see
[`agentic_team_provisioning/README.md`](../../agent_team_studio/agentic_team_provisioning/README.md#roster-identity-thin-refs-manifest-sot)
for the sibling write-up and
[`system_design/adr/ADR-015-invoke-generated-agent-persona-state-precedence.md`](../../../../system_design/adr/ADR-015-invoke-generated-agent-persona-state-precedence.md)
for the locked precedence contract.
