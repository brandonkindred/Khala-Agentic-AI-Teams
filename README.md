# Khala

Khala is a monorepo for multi-agent "team" systems. Each team exposes a FastAPI service and the platform also provides a Unified API plus an Angular UI.

(Named after the Protoss unifying religion from StarCraft — a psionic link joining many minds into one.)

## Repository layout

```text
khala/
├── backend/
│   ├── agents/                 # Team implementations + team-specific APIs (20 enabled teams)
│   ├── unified_api/            # Unified FastAPI app mounting all teams
│   ├── run_unified_api.py      # Unified API launcher
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

The Unified API mounts teams under `/api/*` prefixes. Current configured routes (20 enabled teams):

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
- `/api/investment` (also serves the Investment Strategy Lab sub-team — disabled as a separate mount)
- `/api/nutrition-meal-planning`
- `/api/planning-v3`
- `/api/coding-team` (logical sub-team of software engineering)
- `/api/sales`
- `/api/road-trip-planning`
- `/api/agentic-team-provisioning`
- `/api/startup-advisor`
- `/api/user-agent-founder`
- `/api/deepthought`

## Platform notes (cross-cutting)

- **Software Engineering sub-teams (UI):** Under **Development**, the UI nests **Planning** (`/software-engineering/planning-v3`) and **Coding Team** (`/software-engineering/coding-team`). The **Coding Team** is also a **logical sub-team** in Unified API config (`parent_team_key`: software engineering); HTTP API remains `/api/coding-team`.
- **Investment (two tracks):** One API prefix (`/api/investment`) covers **Advisor / IPS** (user profile) and **Strategy Lab** (ideation and backtests without a profile). UI routes: `/investment`, `/investment/advisor`, `/investment/strategy-lab`. See `backend/agents/investment_team/README.md`.
- **Agentic Team Provisioning:** Conversational design of rosters and processes; roster validation and optional Agent Provisioning bridge. See `backend/agents/agentic_team_provisioning/README.md` and `AGENTIC_TEAM_ARCHITECTURE.md`.
- **Agent anatomy (single agents):** `backend/agents/agent_provisioning_team/AGENT_ANATOMY.md` — standard structure for provisioned AI agents (I/O, tools, memory, prompts, guardrails, subagents).

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
- `backend/agents/sales_team/README.md` (AI Sales Team)
- `backend/agents/road_trip_planning_team/README.md` (Road Trip Planning)
- `backend/agents/agentic_team_provisioning/` (Agentic Team Provisioning)
- `backend/agents/startup_advisor/README.md` (Startup Advisor)
- `backend/agents/user_agent_founder/README.md` (User Agent Founder)
- `backend/agents/deepthought/README.md` (Deepthought recursive agent)

## Docker

For the full stack (Postgres, Temporal, optional Ollama, backend APIs, and UI):

```bash
docker compose -f docker/docker-compose.yml --env-file docker/.env up --build
```

See `docker/README.md` for env vars, ports, and deployment notes.

## Additional docs

- `backend/unified_api/README.md` (mounts, `TeamConfig`, optional `parent_team_key`, logical disabled teams)
- `ARCHITECTURE.md`
- `CONTRIBUTORS.md`
- `CLAUDE.md` (Cursor / Claude Code guidance for this repo)

Individual package READMEs under `backend/agents/**` and `user-interface/` end with a **Khala platform** link back to this file.
