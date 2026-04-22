# agent_provisioning_team.sandbox

Per-agent ephemeral sandbox lifecycle used by the Agent Console **Runner**.

Drives the unified `khala-agent-sandbox` image (`backend/agent_sandbox_image/`,
`backend/agent_sandbox_runtime/`). Each invocation of a specialist agent gets
its own hardened container, loaded with exactly one agent via
`SANDBOX_AGENT_ID`, torn down after it goes idle. Replaces the old per-team
compose-based lifecycle at `backend/agents/agent_sandbox/`, which was removed
in Phase 5 of the sandbox re-architecture (issue #267).

Used by `backend/unified_api/routes/sandboxes.py` (`/api/agents/sandboxes/*`)
and the invoke proxy in `routes/agents.py` (`POST /api/agents/{id}/invoke`).

## Modules

| File | Role |
|---|---|
| `lifecycle.py` | Per-process `Lifecycle` class keyed by `agent_id`: `acquire`, `status`, `teardown`, `list_active`, `note_activity`, idle reaper. |
| `provisioner.py` | `docker run` / `docker inspect` / `docker rm -f` wrapper. Assembles the hardened argv (cap-drop, read-only, no-new-privileges, seccomp, loopback-bound ports, resource caps). Creates the `khala-sandbox` bridge network on demand. |
| `state.py` | Pydantic models (`SandboxState`, `SandboxHandle`, `SandboxStatus`), atomic JSON checkpoint, env-var helpers. |

## State machine

```mermaid
stateDiagram-v2
    [*] --> COLD
    COLD --> WARMING: acquire
    WARMING --> WARM: health OK
    WARMING --> ERROR: run / health fail
    WARM --> COLD: teardown / idle reap
    ERROR --> COLD: teardown
```

Transitions are serialised by a per-agent `asyncio.Lock`. State is
checkpointed after every transition and reconciled with `docker inspect` on
the next request so an API restart doesn't orphan containers.

## SandboxSpec (manifest side)

Each agent's YAML manifest may declare a `sandbox:` block consumed by the
provisioner. Fields live on `agent_registry.models.SandboxSpec`:

| Field | Purpose |
|---|---|
| `env` | Extra env vars to forward into the sandbox container (beyond the default Postgres/LLM set). |
| `extra_pip` | Additional pip packages to install at image build time (Phase 1 image bake). |

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_PROVISIONING_SANDBOX_IMAGE` | `khala-agent-sandbox:latest` | Image tag for the unified single-agent sandbox (Phase 1). |
| `AGENT_PROVISIONING_SANDBOX_NETWORK` | `khala-sandbox` | Docker bridge network. Created on demand; safe to leave at the default. |
| `AGENT_PROVISIONING_SANDBOX_STATE_FILE` | `$AGENT_CACHE/agent_provisioning/sandboxes/state.json` | Where to checkpoint state across restarts. |
| `AGENT_PROVISIONING_SANDBOX_IDLE_MINUTES` | `5` | Idle threshold before the reaper tears a sandbox down. |
| `AGENT_PROVISIONING_SANDBOX_BOOT_TIMEOUT_S` | `90` | How long to wait for `/health` to succeed after boot. |

## Local smoke test

```bash
cd backend && make run
# in another shell (blogging.writer is just an example agent id):
curl -X POST localhost:8080/api/agents/sandboxes/blogging.writer | jq
# poll until status -> warm
curl localhost:8080/api/agents/sandboxes/blogging.writer | jq
curl -X POST localhost:8080/api/agents/blogging.writer/invoke \
     -H 'Content-Type: application/json' \
     -d @agents/blogging/agent_console/samples/blogging.writer/default.json | jq
curl localhost:8080/api/agents/sandboxes | jq
curl -X DELETE localhost:8080/api/agents/sandboxes/blogging.writer
```

## Tests

```bash
cd backend
python3 -m pytest agents/agent_provisioning_team/tests/test_sandbox_lifecycle.py --asyncio-mode=auto
```

Tests patch `provisioner.run_container`, `inspect_host_port`, `is_running`,
and `stop_container` so the suite runs offline.

## Design notes

- **One container per agent.** Each specialist gets its own sandbox — process
  state can't leak between agents. Idle reaper keeps the resident set small.
- **Hardened by default.** The provisioner argv enforces `--cap-drop=ALL`,
  `--read-only`, `--security-opt=no-new-privileges:true`, seccomp, pid/file
  ulimits, 1 CPU / 1 GiB RAM, and binds host ports to `127.0.0.1` (addresses
  issue #255).
- **No shared Postgres.** The host's Postgres creds are forwarded through so
  every sandbox points at the same development DB. Per-sandbox secret
  isolation is issue #257.
- **No auto-start.** The unified API only warms a sandbox on the first
  `acquire`; cold-start cost is paid by the first invocation for each agent.
- **Restart safety.** State is reconciled with `docker inspect` on the next
  request, so an API crash doesn't orphan containers or leak tracked state.
