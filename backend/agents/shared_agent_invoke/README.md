# shared_agent_invoke

One-line-mount FastAPI shim that exposes specialist agents to the Agent
Console **Runner** (Phase 2).

## Mount in your team's `api/main.py`

```python
from shared_agent_invoke import mount_invoke_shim

app = FastAPI(...)
mount_invoke_shim(app, team_key="blogging")
```

That adds `POST /_agents/{agent_id}/invoke` to the team service. The Runner's
unified API proxy calls this endpoint through a warm sandbox container.

The underscore prefix on the path signals "internal sandbox route" — not
part of the team's public API. The unified_api proxy strips public traffic
before reaching it.

## Response envelope

```json
{
  "output": <arbitrary JSON>,
  "duration_ms": 1234,
  "trace_id": "uuid",
  "logs_tail": ["[INFO] ...", "[stdout] ..."],
  "error": null
}
```

- **200** on successful invocation (even if `output` is empty).
- **422** with the envelope in `detail` when the agent's entrypoint raised a
  user-space exception. `logs_tail` captures the last 50 records.
- **404** when the agent is unknown to this service's team.
- **409** when the agent carries the `requires-live-integration` tag.

## Dispatch rules

The shim imports `manifest.source.entrypoint` (`module.path:Symbol`) and
handles three shapes:

1. **Plain function / coroutine** — called with the JSON body directly.
2. **Class** — instantiated with no args; the shim looks for `run`,
   `invoke`, `execute`, or `__call__` in that order.
3. **Factory function** (name starts with `make_`) — called with no args to
   get the agent, then dispatched as in (2).

Constructor args, missing symbols, malformed entrypoints, and missing methods
all raise `AgentNotRunnableError`, which the shim turns into an HTTP-422
envelope with the error text.

## Tests

```bash
cd backend
python3 -m pytest agents/shared_agent_invoke/tests/ --asyncio-mode=auto
```

## Adding new team support

1. Ensure the team's agents have manifests under
   `backend/agents/<team>/agent_console/manifests/`.
2. Add the one-line `mount_invoke_shim(app, team_key=...)` call to the
   team's `api/main.py`.
3. Add a sandbox service entry to `docker/sandbox.compose.yml` and register
   it in `backend/agents/agent_sandbox/config.py`.

Phase 2 wires four teams: `blogging`, `software_engineering`, `planning_v3`,
`branding`. Others can be added incrementally — the catalog shows all 29
agents today; invocation becomes possible per team as the shim lands.
