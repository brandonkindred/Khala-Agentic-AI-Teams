# Agentic Team Provisioning

Conversational service for designing **agentic teams**: named **rosters** of AI agents, **process** definitions (triggers, steps, outputs), and integration with the **Agent Provisioning** team for per-step sandbox environments.

## API

- **Unified API prefix:** `/api/agentic-team-provisioning`
- **Health:** `GET /health`

## Architecture

See [AGENTIC_TEAM_ARCHITECTURE.md](AGENTIC_TEAM_ARCHITECTURE.md) for the required structure (API layer, orchestrator, agents pool, processes pool, infrastructure).

## Roster identity: thin refs, Manifest SoT

Each team has a **roster** of thin refs (`AgenticTeamAgent`: `agent_name`, `source`,
`manifest_id`). The registered `AgentManifest` (see `agent_registry.models.AgentManifest`)
is the sole writable source of truth for an agent's persona — a roster row only
references it. `agent_name` is a team-local slot key (may differ from `manifest.name`);
it is not a persona override.

Persona fields (`role`, `skills`, `capabilities`, `tools`, `expertise`) are never
persisted on the roster row. They're joined at read time via `roster_resolve.resolve_persona`
into an `EnrichedRosterAgent`, and consumed the same way by `roster_validation.py`
(staffing/depth checks), `runtime/agent_builder.py` (system-prompt construction), and
`runtime/pipeline_runner.py` (execution). Roster `PUT` rejects any body that supplies a
persona field with `400` — edit the linked `AgentManifest` instead.

The process designer LLM emits roster JSON alongside process JSON; generated agents are
stamped with `manifest_id` and registered via `register_team_manifests`. Older roster
rows written before this model was thinned may still carry the legacy fat JSON shape —
`roster_resolve.migrate_roster_row` eagerly coerces those to thin refs (stamping a
generated `manifest_id` when needed) the first time they're read.

- **`GET /teams/{team_id}/agents`** — roster
- **`GET /teams/{team_id}/roster/validation`** — `RosterValidationResult` (gaps: unrostered agents, unused roster entries, unstaffed steps, incomplete profiles)

Validation logic lives in `roster_validation.py`.

Field mapping for the from-registry projection (`api/services/teams.py::_roster_agent_from_manifest`)
and for folding a legacy fat row's persona into the target manifest during migration:

| Legacy roster field | Manifest (SoT) | Notes |
|---|---|---|
| `agent_name` | `name` | |
| `role` | `summary` | falls back to `name` when `summary` is empty |
| `skills` | `tags` | |
| `tools` | `cognition.tools` | empty when the manifest has no `cognition` block |
| `expertise` | `[team]` | single-element list: the manifest's home team |
| `capabilities` | *(none)* | never populated from a manifest — open gap |

## Pipeline test runs (execution)

Agent Studio's **pipeline test run** (`POST /teams/{team_id}/test-pipeline/runs`) walks a
process DAG, running each step's agent and pausing at WAIT steps for human input
(`POST .../input`), cancellable via `POST .../cancel`. There are two runtime modes:

- **Thread mode** (default, `TEMPORAL_ADDRESS` unset): the `PipelineRunner` runs the DAG
  in a daemon thread; WAIT steps poll the Postgres run store, and an advisory-locked
  reaper fails orphaned runs whose heartbeat went stale.
- **Temporal mode** (`TEMPORAL_ADDRESS` set): the run dispatches to a durable
  `AgenticPipelineWorkflow` (`temporal/`), each step runs as an activity reusing the same
  `PipelineRunner` handlers, WAIT steps pause on a `submit_input` **signal** +
  `workflow.wait_condition`, and the run survives a worker/process restart. Such runs are
  marked `temporal_owned` and skipped by the DB reaper (Temporal owns their recovery).

Both modes write the same run-store rows, so the status/list endpoints and UI polling
are identical. See `docs/ENV_VARS.md` for the WAIT-timeout/poll/stale knobs.

## Agent Provisioning bridge

When enabled (`AGENTIC_TEAM_AGENT_PROVISIONING_ENABLED`), saving a process can schedule background provisioning via `agent_provisioning_team` for step agents. See `agent_env_provisioning.py`.

## UI

The Angular app (**Agentic Teams**) shows chat, **Team Roster** (live refresh after messages), process diagram, and staffing gaps. Routes under `/agentic-teams`.

## Khala platform

This package is part of the [Khala](../../../README.md) monorepo (Unified API, Angular UI, and full team index).
