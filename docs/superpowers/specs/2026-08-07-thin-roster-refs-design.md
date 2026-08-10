# Design: Thin roster refs for AgenticTeamAgent

Date: 2026-08-07

## Goal

Reduce persisted `AgenticTeamAgent` to a thin roster reference
(`agent_name`, `source`, `manifest_id`) so team composition no longer stores a
second full agent identity. Persona for Stage-3 pipeline, test-chat, recommend,
and validation comes from join-at-read against registry `AgentManifest`.

## Context

Three parallel agent shapes exist today: Studio `AgentDefinition`, fat roster
`AgenticTeamAgent`, and catalog `AgentManifest`. The Identity epic locks
`AgentManifest` as the writable source of truth and roster entries as thin
refs with display/slot-only local naming (`agent_name` may differ from
`manifest.name`).

Today pipeline and test-chat build free-text personas from fat roster fields
(`role`, `skills`, `capabilities`, `tools`, `expertise`). Generated rows often
persist `manifest_id=None` even after `register_team_manifests` writes a
Manifest. `UpdateAgentRequest` mutates fat fields and re-registers generated
Manifests from the roster — a second write SoT.

Runtime binding of Manifest `states[].system_prompt` at invoke remains a
separate epic. This story only joins Manifest summary/tags/tools into the
existing free-text `build_agent` path (ADR-008 behavior preserved).

## Decisions

| Topic | Choice |
|---|---|
| Persistence | Thin only: `agent_name`, `source`, `manifest_id` (required after normalize) |
| Overrides | Display/slot only — `agent_name` is the local slot key; no persona fields on roster |
| Runtime persona | Join-at-read Manifest → `RosterPersonaView` → existing `build_agent` |
| PUT fat persona | HTTP 400 with clear message; no proxy-to-Manifest in this story |
| GET/list | Thin ref + enriched persona view for UI chips (read-only) |
| Legacy rows | Eager migrate on read/write: ensure Manifest, stamp `manifest_id`, persist thin |
| LLM save | Fat LLM blob → register/update Manifest → store thin ref with `manifest_id` |
| From-registry | Store thin ref only (stop denormalizing persona onto the row) |
| Temporal | Persist/serialize thin refs; resolve persona inside activities at run time |
| Frontend | Minimal: thin TS model + read-only enrichment; disable fat PUT; no UI cutover |
| Prompt binding | Out of scope (`states[]` unused by agentic free-text runner) |

## Target shapes

### Persisted `AgenticTeamAgent`

| Field | Meaning |
|---|---|
| `agent_name` | Team-unique local slot key (may differ from `manifest.name`) |
| `source` | `"registry"` \| `"generated"` |
| `manifest_id` | Required join to SoT after normalize/migrate |

### Read `RosterPersonaView` (not persisted)

| View field | Manifest source |
|---|---|
| `role` | `summary` |
| `skills` | `tags` |
| `tools` | `cognition.tools` (else `[]`) |
| `expertise` | `[manifest.team]` |

Same mapping as today’s `_roster_agent_from_manifest` projection, but computed
at read time and never written back to `agentic_team_agents`.

## Module plan

New module: `agent_team_studio/agentic_team_provisioning/roster_resolve.py`

Responsibilities:

1. **Eager migrate** — accept legacy `data_json` with extra fat keys; for
   `source=generated` without `manifest_id`, build/register or look up Manifest
   via existing `manifest_agent_id` / `build_agent_manifest`, stamp id, persist
   thin shape. For `source=registry` without `manifest_id`, fail closed
   (invariant violation).
2. **`resolve_persona(ref) → RosterPersonaView`** — `get_registry().get(manifest_id)`;
   missing Manifest is an explicit error (no silent empty persona for runs).
3. **Enrichment helpers** — thin ref + persona view for API list/GET responses.

Call sites to rewire:

- `runtime/pipeline_runner.py` — resolve then `build_agent`
- Test-chat session / send paths — resolve for starter prompts and agent build
- `roster_validation.py` — coverage against resolved persona / Manifest fields
- `api/main.py` — from-registry thin write; LLM save thin write; recommend via
  resolve; fat PUT → 400
- Temporal activities — resolve at execution time from thin refs

## API behavior

| Endpoint | Behavior |
|---|---|
| `GET .../agents` | Thin refs; response may include enriched persona view |
| `POST .../agents/from-registry` | Thin ref only (`source=registry`, `manifest_id=id`) |
| LLM chat save agents | Manifest upsert from LLM fat block; thin rows with `manifest_id`; preserve registry rows by source/id |
| `PUT .../agents/{name}` fat body | **400** — roster is not the persona write path |
| `DELETE .../agents/{name}` | Unchanged (unregister generated Manifest when applicable) |

## Legacy migration algorithm

On list/get/save touch of a roster row:

1. Parse JSON tolerating unknown/fat keys.
2. If already thin with `manifest_id` → use as-is.
3. If `source=generated` and `manifest_id` missing → ensure Manifest exists
   (register from legacy fat fields or look up generated id), set `manifest_id`.
4. Strip fat keys; persist `{agent_name, source, manifest_id}`.
5. If `source=registry` and `manifest_id` missing → raise (do not invent).

## Frontend (minimal)

- Update `agentic-team.model.ts`: thin `AgenticTeamAgent`; optional enriched
  persona type for list payloads.
- Process-designer inline edit: stop issuing fat PUT; show read-only chips from
  enrichment.
- No Stage-1 drafts work, no product cutover.

## Testing

Must cover:

- Legacy fat JSON migrates to thin + stamped `manifest_id`
- From-registry stores thin ref only
- Mixed roster: registry preserved on LLM save; generated rows carry `manifest_id`
- Fat PUT returns 400
- Pipeline / test-chat / recommend / validation use resolver (registry mocked)
- Tests that asserted `manifest_id is None` for generated rows are updated

Primary suites under
`backend/agents/agent_team_studio/agentic_team_provisioning/tests/`:
`test_registry_roster.py`, `test_roster_validation.py`,
`test_manifest_generation.py`, `test_pipeline_runner.py`,
`test_runtime_cognition.py`, temporal/dispatch and manifests endpoint tests.

## Out of scope

- Studio `AgentDefinition` alignment (sibling Identity story)
- Manifest builder unification module
- Drafts API / Stage-1 authoring UI / full UI cutover
- Runtime binding of Manifest `states[].system_prompt` at invoke
- Typed registry DAG invoke (ADR-009 follow-on)
- Proxying roster PUT into Manifest field edits

## Non-goals / explicit non-changes

- `investment_team.agent_catalog.AgentDefinition` remains a separate domain model
- Sandbox `invoke_generated_agent` continues to take caller-supplied persona body
  (unbound); not fixed here
- ADR-008 free-text runner path stays; this story only changes where persona
  fields are loaded from
