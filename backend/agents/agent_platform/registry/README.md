# Agent Registry

Discovery substrate for the **Agent Studio** (UI at `/agent-studio`).

Loads declarative per-agent manifests from
`backend/agents/<team>/agent_console/manifests/*.yaml` and serves them as
structured metadata at **`/api/agents`**. Disk discovery is the source of truth
for built-in agents; there is no Temporal or LLM dependency in this package.

## Static vs dynamic manifests

| Kind | Source | Persistence |
|---|---|---|
| **Static / disk** | YAML under each team's `agent_console/manifests/` | Loaded into process memory at startup; never written to Postgres |
| **Dynamic** | Runtime registration (e.g. generated roster agents via `AgentRegistry.register` / `replace_dynamic_manifests`) | Optional Postgres overlay (`dynamic_store.py` / `agent_platform.registry.postgres`) when `POSTGRES_HOST` is set |

When Postgres is unset or the store is unavailable, dynamic registration stays
**local-only** (same process). Store read failures degrade to the local view for
catalog/get (except fail-closed callers that set `require_store` /
`require_persist`). The HTTP catalog routes remain read-only; writers are
in-process registry APIs used by teams that generate agents.

Agent Studio's save/register path is one such writer: it projects its editable
`AgentDefinition` view-model into an `AgentManifest` before calling
`AgentRegistry.register`. Construction of generated and Studio manifests goes
through [`shared.manifests`](../../../../shared/manifests/README.md), not
ad-hoc `AgentManifest(...)`. See
[`studio/README.md`](../studio/README.md#identity-agentdefinition-view-model-vs-agentmanifest-sot)
for the full `AgentDefinition` ↔ `AgentManifest` field mapping.

A registered manifest's `role`/`skills`/`capabilities`/`expertise` and
`system_prompt` are the *default* persona for that agent's invoke, not the whole
story: the shared generated-agent runtime (`invoke_generated_agent`) binds an
omitted request field from the manifest but lets an explicitly-present request
field override it for that invoke only. See
[`studio/README.md`](../studio/README.md#identity-agentdefinition-view-model-vs-agentmanifest-sot),
[`agentic_team_provisioning/README.md`](../../agent_team_studio/agentic_team_provisioning/README.md#roster-identity-thin-refs-manifest-sot),
and
[`system_design/adr/ADR-015-invoke-generated-agent-persona-state-precedence.md`](../../../../system_design/adr/ADR-015-invoke-generated-agent-persona-state-precedence.md)
for the locked precedence contract.

## Why it exists

Historically, Khala had a flat, team-level roster in
[`unified_api/config.py`](../../../unified_api/config.py) and no structured
metadata for individual specialist agents. `AGENT_ANATOMY.md` prescribed a
contract in prose but nothing queried it. Agent Studio needed a way to
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
sandbox:                         # consumed by the per-agent sandbox lifecycle
  manifest_path: default.yaml
  access_tier: standard          # minimal | standard | elevated | full
  env:                           # optional: extra env vars forwarded into the container
    FEATURE_FLAG_X: "true"
  extra_pip:                     # optional: extra pip packages baked into the image
    - pandas==2.2.*
cognition:                       # consumed by the Agent Cognition Core (later phases)
  memory:
    retention_days_events: 90    # raw episodic events pruned after N days (>= 1)
  tools: [git, http_api]         # tool ids resolved against the cognition tool registries
  rule_packs: [default_guardrails]  # seed rule packs installed on first provision
  requires_idempotency_key: false   # true => side-effecting; reject invokes lacking a caller key
  knowledge_graph:                  # Neo4j + Graphiti knowledge base (attached by default)
    enabled: true                   # set false to opt this agent out of the graph entirely
    ingest_events: true             # ingest raw episodic events into the graph
    ingest_summaries: true          # ingest rollup summaries into the graph
    ground_rule_proposals: true     # feed graph context into rule reflection (HITL preserved)
source:
  entrypoint: blogging.blog_planning_agent.agent:BlogPlanningAgent
  anatomy_ref: backend/agents/blogging/blog_planning_agent/ANATOMY.md
```

A fully-annotated standalone example lives at
[`agent_cognition/examples/cognition_manifest.example.yaml`](../agent_cognition/examples/cognition_manifest.example.yaml).

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
| `cognition` | no | Per-agent cognition config (memory retention, tools, rule packs, idempotency requirement, `knowledge_graph`). Consumed by the Agent Cognition Core; UI shows an indicator when present. |
| `cognition.knowledge_graph` | no | Neo4j + Graphiti knowledge base for this agent. **Attached by default** (`enabled: true`) whenever a `cognition` block is present — every sub-field is defaulted, so an omitted `knowledge_graph` still validates default-on. Set `enabled: false` to opt the agent out; the graph sync worker and reflection grounding then skip it. |

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

## API surface (catalog HTTP is read-only)

| Route | Purpose |
|---|---|
| `GET /api/agents` | List `AgentSummary[]`. Query params: `team`, `tag`, `q` (full-text). |
| `GET /api/agents/teams` | List `TeamGroup[]` for the catalog sidebar filter. |
| `GET /api/agents/{agent_id}` | Return `AgentDetail` (manifest + anatomy markdown if `anatomy_ref` resolves on disk). |
| `GET /api/agents/{agent_id}/schema/input` | Resolve input `schema_ref` to JSON Schema. 404 if missing or unimportable. |
| `GET /api/agents/{agent_id}/schema/output` | Same, for output. |

The router lives at [`backend/unified_api/routes/agents.py`](../../../unified_api/routes/agents.py)
and is wired in `unified_api/main.py` alongside `llm_tools`, `llm_usage`, etc.

## Reloading

The registry is a process-wide `lru_cache` singleton. To force a reload without
restarting the server:

```python
from agent_platform.registry import get_registry
get_registry.cache_clear()
```

## Tests

```bash
cd backend
python3 -m pytest agents/agent_platform/registry/tests/ unified_api/tests/test_agents_route.py -v
```

## Roadmap

1. **Phase 1 — Catalog** *(shipped)*: registry + API + browsable UI.
2. **Phase 2 — Runner + Sandboxes** *(shipped)*: `POST /api/agents/{id}/invoke`, per-agent ephemeral Docker sandboxes (`agent_platform.sandbox`, unified `khala-agent-sandbox` image), invoke shim (`shared.agent_invoke`), auto-generated golden samples. See [`sandbox/README.md`](../sandbox/README.md) and [`shared.agent_invoke/README.md`](../../../shared/agent_invoke/README.md).
3. **Phase 3 — Runs**: Postgres-backed run history, user-saved ad-hoc inputs, run diffing, JSON-schema-driven form UI.
4. **Phase 4 — Breadth**: manifest coverage for all 24 teams, pre-warming, batch invocation.

## Authoring golden samples (Phase 2+)

Location: `backend/agents/<team_dir>/agent_console/samples/<agent_id>/*.json`
(team_dir is the manifest's on-disk parent, e.g. `branding_team/`, not the
`manifest.team` key).

Auto-generate a minimal skeleton for every manifest with `inputs.schema_ref`:

```bash
cd backend
PYTHONPATH="agents:." python3 -m agent_platform.registry.scripts.generate_sample_skeletons
```

The script never clobbers hand-edited samples. Edit the emitted
`default.json` to make it realistic — the Runner UI's "Sample" dropdown
surfaces every `.json` file in the directory.
