# Strands Agents

Strands Agents is a monorepo for multi-agent "team" systems. Each team exposes a FastAPI service and the platform also provides a Unified API plus an Angular UI.

## Repository layout

```text
strands-agents/
├── backend/
│   ├── agents/                 # Team implementations + team-specific APIs (19 teams)
│   ├── unified_api/            # Unified FastAPI app mounting all teams
│   ├── run_unified_api.py      # Unified API launcher
│   ├── studiogrid/             # StudioGrid design-system workflow
│   ├── blogging_service/       # Isolated blogging microservice container
│   ├── job_service/            # Centralized job state tracking service (Postgres)
│   ├── team_service/           # Generic team microservice container
│   ├── studiogrid_service/     # StudioGrid microservice container
│   ├── post_mortems/           # Agent failure audit log
│   ├── Makefile                # Build, lint, test, run targets
│   └── pyproject.toml          # Ruff + pytest config
├── user-interface/             # Angular 19 frontend
└── docker/                     # Full-stack Docker Compose setup
```

## Quick start

### 1) Backend dependencies

```bash
cd backend
make install          # Create venv, install deps
# Or manually:
pip install -r requirements.txt
```

### 2) Run the Unified API (recommended)

```bash
cd backend
python run_unified_api.py
```

Unified API docs: <http://localhost:8080/docs>

### 3) Run the UI

```bash
cd user-interface
npm ci
npm start
```

UI: <http://localhost:4200>

## Unified API team routes

The Unified API mounts teams under `/api/*` prefixes. Current configured routes (19 teams):

- `/api/blogging`
- `/api/software-engineering`
- `/api/personal-assistant`
- `/api/market-research`
- `/api/soc2-compliance`
- `/api/social-marketing`
- `/api/branding`
- `/api/agent-provisioning`
- `/api/accessibility-audit`
- `/api/ai-systems`
- `/api/investment`
- `/api/nutrition-meal-planning`
- `/api/planning-v3`
- `/api/coding-team`
- `/api/studio-grid`
- `/api/sales`
- `/api/road-trip-planning`
- `/api/agentic-team-provisioning`
- `/api/startup-advisor`

## Team documentation

- `backend/agents/README.md` (backend agent monorepo overview)
- `backend/agents/software_engineering_team/README.md`
- `backend/agents/blogging/README.md`
- `backend/agents/personal_assistant_team/README.md`
- `backend/agents/social_media_marketing_team/README.md`
- `backend/agents/market_research_team/README.md`
- `backend/agents/soc2_compliance_team/README.md`
- `backend/agents/branding_team/README.md`
- `backend/agents/agent_provisioning_team/README.md`
- `backend/agents/accessibility_audit_team/README.md`
- `backend/agents/ai_systems_team/README.md`
- `backend/agents/investment_team/README.md`
- `backend/agents/nutrition_meal_planning_team/README.md`
- `backend/agents/planning_v3_team/README.md`
- `backend/agents/coding_team/README.md`
- `backend/agents/sales_team/` (AI Sales Team)
- `backend/agents/road_trip_planning_team/` (Road Trip Planning)
- `backend/agents/agentic_team_provisioning/` (Agentic Team Provisioning)
- `backend/agents/startup_advisor/` (Startup Advisor)

## Docker

For the full stack (Postgres, Temporal, optional Ollama, backend APIs, and UI):

```bash
docker compose -f docker/docker-compose.yml --env-file docker/.env up --build
```

See `docker/README.md` for env vars, ports, and deployment notes.

## Additional docs

- `backend/unified_api/README.md`
- `backend/studiogrid/README.md`
- `backend/post_mortems/POST_MORTEMS.md`
- `ARCHITECTURE.md`
- `CONTRIBUTORS.md`
