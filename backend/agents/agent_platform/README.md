# Agent platform

In-process backend for discovering, authoring, running, and sandboxing specialist
agents. Lives at `backend/agents/agent_platform/` so it sits on the same
`PYTHONPATH` root (`backend/agents`) as every other agent package.

## Members

| Subpackage | Role | Public import |
|---|---|---|
| [`registry/`](registry/README.md) | Manifest catalog serving `/api/agents` | `from agent_platform.registry import …` |
| `console/` | Runs / saved-inputs / diff data layer | not yet relocated |
| [`sandbox/`](sandbox/README.md) | Per-agent ephemeral sandbox lifecycle and sandbox-only Temporal wiring | `from agent_platform.sandbox import acquire` |
| [`studio/`](studio/README.md) | Conversational single-agent authoring + `/api/agent-studio` router | `from agent_platform.studio import router` |

This package's `__init__.py` is a thin boundary docstring only — it re-exports
nothing. Callers import from the subpackage façades.

## Not in this package

- Docker/environment provisioning infra (`agent_team_studio/agent_provisioning_team/`)
- Domain apps that consume the platform (`agentic_team_provisioning/`, `user_agent_founder/`)
- Unified-API HTTP route modules for registry/console/sandbox (`unified_api/routes/sandboxes.py` stays
  a bare `include_router` mount). Studio's router lives in `agent_platform.studio`.

Layout and import map: `system_design/adr/ADR-013-agent-platform-package-layout.md`.
Migration order and non-goals: `system_design/adr/ADR-014-agent-platform-non-goals-and-migration-order.md`.
