# Strands Agents User Interface

Angular application providing an interactive UI for all Strands agent APIs: Blogging, Software Engineering Team, Personal Assistant, Market Research, SOC2 Compliance, Social Media Marketing, and Branding.

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

API base URLs are configured in `src/environments/environment.ts` (development) and `src/environments/environment.prod.ts` (production). Default ports:

| API | Default URL | Port |
|-----|-------------|------|
| Blogging | `http://localhost:8001` | 8001 |
| Software Engineering Team | `http://localhost:8000` | 8000 |
| Personal Assistant | `http://localhost:8015` | 8015 |
| Market Research | `http://localhost:8011` | 8011 |
| SOC2 Compliance | `http://localhost:8020` | 8020 |
| Social Media Marketing | `http://localhost:8010` | 8010 |
| Branding | `http://localhost:8012` | 8012 |
| **Unified API** | `http://localhost:8080` | 8080 |

**Note:** The Unified API provides all team APIs under a single endpoint with namespaced prefixes (e.g., `/api/blogging`, `/api/personal-assistant`).

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
