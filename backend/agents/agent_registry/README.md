# Agent Registry

Discovery substrate for the **Agent Console** (UI at `/agent-console`).

Loads declarative per-agent manifests from
`backend/agents/<team>/agent_console/manifests/*.yaml` and serves them as
structured metadata at **`/api/agents`**. Read-only; no Postgres, no Temporal,
no LLM.

## Why it exists

Historically, Khala had a flat, team-level roster in
[`unified_api/config.py`](../../unified_api/config.py) and no structured
metadata for individual specialist agents. `AGENT_ANATOMY.md` prescribed a
contract in prose but nothing queried it. The Agent Console needed a way to
browse, describe, and (eventually) invoke every agent in the system — the
registry is that substrate.

Phase 1 (this module) ships the **Catalog**. Phases 2–4 will consume the same
manifests for isolated invocation in warm sandboxes, golden sample inputs, and
sandbox-backed test runs.

## Authoring a manifest

Place one YAML file per agent at
`backend/agents/<team>/agent_console/manifests/<agent_id>.yaml`.

Minimal manifest:

```yaml
schema_version: 1
id: blogging.planner
team: blogging
name: Blog Planner
summary: Turns a topic + research brief into a structured ContentPlan.
source:
  entrypoint: blogging.blog_planning_agent.agent:BlogPlanningAgent
```

Full manifest (all optional fields):

```yaml
schema_version: 1
id: blogging.planner
team: blogging                   # must match a TEAM_CONFIGS key (warning otherwise)
name: Blog Planner
summary: One-liner shown on catalog cards.
description: |
  Long-form markdown rendered in the drawer.
tags: [planning, content]
inputs:
  schema_ref: blogging.blog_planning_agent.models:PlanningInput
  description: Optional free-text description of the input.
outputs:
  schema_ref: blogging.shared.content_plan:ContentPlan
invoke:                          # consumed by Phase 2 (Runner)
  kind: http                     # http | function | temporal
  method: POST
  path: /api/blogging/plan
sandbox:                         # consumed by Phase 4 (sandbox reuse)
  manifest_path: default.yaml
  access_tier: standard          # minimal | standard | elevated | full
source:
  entrypoint: blogging.blog_planning_agent.agent:BlogPlanningAgent
  anatomy_ref: backend/agents/blogging/blog_planning_agent/ANATOMY.md
```

### Field rules

| Field | Required | Notes |
|---|---|---|
| `schema_version` | yes | Always `1` today. |
| `id` | yes | Globally unique dotted identifier, e.g. `team.agent_name`. |
| `team` | yes | Must match a key in `TEAM_CONFIGS` or the loader logs a warning. |
| `name`, `summary` | yes | Shown on catalog cards. |
| `source.entrypoint` | yes | `module.path:Symbol` pointing to the agent's class/factory. **Not imported** at registry load — it's metadata. |
| `inputs.schema_ref` / `outputs.schema_ref` | no | `module.path:ClassName`. Resolved **lazily** by the `/schema/input` and `/schema/output` endpoints via `TypeAdapter.json_schema()`. If the import fails (e.g. the unified_api container doesn't have team code), the endpoint returns 404 — the UI handles this gracefully. |
| `invoke`, `sandbox` | no | Metadata for later phases. UI shows indicators when present. |

### Path conventions

```
backend/agents/<team>/agent_console/
  manifests/
    <agent_id>.yaml           # one file per specialist agent
  samples/                    # Phase 3 — golden inputs
    <agent_id>/
      *.json
```

Duplicate `id`s across files are deduped (last-wins with a warning log).
Malformed YAML and manifests that fail validation are skipped with a warning.

## API surface (read-only)

| Route | Purpose |
|---|---|
| `GET /api/agents` | List `AgentSummary[]`. Query params: `team`, `tag`, `q` (full-text). |
| `GET /api/agents/teams` | List `TeamGroup[]` for the catalog sidebar filter. |
| `GET /api/agents/{agent_id}` | Return `AgentDetail` (manifest + anatomy markdown if `anatomy_ref` resolves on disk). |
| `GET /api/agents/{agent_id}/schema/input` | Resolve input `schema_ref` to JSON Schema. 404 if missing or unimportable. |
| `GET /api/agents/{agent_id}/schema/output` | Same, for output. |

The router lives at [`backend/unified_api/routes/agents.py`](../../unified_api/routes/agents.py)
and is wired in `unified_api/main.py` alongside `llm_tools`, `llm_usage`, etc.

## Reloading

The registry is a process-wide `lru_cache` singleton. To force a reload without
restarting the server:

```python
from agent_registry import get_registry
get_registry.cache_clear()
```

## Tests

```bash
cd backend
python3 -m pytest agents/agent_registry/tests/ unified_api/tests/test_agents_route.py -v
```

## Roadmap

1. **Phase 1 — Catalog** *(this release)*: registry + API + browsable UI.
2. **Phase 2 — Runner**: `POST /api/agents/{id}/invoke`, auto-generated form from `inputs.schema_ref`.
3. **Phase 3 — Samples**: golden sample inputs per agent, saved runs, diffs.
4. **Phase 4 — Sandbox reuse**: warm sandboxes via the provisioning team, reused across invocations.
