# Architecture

## Overview

The user interface is an Angular 19 standalone application that connects to five agent APIs. Each API has a dedicated feature area with forms, results display, and health indicators.

## High-Level Structure

```mermaid
flowchart TB
  subgraph ui [Angular App]
    Shell[AppShellComponent]
    Shell --> Blog[BloggingDashboard]
    Shell --> SE[SoftwareEngineeringDashboard]
    Shell --> MR[MarketResearchDashboard]
    Shell --> Soc2[SOC2ComplianceDashboard]
    Shell --> SM[SocialMarketingDashboard]
    Blog --> BlogSvc[BloggingApiService]
    SE --> SESvc[SoftwareEngineeringApiService]
    MR --> MRSvc[MarketResearchApiService]
    Soc2 --> Soc2Svc[Soc2ComplianceApiService]
    SM --> SMSvc[SocialMarketingApiService]
  end
  subgraph apis [Agent APIs]
    BlogSvc --> BlogAPI["Blogging :8000"]
    SESvc --> SEAPI["SE Team :8000"]
    MRSvc --> MRAPI["Market Research :8011"]
    Soc2Svc --> Soc2API["SOC2 :8020"]
    SMSvc --> SMAPI["Social Marketing :8010"]
  end
```

## Routing

- `/` redirects to `/blogging`
- `/blogging` – Blogging API (research-and-review, full-pipeline)
- `/software-engineering` – Software Engineering Team API
- `/market-research` – Market Research API
- `/soc2-compliance` – SOC2 Compliance API
- `/social-marketing` – Social Media Marketing API

## Core Modules

### `core/`

- **error-handler.interceptor.ts** – Catches HTTP errors, shows MatSnackBar, rethrows for caller handling

### `shared/`

- **loading-spinner** – Reusable loading indicator
- **error-message** – Inline error display

### `models/`

TypeScript interfaces mirroring backend Pydantic models for type-safe API calls.

### `services/`

One service per API:

- `BloggingApiService`
- `SoftwareEngineeringApiService`
- `MarketResearchApiService`
- `Soc2ComplianceApiService`
- `SocialMarketingApiService`

## Feature Structure

Each feature follows the same pattern:

1. **Dashboard component** – Container with tabs or sections
2. **Form component(s)** – Collect request payload, emit on submit
3. **Results/status component(s)** – Display response, poll when needed
4. **Health indicator** – Calls `GET /health` for the API

## Data Flow

1. User fills form → component emits request
2. Dashboard calls service method with request
3. Service uses `HttpClient` to call API
4. On success: dashboard stores result, passes to results component
5. On error: interceptor shows snackbar; dashboard may set inline error

## Polling

Job-based APIs (SOC2, Social Marketing, Software Engineering) use `timer(0, 60000).pipe(switchMap(...))` to poll status every 60 seconds until completed or failed.

## SSE

Software Engineering execution stream uses `EventSource` to subscribe to `GET /execution/stream` for real-time events.
