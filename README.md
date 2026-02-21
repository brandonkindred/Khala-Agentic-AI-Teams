# Strands Agents

**Strands Agents** is a monorepo of multi-agent systems built in the Strands style—research, orchestration, and human-in-the-loop workflows. Each team simulates specialized roles (engineers, marketers, auditors, researchers) and produces production-ready artifacts.

---

## Table of Contents

- [Overview](#overview)
- [Quick Start](#quick-start)
- [Project Structure](#project-structure)
- [Agent Teams](#agent-teams)
- [User Interface](#user-interface)
- [Build & Run](#build--run)
- [Docker Deployment](#docker-deployment)
- [Configuration & Environment](#configuration--environment)
- [Testing](#testing)
- [Deployment](#deployment)
- [Contributing](#contributing)
- [License](#license)

---

## Overview

This repository provides **multiple Strands-style agent systems** in a unified monorepo:

| Team | Purpose |
|------|---------|
| **Software Engineering Team** | Full dev team simulation: architecture, Tech Lead, backend/frontend workers, DevOps, security, QA, code review, accessibility. Reads `initial_spec.md` and produces working code. |
| **Blogging** | Research, review, draft, copy-edit, and publication agents for content creation (web + arXiv research, title/outline, draft with style guide, copy-editor loop, platform-specific output). |
| **Social Media Marketing** | Campaign planning with collaboration agents, human approval gate, and platform specialists (LinkedIn, Facebook, Instagram, X). Produces execution-ready content plans. |
| **SOC2 Compliance** | Multi-agent SOC2 audit: Security, Availability, Processing Integrity, Confidentiality, Privacy TSC agents review a repo and produce a compliance report or next-steps document. |
| **Market Research** | Human-AI collaborative workflow for user discovery and product concept viability; transcript ingestion, UX synthesis, experiment scripts, human approval gates. |
| **Investment Team** | Multi-asset investment organization with IPS hard constraints, strategy validation, promotion gates, separation-of-duties, risk veto, and monitor-only safety degradation. |

The **User Interface** is an Angular 19 application that provides interactive dashboards for all agent APIs.

---

## Quick Start

### Option A: Docker (All 6 Agent APIs + Tools)

```bash
# Ensure Ollama is running on the host
curl http://localhost:11434/api/tags

# From project root
cd agents
docker-compose up -d

# Verify APIs
curl http://localhost:18000/health   # Software Engineering
curl http://localhost:18001/health   # Blogging
curl http://localhost:18002/health   # Market Research
curl http://localhost:18003/health   # SOC2 Compliance
curl http://localhost:18004/health   # Social Marketing
curl http://localhost:18005/health   # Blog Research
```

### Option B: Local Development

**1. Install dependencies**

```bash
# Python (from repo root)
pip install -r agents/requirements.txt
pip install -r agents/software_engineering_team/requirements.txt
pip install -r agents/blogging/requirements.txt

# Node.js for UI (use NVM - Node 22.12 recommended per .nvmrc)
cd user-interface
nvm use          # or: nvm install (if 22.12 not installed)
npm ci
```

**2. Start agent APIs** (each in its own terminal)

```bash
# Software Engineering (port 8000)
cd agents && python -m uvicorn software_engineering_team.api.main:app --host 0.0.0.0 --port 8000

# Blogging (port 8001)
cd agents && PYTHONPATH=blogging python -m uvicorn blogging.api.main:app --host 0.0.0.0 --port 8001

# Market Research (port 8011)
cd agents && python -m uvicorn market_research_team.api.main:app --host 0.0.0.0 --port 8011

# SOC2 Compliance (port 8020)
cd agents && python -m uvicorn soc2_compliance_team.api.main:app --host 0.0.0.0 --port 8020

# Social Marketing (port 8010)
cd agents && python -m uvicorn social_media_marketing_team.api.main:app --host 0.0.0.0 --port 8010
```

**3. Start the User Interface**

```bash
cd user-interface
ng serve
```

Open **http://localhost:4200/**.

---

## Project Structure

```
strands-agents/
├── agents/                          # All agent teams (Python)
│   ├── api/                         # Blog research-and-review HTTP API
│   ├── blogging/                    # Blogging agent suite
│   ├── software_engineering_team/   # Full software dev team simulation
│   ├── social_media_marketing_team/ # Campaign planning with platform specialists
│   ├── soc2_compliance_team/        # SOC2 compliance audit
│   ├── investment_team/             # Multi-asset investment (IPS-first)
│   ├── market_research_team/        # Market research and concept viability
│   ├── docker/                      # Docker config, default spec
│   ├── Dockerfile                   # Multi-stage image (all 6 teams)
│   ├── docker-compose.yml           # Run all APIs in one container
│   ├── supervisord.conf             # Process manager for container
│   ├── entrypoint.sh
│   └── requirements.txt
├── user-interface/                  # Angular 19 UI
│   ├── src/
│   │   ├── app/
│   │   │   ├── components/          # Feature and shared components
│   │   │   ├── core/                # HTTP interceptor, error handling
│   │   │   ├── models/              # TypeScript interfaces for API
│   │   │   ├── services/            # API services
│   │   │   └── shared/             # Loading spinner, error message
│   │   └── environments/            # API base URLs
│   ├── docs/                        # Architecture, API mapping, accessibility
│   └── angular.json
├── README.md                        # This file
└── CONTRIBUTORS.md                  # Contribution guidelines
```

---

## Agent Teams

### Software Engineering Team

Multi-agent dev team: Spec Intake, Project Planning, Architecture, Tech Lead, Backend/Frontend workers, DevOps, Security, QA, Code Review, Accessibility, Integration, Documentation.

- **Spec:** `initial_spec.md` at repo root
- **Output:** Working code in `backend/` and `frontend/`, CI/CD, docs
- **Docs:** [agents/software_engineering_team/README.md](agents/software_engineering_team/README.md)

### Blogging

Research → Review → Draft → Copy Editor loop → Publication. Web + arXiv research, title/outline, style-guided draft, platform-specific output (Medium, dev.to, Substack).

- **API:** `POST /research-and-review`, `POST /full-pipeline`
- **Docs:** [agents/blogging/README.md](agents/blogging/README.md)

### Social Media Marketing

Campaign proposal collaboration, human approval gate, concept scoring (≥70% engagement), platform specialists (LinkedIn, Facebook, Instagram, X).

- **API:** `POST /social-marketing/run`, `GET /social-marketing/status/{id}`, `POST /social-marketing/revise/{id}`
- **Docs:** [agents/social_media_marketing_team/README.md](agents/social_media_marketing_team/README.md)

### SOC2 Compliance

Five TSC agents (Security, Availability, Processing Integrity, Confidentiality, Privacy) + Report Writer. Produces compliance report or next-steps document.

- **API:** `POST /soc2-audit/run`, `GET /soc2-audit/status/{id}`
- **Docs:** [agents/soc2_compliance_team/README.md](agents/soc2_compliance_team/README.md)

### Market Research

Human-AI workflow: transcript ingestion, UX synthesis, viability recommendation, research scripts, human approval gates.

- **API:** `POST /market-research/run`
- **Docs:** [agents/market_research_team/README.md](agents/market_research_team/README.md)

### Investment Team

IPS-first multi-asset organization with PolicyGuardian, Validation, PromotionGate, InvestmentCommittee agents. Separation-of-duties, risk veto, monitor-only safety.

- **Docs:** [agents/investment_team/README.md](agents/investment_team/README.md)

---

## User Interface

Angular 19 standalone app with dashboards for each agent API.

### Routes

| Route | API |
|-------|-----|
| `/` | Redirects to `/blogging` |
| `/blogging` | Blogging (research-and-review, full-pipeline) |
| `/software-engineering` | Software Engineering Team |
| `/market-research` | Market Research |
| `/soc2-compliance` | SOC2 Compliance |
| `/social-marketing` | Social Media Marketing |

### Features

- Forms for each API's request payload
- Job status polling for async endpoints
- SSE stream for Software Engineering execution
- Health indicators per API
- WCAG 2.2–oriented accessibility

### Docs

- [user-interface/docs/ARCHITECTURE.md](user-interface/docs/ARCHITECTURE.md)
- [user-interface/docs/API_MAPPING.md](user-interface/docs/API_MAPPING.md)
- [user-interface/docs/ACCESSIBILITY.md](user-interface/docs/ACCESSIBILITY.md)

---

## Build & Run

### Prerequisites

| Tool | Version | Notes |
|------|---------|-------|
| **Python** | 3.10+ | For all agent teams |
| **Node.js** | 22.12+ or 20.19+ | Use [NVM](https://github.com/nvm-sh/nvm); required for SE team frontend builds and UI |
| **npm** | 10+ | |
| **Ollama** | (optional) | For local LLM inference |
| **Docker** | 20.10+ | For containerized deployment |
| **Docker Compose** | v2+ | |

### Build User Interface

```bash
cd user-interface
npm ci
ng build
# Production: ng build --configuration production
# Output: dist/user-interface/
```

### Run Individual Agent APIs

| Team | Command | Port |
|------|---------|------|
| Software Engineering | `uvicorn software_engineering_team.api.main:app --host 0.0.0.0 --port 8000` | 8000 |
| Blogging | `PYTHONPATH=blogging uvicorn blogging.api.main:app --host 0.0.0.0 --port 8001` | 8001 |
| Market Research | `uvicorn market_research_team.api.main:app --host 0.0.0.0 --port 8011` | 8011 |
| SOC2 Compliance | `uvicorn soc2_compliance_team.api.main:app --host 0.0.0.0 --port 8020` | 8020 |
| Social Marketing | `uvicorn social_media_marketing_team.api.main:app --host 0.0.0.0 --port 8010` | 8010 |
| Blog Research | `PYTHONPATH=blogging uvicorn api.main:app --host 0.0.0.0 --port 8005` | 8005 |

Run from `agents/` directory with `PYTHONPATH` set as needed.

---

## Docker Deployment

Run all 6 agent teams in one container with pre-installed tools (Node.js, Angular CLI, Git, Docker-in-Docker).

### Quick Start

```bash
cd agents
docker-compose up -d
```

### Port Mapping (Host → Container)

| Host Port | Container Port | Team |
|-----------|----------------|------|
| 18000 | 8000 | Software Engineering |
| 18001 | 8001 | Blogging |
| 18002 | 8002 | Market Research |
| 18003 | 8003 | SOC2 Compliance |
| 18004 | 8004 | Social Marketing |
| 18005 | 8005 | Blog Research |

### Using the UI with Docker

When APIs run in Docker, point the UI to host ports 18000–18005. Edit `user-interface/src/environments/environment.ts`:

```typescript
export const environment = {
  production: false,
  bloggingApiUrl: 'http://localhost:18001',
  softwareEngineeringApiUrl: 'http://localhost:18000',
  marketResearchApiUrl: 'http://localhost:18002',
  soc2ComplianceApiUrl: 'http://localhost:18003',
  socialMarketingApiUrl: 'http://localhost:18004',
};
```

Or use the Blog Research API at 18005 for research-and-review.

### Custom Spec (Software Engineering)

Pass a spec file at build time:

```bash
SPEC_FILE=./my-project/initial_spec.md docker-compose build
```

Default spec: `docker/default_initial_spec.md` (Task Manager API example).

### Volume Mounts

| Host | Container | Purpose |
|------|-----------|---------|
| `./workspace` | `/workspace` | Agent output (repos, specs, artifacts) |

### Full Docker Docs

See [agents/docker/README.md](agents/docker/README.md) for environment variables, troubleshooting, and security notes.

---

## Configuration & Environment

### Agent Environment Variables

| Variable | Team | Description | Default |
|----------|------|-------------|---------|
| `TAVILY_API_KEY` | Blogging | Web search API key | Required for research |
| `SW_LLM_PROVIDER` | Software Engineering | `dummy` or `ollama` | `dummy` |
| `SW_LLM_MODEL` | Software Engineering | Model name | `qwen3-coder-next:cloud` |
| `SW_LLM_BASE_URL` | Software Engineering | Ollama API URL | `http://127.0.0.1:11434` |
| `SW_LLM_MODEL_<AGENT>` | Software Engineering | Per-agent model override | - |
| `SOC2_LLM_PROVIDER` | SOC2 | `ollama` or `dummy` | `ollama` |
| `SOC2_LLM_MODEL` | SOC2 | Model name | `llama3.1` |
| `SOC2_LLM_BASE_URL` | SOC2 | Ollama API URL | `http://127.0.0.1:11434` |

### UI Environment Files

- **Development:** `user-interface/src/environments/environment.ts`
- **Production:** `user-interface/src/environments/environment.prod.ts` (uses `/api/*` proxy paths)

---

## Testing

### Python (Agents)

Run tests **per team** (each team has its own `PYTHONPATH` and imports):

```bash
# From repo root - run each team's tests
pytest agents/software_engineering_team/tests/ -v
pytest agents/blogging/tests/ -v
pytest agents/market_research_team/tests/ -v
pytest agents/soc2_compliance_team/tests/ -v
pytest agents/social_media_marketing_team/tests/ -v

# Or from each team directory
cd agents/software_engineering_team && pytest
cd agents/blogging && pytest
# ... etc

# With logs
pytest agents/<team>/tests/ -v --log-cli-level=INFO
```

**Note:** Running `pytest` without a path from repo root may fail due to import path differences between teams.

### Angular (UI)

```bash
cd user-interface
ng test
```

With coverage:

```bash
ng test --no-watch --code-coverage
# Report: coverage/user-interface/index.html
```

**Note:** Tests require Chrome or ChromeHeadless. Set `CHROME_BIN` if Chrome is not in PATH.

---

## Deployment

### Port Allocation

Default ports: 8000 (SE/Blog), 8010 (social), 8011 (market research), 8020 (SOC2). Override with `--port` when running multiple services.

### Production Checklist

1. Set all required environment variables (e.g. `TAVILY_API_KEY`, `SW_LLM_*`).
2. Run `uvicorn` without `--reload`.
3. Use a process manager (systemd, supervisord) or container orchestration.
4. Put a reverse proxy (TLS, auth, rate limiting) in front of APIs.
5. For the UI, use production build and configure API proxy or CORS.

### Example Production Command

```bash
uvicorn software_engineering_team.api.main:app --host 0.0.0.0 --port 8000 --workers 2
```

---

## Contributing

See [CONTRIBUTORS.md](CONTRIBUTORS.md) for contribution guidelines, code standards, and pull request process.

---

## License

This project is licensed under the Apache License 2.0. See the [LICENSE](LICENSE) file for details.
