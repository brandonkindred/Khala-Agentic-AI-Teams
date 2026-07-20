# shared_agent_invoke

FastAPI shim mounted inside the `khala-agent-sandbox` image. Exposes the
single specialist agent loaded by the sandbox runtime to the Agent Console
**Runner** over `POST /_agents/{agent_id}/invoke`.

The shim does **not** run inside production team services; it lives only
inside the sandbox container started by
`backend/agents/agent_provisioning_team/sandbox/`. The sandbox's
`agent_sandbox_runtime/entrypoint.py` mounts it and adds a per-container
middleware that restricts dispatch to the one agent id bound via
`SANDBOX_AGENT_ID`.

## Mount

```python
from shared_agent_invoke import mount_invoke_shim

app = FastAPI(...)
mount_invoke_shim(app)
```

The underscore prefix on the path signals "internal sandbox route" — not
part of any team's public API. The unified API proxies public traffic
through `POST /api/agents/{id}/invoke` before it reaches this endpoint.

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
- **404** when the agent is unknown to the registry.
- **409** when the agent carries the `requires-live-integration` tag.
- **422** with the envelope in `detail` when the agent's entrypoint raised a
  user-space exception. `logs_tail` captures the last 50 records.
- **500** with the envelope in `detail` when the dispatcher can't load the
  entrypoint (missing symbol, bad factory, etc.) — infra failures must not
  look like a successful invocation to the proxy's run-persistence layer.

## Dispatch rules

The shim imports `manifest.source.entrypoint` (`module.path:Symbol`) and
handles three shapes:

1. **Plain function / coroutine** — called with the JSON body directly.
2. **Class** — instantiated with no args; the shim looks for `run`,
   `invoke`, `execute`, or `__call__` in that order.
3. **Factory function** (name starts with `make_`) — called with no args to
   get the agent, then dispatched as in (2).

Constructor args, missing symbols, malformed entrypoints, and missing methods
all raise `AgentNotRunnableError`, which the shim turns into an HTTP-500
envelope with the error text.

## Tests

```bash
cd backend
python3 -m pytest agents/shared_agent_invoke/tests/ --asyncio-mode=auto
```

## Adding a new agent to the Runner

1. Add a manifest under `backend/agents/<team>/agent_console/manifests/` with
   a valid `source.entrypoint` (and optional `sandbox.env` / `sandbox.extra_pip`).
2. Generate a golden sample with
   `python3 -m agent_registry.scripts.generate_sample_skeletons`.

No per-team wiring is needed — the sandbox image is team-agnostic and the
unified API resolves the agent via its manifest at invoke time.
