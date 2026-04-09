# Strands Agents User Interface

Angular application providing an interactive UI for all Strands agent APIs: Blogging, Software Engineering Team, Personal Assistant, Market Research, SOC2 Compliance, Social Media Marketing, Branding, Agent Provisioning, Accessibility Audit, AI Systems, Investment (Advisor + Strategy Lab), Nutrition & Meal Planning, Planning V3, Coding Team, StudioGrid, AI Sales Team, Road Trip Planning, Agentic Team Provisioning, Startup Advisor, User Agent Founder, and Deepthought.

## Prerequisites

- **Node.js** v22.12+ or v20.19+ (use [NVM](https://github.com/nvm-sh/nvm) if needed)
- **npm** 10+

## Installation

```bash
cd user-interface
npm ci
```

If using NVM:

```bash
nvm use
npm ci
```

## Configuration

API base URLs are configured in `src/environments/environment.ts` (development) and `src/environments/environment.prod.ts` (production). All teams are served by the **Unified API**:

| API | Default URL | Port |
|-----|-------------|------|
| **Unified API (local)** | `http://localhost:8080` | 8080 |
| **Unified API (Docker)** | `http://localhost:8888` | 8888 (host) → 8080 (container) |

**Note:** The Unified API provides all 21 team APIs under a single endpoint with namespaced prefixes — `/api/blogging`, `/api/software-engineering`, `/api/personal-assistant`, `/api/market-research`, `/api/soc2-compliance`, `/api/social-marketing`, `/api/branding`, `/api/agent-provisioning`, `/api/accessibility-audit`, `/api/ai-systems`, `/api/investment`, `/api/nutrition-meal-planning`, `/api/planning-v3`, `/api/coding-team`, `/api/studio-grid`, `/api/sales`, `/api/road-trip-planning`, `/api/agentic-team-provisioning`, `/api/startup-advisor`, `/api/user-agent-founder`, `/api/deepthought`. When running via Docker Compose the UI at port 4201 proxies all `/api/*` requests to the agents container exposed on host port 8888.

To override, edit `src/environments/environment.ts` before building.

## Development

```bash
ng serve
```

Open `http://localhost:4200/`. The app will reload on file changes.

## Build

```bash
ng build
```

Production build:

```bash
ng build --configuration production
```

Output: `dist/user-interface/`

## Testing

```bash
ng test
```

With code coverage (target: **80%** line coverage for `src/app`):

```bash
npm run test:coverage
```

Or:

```bash
ng test --configuration=ci --no-watch
```

Coverage report: `coverage/user-interface/index.html`. The project aims for at least 80% line coverage under `src/app` (excluding `*.spec.ts` and `*.model.ts`).

**Note:** Tests require Chrome or ChromeHeadless. Set `CHROME_BIN` if Chrome is not in PATH.

## Project Structure

```
src/
├── app/
│   ├── components/     # Feature and shared components
│   ├── core/           # HTTP interceptor, error handling
│   ├── models/         # TypeScript interfaces for API request/response
│   ├── services/       # API services
│   └── shared/         # Loading spinner, error message components
├── environments/       # API base URLs
└── styles.scss         # Global styles
```

## API Endpoints Covered

See [docs/API_MAPPING.md](docs/API_MAPPING.md) for the full UI-to-API mapping.

## Further Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [API Mapping](docs/API_MAPPING.md)
- [Accessibility](docs/ACCESSIBILITY.md)

## UI navigation highlights

- **Software Engineering:** Main dashboard plus nested **Planning** (`/software-engineering/planning-v3`) and **Coding Team** (`/software-engineering/coding-team`) — the latter is the SE sub-team surface; API prefix remains `/api/coding-team`.
- **Investment:** **Advisor & IPS** (`/investment/advisor`), **Strategy Lab** (`/investment/strategy-lab`, profile not required for lab flows), and overview (`/investment`).
- **Agentic Teams:** Process designer with a live **Team Roster** column (agents, roles, skills, staffing validation) alongside chat and the process diagram (`/agentic-teams`).

`src/environments/environment.ts` includes `codingTeamApiUrl` (and the usual unified-style `*ApiUrl` fields) when calling team-specific health endpoints.

## Strands platform

This package is part of the [Strands Agents](../README.md) monorepo (Unified API, Angular UI, and full team index).
