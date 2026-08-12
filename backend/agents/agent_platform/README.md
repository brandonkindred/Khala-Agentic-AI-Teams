# agent_platform

In-process platform core for discover / author / run / sandbox agents.

## Members

| Subpackage | Status in this tree |
|---|---|
| `studio` | Present — authoring modules + `/api/agent-studio` router |
| `registry` | Not yet — still `agent_registry` |
| `console` | Not yet — still `agent_console` |
| `sandbox` | Not yet — still `agent_team_studio.agent_provisioning_team.sandbox` |

## Not in this package

- Docker/env provisioning under `agent_team_studio.agent_provisioning_team/`
- `agentic_team_provisioning` and `user_agent_founder` (consumers)

## Public imports

```python
from agent_platform.studio import router, get_studio_service
from agent_platform.studio import build_studio_agent_manifest, clone_from_manifest
from agent_platform.studio.temporal.worker import start_agent_studio_temporal_worker_thread
from agent_platform.studio.postgres import SCHEMA
```

The unified API mounts `router` at `/api/agent-studio` when
`TEAM_CONFIGS["agent_studio"]` is enabled. Worker boot stays in the unified-API
lifespan.
