# Agent Provisioning Team

A swarm of agents that provisions sandboxed Docker environments with configurable tool accounts for AI agents, following an employee-onboarding model with least-privilege access and comprehensive onboarding documentation.

## Standard agent anatomy (required)

The Agent Provisioning team **must** design and deliver AI agents that match the canonical structure in **[`AGENT_ANATOMY.md`](AGENT_ANATOMY.md)**. That document is the normative checklist (Input/Output, Agent core, Tools, tiered Memory, Prompt roles, Security Guardrails, and Subagents with recursive INPUT/OUTPUT). Reference diagrams live in [`design_assets/`](design_assets/).

## Overview

The Agent Provisioning Team automates the process of setting up development environments for AI agents. Like onboarding a new employee at a company, it provisions:

- **Sandboxed Docker containers** - Isolated execution environments
- **Tool accounts** - PostgreSQL databases, Redis caches, Git repos
- **Secure credentials** - Auto-generated passwords and tokens
- **Access controls** - Least-privilege permissions per tool
- **Onboarding documentation** - Getting-started guides and environment info

## Architecture

HTTP provisioning and deprovisioning run as durable Temporal workflows
(`AgentProvisioningWorkflow` / `AgentDeprovisioningWorkflow`). Each workflow
drives the same **6 sequential phases**:

```
1. SETUP              → Create Docker container
2. CREDENTIAL_GEN     → Generate passwords/tokens
3. ACCOUNT_PROVISION  → Create accounts in tools
4. ACCESS_AUDIT       → Verify least-privilege
5. DOCUMENTATION      → Generate onboarding docs
6. DELIVER            → Finalize and return results
```

Progress is tracked via the job store (updated by Temporal activities) and exposed through REST API endpoints. `orchestrator.py` remains for unit tests and non-HTTP callers; the REST surface does not invoke it for provision/deprovision.

## Durable execution (Temporal)

Provisioning, resume, restart, and deprovision **require** Temporal
(`TEMPORAL_ADDRESS` set and worker reachable). The API returns **503** when
Temporal is unavailable — there is no in-process fallback.

Workflows/activities live in `temporal/`. Provisioning/deprovision are
registered in the `WORKFLOWS` / `ACTIVITIES` lists in `temporal/__init__.py`,
served by the worker started explicitly via
`start_agent_provisioning_temporal_worker_thread` (task queue
`agent-provisioning`) from the `agent-provisioning-service` team_service
entrypoint (`TEAM_TEMPORAL_WORKER_MODULE` / `TEAM_TEMPORAL_WORKER_FUNC`), with
the API lifespan as a standalone-dev backstop (`uvicorn ...:app`).
Importing the package does not start a worker.
**Sandbox workflows/activities are not part of this team.** They live in
`agent_platform.sandbox.temporal` (`SANDBOX_WORKFLOWS` / `SANDBOX_ACTIVITIES`)
and run on `SANDBOX_TASK_QUEUE`, served only by a worker started from
`unified_api/main.py`'s lifespan. This team's worker serves provisioning /
deprovision only. See `agent_platform/sandbox/README.md`.

### Coverage — every team operation → its workflow/activity

| Operation | Entry point | Workflow | Activities |
|---|---|---|---|
| Provision | `POST /provision` | `AgentProvisioningWorkflow` | `setup` / `credentials` / per-tool `provision_tool` (parallel) / `audit` / `documentation` / `deliver` / `compensate` |
| Resume / restart | `POST /provision/job/{id}/resume`·`/restart` | `AgentProvisioningWorkflow` (`skip_phases` + `prior_results`) | same |
| Deprovision | `DELETE /environments/{agent_id}` | `AgentDeprovisioningWorkflow` | `deprovision_activity` |
| Sandbox warm | `POST /api/agents/sandboxes/{id}/warm`, invoke proxy | `SandboxAcquireWorkflow` | `sandbox_acquire_activity` |
| Sandbox teardown | `DELETE /api/agents/sandboxes/{id}` | `SandboxTeardownWorkflow` | `sandbox_teardown_activity` |
| Sandbox idle reaper | started once at API boot | `SandboxReaperWorkflow` (self-scheduling, fixed id, `continue_as_new`) | `sandbox_reap_activity` |

