# agent_platform

In-process platform package for discover / author / run / sandbox agents.

Today this package contains **sandbox** only. Registry, console, and studio
move here in later changes. Import with a dotted prefix:
`from agent_platform.sandbox import acquire`.

## What is in this package

| Subpackage | Role |
|---|---|
| `sandbox/` | Per-agent ephemeral sandbox lifecycle and sandbox-only Temporal wiring. |

## What is not in this package

- Docker/env provisioning (`agent_team_studio.agent_provisioning_team`)
- Agentic compose and persona runner (consumers, not members)
- Unified-API HTTP route modules (`unified_api/routes/sandboxes.py` stays
  a bare `include_router` mount)

See `sandbox/README.md` for the runner itself.
