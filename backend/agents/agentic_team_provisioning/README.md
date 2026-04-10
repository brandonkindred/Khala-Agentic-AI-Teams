# Agentic Team Provisioning

Conversational service for designing **agentic teams**: named **rosters** of AI agents, **process** definitions (triggers, steps, outputs), and integration with the **Agent Provisioning** team for per-step sandbox environments.

## API

- **Unified API prefix:** `/api/agentic-team-provisioning`
- **Health:** `GET /health`

## Architecture

See [AGENTIC_TEAM_ARCHITECTURE.md](AGENTIC_TEAM_ARCHITECTURE.md) for the required structure (API layer, orchestrator, agents pool, processes pool, infrastructure).

## Roster and staffing validation

Each team has a **roster** (`AgenticTeamAgent`): `agent_name`, `role`, `skills`, `capabilities`, `tools`, `expertise`. The process designer LLM emits roster JSON alongside process JSON.

- **`GET /teams/{team_id}/agents`** — roster
- **`GET /teams/{team_id}/roster/validation`** — `RosterValidationResult` (gaps: unrostered agents, unused roster entries, unstaffed steps, incomplete profiles)

Validation logic lives in `roster_validation.py`.

## Agent Provisioning bridge

When enabled (`AGENTIC_TEAM_AGENT_PROVISIONING_ENABLED`), saving a process can schedule background provisioning via `agent_provisioning_team` for step agents. See `agent_env_provisioning.py`.

## UI

The Angular app (**Agentic Teams**) shows chat, **Team Roster** (live refresh after messages), process diagram, and staffing gaps. Routes under `/agentic-teams`.

## Khala platform

This package is part of the [Khala](../../../README.md) monorepo (Unified API, Angular UI, and full team index).
