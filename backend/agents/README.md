# Backend Agents

This directory contains the Strands agent-team implementations and their APIs.

## Directory structure

```text
backend/agents/
├── api/                               # Legacy blog research-and-review API package
├── blogging/
├── software_engineering_team/
├── coding_team/
├── planning_v3_team/
├── personal_assistant_team/
├── social_media_marketing_team/
├── market_research_team/
├── soc2_compliance_team/
├── branding_team/
├── agent_provisioning_team/
├── agentic_team_provisioning/         # Conversational team/process creation
├── accessibility_audit_team/
├── ai_systems_team/
├── investment_team/
├── nutrition_meal_planning_team/
├── road_trip_planning_team/
├── sales_team/
├── startup_advisor/                   # Persistent conversational startup advisor
├── agent_repair_team/                 # Agent crash recovery
├── integrations/                      # Shared integrations layer used across teams
├── llm_service/                       # Centralized LLM client (Ollama, dummy)
├── docker/                            # Agents-only Docker assets
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

This mounts all 19 enabled team APIs behind one server on port `8080` by default.

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
- `road_trip_planning_team/`
- `sales_team/`
- `startup_advisor/`
- `agentic_team_provisioning/`
- `llm_service/README.md`

## Shared integrations

`integrations/` provides provider-neutral integration contracts and routing that can be reused by any team.