Deprovision uses `execute_workflow_sync` (sync `DELETE /environments` handler).
Platform sandbox dispatch (`agent_platform.sandbox.temporal.dispatch`) branches on `is_temporal_enabled()` and uses
`execute_workflow_async` (`async def` sandbox routes) so the API event loop is
never blocked.

### Sandbox lifecycle invariants

The sandbox pool (`sandbox/`) is a process-wide singleton with in-memory state.
Moving its mutators onto Temporal upholds two invariants (see `sandbox/README.md`
and the docstrings in `sandbox/lifecycle.py`):

- **Loop affinity** — every `asyncio.Lock` taker (`acquire`, `teardown`, and
  `reap_once` via `teardown`) runs on exactly one loop: the Temporal worker loop
  when enabled, the API loop when not. Never half-migrate.
- **Thread safety** — all `_state` reads/writes and every persist are serialized
  by a `threading.Lock`, because mutators now run on the worker thread while
  read-only ops (`status`/`list`/`metrics`/`note_activity`) stay on the API loop.

**Durability caveat:** sandbox `_state` is in-memory per process, so a sandbox
activity retried on a *different* worker replica sees empty state. Single-process
deployments (the norm here) are unaffected; the concentrated Temporal win is the
durable, single-instance idle reaper plus per-activity retries/observability.

## Runbook: verifying the lock rollout has drained

`AgentProvisioningWorkflow`/`AgentDeprovisioningWorkflow` gate their per-`agent_id`
ownership lock (`shared/agent_lock.py`) behind `workflow.patched(...)` markers in
`temporal/workflows.py`, so a workflow history recorded before the lock existed
keeps replaying its original, lock-free command sequence for its entire remaining
lifetime. During rollout, a new request for the same `agent_id` could otherwise
race that still-open pre-patch execution, since neither ever writes a lock record
for the other to see. `POST /provision`/`DELETE /environments/{agent_id}` guard
against this automatically (`AGENT_PROVISIONING_DRAIN_GATE_ENABLED`, default on),
but use this procedure when the gate is disabled or you want to confirm the drain
yourself — during initial rollout verification, for example.

