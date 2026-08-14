# Backend Agents

This directory contains the Khala agent-team implementations, the in-process
agent platform, and their APIs.

## Platform vs infra vs domain apps

| Kind | Where | Role |
|---|---|---|
| **Platform** | `agent_platform/` | In-process registry, console, sandbox, Studio. Import as `agent_platform.<subpackage>`. |
| **Cognition** | `agent_cognition/` | Memory, rules, and tools substrate used at the invoke boundary. |
| **Shared infra** | `backend/shared/` (sibling of this tree) | Postgres, Temporal, agent invoke — import as `shared.<name>`. |
| **Provisioning infra** | `agent_team_studio/agent_provisioning_team/` | Docker/environment stand-up. Not a member of `agent_platform`. |
| **Domain apps** | `agent_team_studio/agentic_team_provisioning/`, `agent_team_studio/user_agent_founder/` | Consume the platform; keep their own APIs, workers, and schemas. |

Do not recreate top-level `agent_registry/`, `agent_console/`, `agent_studio/`,
`agent_provisioning_team/`, `agentic_team_provisioning/`, or `user_agent_founder/`
packages. Those locations are gone.

## Directory structure

```text
backend/agents/
├── accessibility_audit_team/
├── agent_cognition/                   # Invoke-boundary memory / rules / tools
├── agent_git_tools/                   # Shared Git tooling for agents
├── agent_llm_tools_service/           # LLM tools discovery service
├── agent_platform/                    # Registry, console, sandbox, Studio
├── agent_repair_team/                 # Agent crash recovery
├── agent_repo_tools/
├── agent_team_studio/                 # Provisioning infra + domain apps
│   ├── agent_provisioning_team/
│   ├── agentic_team_provisioning/
│   └── user_agent_founder/
├── ai_systems_team/
├── analytics/                         # Analytics utilities
├── blogging/
├── branding_team/
├── continuation_logs/                 # Continuation log storage
├── deepthought/                       # Recursive self-organising agent
├── docker/                            # Agents-only Docker assets
├── investment_team/
├── llm_service/                       # Centralized LLM client (Ollama, dummy)
├── market_research_team/
├── personal_assistant_team/
├── planning_team/
├── product_delivery/
├── road_trip_planning_team/
├── sales_team/
├── soc2_compliance_team/
├── social_media_marketing_team/
├── software_engineering_team/
├── startup_advisor/                   # Persistent conversational startup advisor
├── team_assistant/                    # Team assistant utilities
├── team_contract/                     # Team contract definitions
├── user_profile/
├── job_service_client.py              # HTTP client for centralized job service
├── Dockerfile
└── requirements.txt
```

## Running via Unified API (recommended)

From `backend/`:

```bash
python run_unified_api.py
```

This mounts all enabled team APIs behind one server on port `8080` by default.
The authoritative count is `TEAM_CONFIGS` in `backend/unified_api/config.py`.

## Running individual team APIs

Most team APIs can be run with `uvicorn` from `backend/agents/`.

Examples:

```bash
# Software Engineering
python -m uvicorn software_engineering_team.api.main:app --host 0.0.0.0 --port 8000

# Blogging
PYTHONPATH=blogging python -m uvicorn blogging.api.main:app --host 0.0.0.0 --port 8001

# Social Media Marketing
python -m uvicorn social_media_marketing_team.api.main:app --host 0.0.0.0 --port 8010
```

For team-specific setup and env vars, use each team's README.

## Team READMEs

- `software_engineering_team/README.md`
- `planning_team/README.md`
- `blogging/README.md`
- `personal_assistant_team/README.md`
- `social_media_marketing_team/README.md`
- `market_research_team/README.md`
- `soc2_compliance_team/README.md`
- `branding_team/README.md`
- `agent_team_studio/agent_provisioning_team/README.md`
- `accessibility_audit_team/README.md`
- `ai_systems_team/README.md`
- `investment_team/README.md`
- `road_trip_planning_team/README.md`
- `sales_team/README.md`
- `startup_advisor/README.md`
- `agent_team_studio/agentic_team_provisioning/README.md`
- `agent_team_studio/user_agent_founder/README.md`
- `deepthought/README.md`
- `llm_service/README.md`
- `agent_platform/README.md`
- `agent_cognition/README.md`

## Platform shared packages

Cross-team infra (Postgres, Temporal, agent invoke, observability, …) lives outside this
tree at `backend/shared/`, imported as `shared.<name>` (e.g. `from shared.postgres import
TeamSchema`). See [`backend/shared/postgres/README.md`](../shared/postgres/README.md) and
[`backend/shared/temporal/README.md`](../shared/temporal/README.md).

## Khala platform

This package is part of the [Khala](../../README.md) monorepo (Unified API, Angular UI, and full team index).
