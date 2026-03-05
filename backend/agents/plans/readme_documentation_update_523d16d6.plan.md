---
name: README Documentation Update
overview: Add README.md files to teams missing documentation (frontend_team, api/) and update the root README.md to be comprehensive with all teams, testing instructions, and deployment guidance.
todos:
  - id: frontend-team-readme
    content: Create software_engineering_team/frontend_team/README.md with agent roster, pipeline diagram, and quality gates
    status: completed
  - id: api-readme
    content: Create api/README.md documenting the Blog Research & Review HTTP API
    status: completed
  - id: root-readme-update
    content: Update root README.md with all teams, testing section, prerequisites, and deployment guidance
    status: completed
isProject: false
---

# README Documentation Update Plan

## Summary of Findings

**Teams missing README.md (2 directories):**

1. `[software_engineering_team/frontend_team/](software_engineering_team/frontend_team/)` - Sub-team with 9 agents (UX Designer, UI Designer, Design System, Frontend Architect, Feature Agent, UX Engineer, Performance Engineer, Build/Release, Accessibility Agent)
2. `[api/](api/)` - Root-level Blog Research and Review HTTP API

**Root README.md gaps:**

- Missing `market_research_team/` from project structure and table
- "How to run each team" table missing `market_research_team` and `investment_team`
- Mermaid diagram incomplete (missing SOC2, investment, market research teams)
- No testing section
- Limited prerequisites/environment setup details
- No deployment/production guidance

---

## 1. Create `software_engineering_team/frontend_team/README.md`

Include:

- Team overview and purpose (frontend sub-orchestration)
- Agent roster table (9 agents with roles):
  - UX Designer - User flows and interaction patterns
  - UI Designer - Visual design and component specs
  - Design System - Component library alignment
  - Frontend Architect - Technical architecture decisions
  - Feature Agent - Implementation (FrontendExpertAgent)
  - UX Engineer - Polish and micro-interactions
  - Performance Engineer - Bundle size, rendering optimization
  - Build/Release - CI/CD and release planning
  - Accessibility Agent - WCAG 2.2 compliance
- Pipeline diagram showing the workflow order:

```
UX Designer -> UI Designer -> Design System -> Frontend Architect -> Feature Implementation -> UX Engineer -> Performance Engineer -> Build/Release
```

- Quality gates integration (QA, Security, Code Review, Acceptance Verifier, DbC)
- Lightweight task path (skips design phase for fix/patch tasks)
- Reference to parent `[software_engineering_team/README.md](software_engineering_team/README.md)`

---

## 2. Create `api/README.md`

Include:

- Purpose: Blog Research and Review HTTP API (port 8000)
- Relationship to `[blogging/](blogging/)` agents
- Endpoints documented:
  - `POST /research-and-review` - Run research + review pipeline
  - `GET /health` - Health check
- Request/response examples
- How to run:
  - `PYTHONPATH=blogging uvicorn api.main:app --reload --host 0.0.0.0 --port 8000`
- Environment variables (`TAVILY_API_KEY`)
- Reference to full blogging pipeline in `[blogging/README.md](blogging/README.md)`

---

## 3. Update Root `README.md`

### 3.1 Update project structure

Add `market_research_team/` to the directory tree and table:

```
strands-agents/
├── api/                            # Blog research-and-review HTTP API
├── blogging/                       # Blogging agent suite
├── software_engineering_team/      # Full software dev team simulation
├── social_media_marketing_team/    # Campaign planning with platform specialists
├── soc2_compliance_team/           # SOC2 compliance audit and certification
├── investment_team/                # Multi-asset investment organization
├── market_research_team/           # Market research and concept viability
└── requirements.txt
```

### 3.2 Update "How to run each team" table

Add missing teams:


| Team            | Directory | Command                                                                | Port |
| --------------- | --------- | ---------------------------------------------------------------------- | ---- |
| Investment team | package   | `uvicorn investment_team.api.main:app --host 0.0.0.0 --port 8030`      | 8030 |
| Market research | package   | `uvicorn market_research_team.api.main:app --host 0.0.0.0 --port 8010` | 8010 |


### 3.3 Update mermaid diagram

Add all 6 teams with their key agents/components

### 3.4 Add Testing section

Document how to run tests:

```bash
# Run all tests
pytest

# Run specific team tests
pytest software_engineering_team/tests/ -v
pytest blogging/tests/ -v
pytest market_research_team/tests/ -v
pytest soc2_compliance_team/tests/ -v
pytest social_media_marketing_team/tests/ -v

# Run with logs
pytest tests/ -v --log-cli-level=INFO
```

### 3.5 Add Prerequisites section

- Python 3.10+
- NVM + Node 22.12+ (for frontend builds in software_engineering_team)
- Ollama (optional, for LLM)
- API keys (TAVILY_API_KEY for blogging research)

### 3.6 Add Environment Variables section

Consolidate all team-specific env vars with descriptions

### 3.7 Add Deployment/Production section

- Docker considerations
- Environment variable configuration
- Port allocation summary

---

## File Changes Summary


| File                                                | Action |
| --------------------------------------------------- | ------ |
| `software_engineering_team/frontend_team/README.md` | Create |
| `api/README.md`                                     | Create |
| `README.md`                                         | Update |


