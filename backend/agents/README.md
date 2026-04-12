# Backend Agents

This directory contains the Khala agent-team implementations and their APIs.

## Directory structure

```text
backend/agents/
├── accessibility_audit_team/
├── agent_git_tools/                   # Shared Git tooling for agents
├── agent_llm_tools_service/           # LLM tools discovery service
├── agent_provisioning_team/
├── agent_repair_team/                 # Agent crash recovery
├── agentic_team_provisioning/         # Conversational team/process creation (see README.md)
├── ai_systems_team/
├── analytics/                         # Analytics utilities
├── api/                               # Legacy blog research-and-review API package
├── blogging/
├── branding_team/
├── coding_team/
├── continuation_logs/                 # Continuation log storage
├── deepthought/                       # Recursive self-organising agent
├── docker/                            # Agents-only Docker assets
├── integrations/                      # Shared integrations layer used across teams
├── investment_team/
├── llm_service/                       # Centralized LLM client (Ollama, dummy)
├── market_research_team/
├── nutrition_meal_planning_team/
├── personal_assistant_team/
├── planning_v3_team/
├── plans/                             # Planning artifacts
├── road_trip_planning_team/
├── sales_team/
├── shared_observability/              # Shared observability (Prometheus, logging)
├── shared_postgres/                   # Shared Postgres utilities
├── shared_temporal/                   # Shared Temporal workflow utilities
├── soc2_compliance_team/
├── social_media_marketing_team/
├── software_engineering_team/
├── startup_advisor/                   # Persistent conversational startup advisor
├── team_assistant/                    # Team assistant utilities
├── team_contract/                     # Team contract definitions
├── user_agent_founder/                # Autonomous startup founder agent
├── shared_job_management.py           # Shared job state helpers
├── job_service_client.py              # HTTP client for centralized job service
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## Running via Unified API (recommended)

From `backend/`:

```bash
python run_unified_api.py
```

This mounts all 20 enabled team APIs behind one server on port `8080` by default.

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
- `coding_team/README.md`
- `planning_v3_team/README.md`
- `blogging/README.md`
- `personal_assistant_team/README.md`
- `social_media_marketing_team/README.md`
- `market_research_team/README.md`
- `soc2_compliance_team/README.md`
- `branding_team/README.md`
- `agent_provisioning_team/README.md`
- `accessibility_audit_team/README.md`
- `ai_systems_team/README.md`
- `investment_team/README.md`
- `nutrition_meal_planning_team/README.md`
- `road_trip_planning_team/README.md`
- `sales_team/README.md`
- `startup_advisor/README.md`
- `agentic_team_provisioning/README.md`
- `user_agent_founder/README.md`
- `deepthought/README.md`
- `llm_service/README.md`

## Shared integrations

`integrations/` provides provider-neutral integration contracts and routing that can be reused by any team.

## Khala platform

This package is part of the [Khala](../../README.md) monorepo (Unified API, Angular UI, and full team index).
