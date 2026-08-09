# Agentic Team Provisioning

Conversational service for designing **agentic teams**: named **rosters** of AI agents, **process** definitions (triggers, steps, outputs), and integration with the **Agent Provisioning** team for per-step sandbox environments.

## API

- **Unified API prefix:** `/api/agentic-team-provisioning`
- **Health:** `GET /health`

## Architecture

See [AGENTIC_TEAM_ARCHITECTURE.md](AGENTIC_TEAM_ARCHITECTURE.md) for the required structure (API layer, orchestrator, agents pool, processes pool, infrastructure).

## Roster and staffing validation

Each team has a **roster** of thin refs (`AgenticTeamAgent`: `agent_name`, `source`, `manifest_id`). Persona fields (`role`, `skills`, `capabilities`, `tools`, `expertise`) live on the linked `AgentManifest` and are joined at read time via `roster_resolve.resolve_persona`. The process designer LLM emits roster JSON alongside process JSON; generated agents are stamped with `manifest_id` and registered via `register_team_manifests`.

- **`GET /teams/{team_id}/agents`** — roster
- **`GET /teams/{team_id}/roster/validation`** — `RosterValidationResult` (gaps: unrostered agents, unused roster entries, unstaffed steps, incomplete profiles)

Validation logic lives in `roster_validation.py`.

## Roster identity: thin refs vs. Manifest SoT

`AgenticTeamAgent` (`models.py`) is the thin, sole persisted/API roster shape —
only `agent_name`, `source`, `manifest_id`. The registered `AgentManifest` (see
`agent_registry.models.AgentManifest`) is the sole writable source of truth for
an agent's persona; nothing writes `role`/`skills`/`capabilities`/`tools`/
`expertise` onto a roster row. `agent_name` is a team-local slot key (may differ
from `manifest.name`); it is not a persona override.

Persona fields are resolved live from the linked manifest at read time via
`roster_resolve.persona_from_manifest`/`resolve_persona`, and joined into the
API response shape `EnrichedRosterAgent` (`models.py`) by
`api/main.py::enrich_roster_agent` — never persisted. `roster_validation.py`,
`runtime/agent_builder.py`, and `runtime/pipeline_runner.py` all consume the
resolved persona, not stored fields. Legacy rows persisted before this
migration (still carrying fat JSON in `assistant/store.py`) self-heal on first
read via `roster_resolve.migrate_roster_row`, which folds any fat fields into
the linked (or newly generated) `AgentManifest` and rewrites the row thin.
`PUT /teams/{team_id}/agents/{agent_name}` rejects any persona field in the
request body with a 400 (`api/services/teams.py::update_roster_agent`) — the
API boundary enforces that Manifest is the only writable persona identity.

Field mapping, as implemented by the from-registry projection
(`api/services/teams.py::_roster_agent_from_manifest`) and the read-time join
(`roster_resolve.persona_from_manifest`):

| `EnrichedRosterAgent` field (read view) | Manifest (SoT) | Notes |
|---|---|---|
| `agent_name` | `name` | thin ref only; not resolved from the manifest at read time |
| `role` | `summary` | falls back to `name` when `summary` is empty |
| `skills` | `tags` | marker tags (`"generated"`, team key) stripped |
| `tools` | `cognition.tools` | empty when the manifest has no `cognition` block |
| `expertise` | `[team]` | single-element list: the manifest's home team |
| `capabilities` | *(none)* | never populated from a manifest — no Manifest field carries this concept |

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