**Exact Temporal visibility query** (mirrors the filter
`shared/visibility_query.py`'s `find_open_pre_patch_executions` builds):

```bash
temporal workflow list --query "WorkflowType IN ('AgentProvisioningWorkflow', 'AgentDeprovisioningWorkflow') AND ExecutionStatus = 'Running' AND StartTime < '<cutoff-utc>'"
```

`<cutoff-utc>` is the `AGENT_PROVISIONING_LOCK_PATCH_CUTOFF_AT` value (the
lock-patch release's deploy time) rendered as `YYYY-MM-DDTHH:MM:SSZ`.

Equivalently, from a shell with the team's environment loaded, run the same
detection helper the automated gate calls (this also resolves each hit's
`agent_id`):

```bash
python -c "from agent_provisioning_team.shared.visibility_query import find_open_pre_patch_executions; print(find_open_pre_patch_executions())"
```

**Drain complete** once either form returns zero results — no open execution of
either workflow type predates the cutoff. Only then is it safe to disable the gate
long-term, and only once no history predates the patch at all can the
`workflow.patched(...)` markers themselves be deprecated and removed (see the
`TODO` beside `_PROVISIONING_LOCK_PATCH`/`_DEPROVISIONING_LOCK_PATCH` in
`temporal/workflows.py`). Full env var semantics:
[`docs/ENV_VARS.md`](../../../docs/ENV_VARS.md#agent-provisioning).

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/provision` | Start provisioning job |
| GET | `/provision/status/{job_id}` | Get job status with phase progress |
| GET | `/provision/jobs` | List all provisioning jobs |
| GET | `/environments` | List all provisioned agents |
| GET | `/environments/{agent_id}` | Get agent environment status |
| DELETE | `/environments/{agent_id}` | Deprovision an agent |

### Start Provisioning

```bash
curl -X POST http://localhost:8006/provision \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "agent-001",
    "manifest_path": "default.yaml",
    "access_tier": "standard"
  }'
```

Response:
```json
{
  "job_id": "uuid...",
  "status": "running",
  "message": "Provisioning started. Poll GET /provision/status/{job_id} for progress."
}
```

### Check Status

```bash
curl http://localhost:8006/provision/status/{job_id}
```

Response:
```json
{
  "job_id": "uuid...",
  "status": "running",
  "agent_id": "agent-001",
  "current_phase": "account_provisioning",
  "current_tool": "postgresql",
  "progress": 45,
  "tools_completed": 1,
  "tools_total": 3,
  "completed_phases": ["setup", "credential_generation"]
}
```

## Access Tiers

| Tier | Description |
|------|-------------|
| `minimal` | Read-only access to tools |
| `standard` | Read/write access (default) |
| `elevated` | Administrative access to own resources |
| `full` | Full administrative access (audited) |

## Tool Manifests

Manifests define which tools to provision. Located in `manifests/`:

- `default.yaml` - Full dev environment (PostgreSQL, Redis, Git)
- `minimal.yaml` - Lightweight (Git only)
- `full.yaml` - Complete environment with all features

### Manifest Format

```yaml
version: "1.0"
base_image: "python:3.11-slim"

environment:
  PYTHONUNBUFFERED: "1"

tools:
  - name: postgresql
    provisioner: postgres_provisioner
    access_level: read_write
    config:
      database_prefix: "agent_"
    onboarding:
      description: "PostgreSQL database"
      env_var: "POSTGRES_URL"
      getting_started: "Connect using: psql $POSTGRES_URL"
```

## Tool Provisioners

| Provisioner | Tool | Capabilities |
|-------------|------|--------------|
| `docker_provisioner` | Docker | Container lifecycle |
| `postgres_provisioner` | PostgreSQL | Database + user creation |
| `redis_provisioner` | Redis | ACL with key prefix |
| `git_provisioner` | Git | SSH keys + repo init |
| `generic_provisioner` | Custom | Template for extensions |

## Directory Structure

```
agent_provisioning_team/
├── models.py              # Domain models
├── orchestrator.py        # In-process phase runner (tests / non-HTTP callers)
├── temporal/              # AgentProvisioningWorkflow + activities (HTTP path)
├── phases/                # Phase implementations
├── tool_agents/           # Tool provisioners
├── shared/                # Stores and utilities
├── api/                   # FastAPI endpoints (Temporal-only provision/deprovision)
└── manifests/             # Tool manifest examples
```

## Running Locally

```bash
# Install dependencies
pip install -r requirements.txt

# Start the API server
uvicorn agent_provisioning_team.api.main:app --host 0.0.0.0 --port 8006
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `PROVISION_CREDENTIAL_KEY` | Fernet encryption key | Auto-generated |
| `POSTGRES_HOST` | PostgreSQL host | `localhost` |
| `POSTGRES_PORT` | PostgreSQL port | `5432` |
| `POSTGRES_USER` | PostgreSQL admin user | `postgres` |
| `POSTGRES_PASSWORD` | PostgreSQL admin password | - |
| `REDIS_HOST` | Redis host | `localhost` |
| `REDIS_PORT` | Redis port | `6379` |
| `REDIS_PASSWORD` | Redis admin password | - |

## Khala platform

This package is part of the [Khala](../../../README.md) monorepo (Unified API, Angular UI, and full team index).
