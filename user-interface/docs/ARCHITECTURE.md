# Architecture

## Overview

The user interface is an Angular 19 standalone application that connects to multiple agent APIs. Each API has a dedicated feature area with forms, results display, and health indicators.

## High-Level Structure

```mermaid
flowchart TB
  subgraph ui [Angular App]
    Shell[AppShellComponent]
    Shell --> Blog[BloggingDashboard]
    Shell --> SE[SoftwareEngineeringDashboard]
    Shell --> PA[PersonalAssistantDashboard]
    Shell --> MR[MarketResearchDashboard]
    Shell --> Soc2[SOC2ComplianceDashboard]
    Shell --> SM[SocialMarketingDashboard]
    Blog --> BlogSvc[BloggingApiService]
    SE --> SESvc[SoftwareEngineeringApiService]
    PA --> PASvc[PersonalAssistantApiService]
    MR --> MRSvc[MarketResearchApiService]
    Soc2 --> Soc2Svc[Soc2ComplianceApiService]
    SM --> SMSvc[SocialMarketingApiService]
  end
  subgraph apis [Agent APIs]
    BlogSvc --> BlogAPI["Blogging :8001"]
    SESvc --> SEAPI["SE Team :8000"]
    PASvc --> PAAPI["Personal Assistant :8015"]
    MRSvc --> MRAPI["Market Research :8011"]
    Soc2Svc --> Soc2API["SOC2 :8020"]
    SMSvc --> SMAPI["Social Marketing :8010"]
  end
```

## Routing

- `/` redirects to `/blogging`
- `/blogging` – Blogging API (research-and-review, full-pipeline)
- `/software-engineering` – Software Engineering Team API
- `/personal-assistant` – Personal Assistant API (chat, profile, tasks, calendar, deals, reservations, documents)
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
- `PersonalAssistantApiService`
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

## Personal Assistant Dashboard

The Personal Assistant dashboard (`/personal-assistant`) is a tabbed interface with the following sections:

```mermaid
flowchart TB
  subgraph pa [Personal Assistant Dashboard]
    PAD[PersonalAssistantDashboard]
    PAD --> Chat[PaChatComponent]
    PAD --> Profile[PaProfileComponent]
    PAD --> Tasks[PaTasksComponent]
    PAD --> Calendar[PaCalendarComponent]
    PAD --> Deals[PaDealsComponent]
    PAD --> Reservations[PaReservationsComponent]
    PAD --> Documents[PaDocumentsComponent]
  end
  subgraph api [Personal Assistant API]
    Chat --> AssistantEndpoint["/assistant/jobs?user_id={id}"]
    Profile --> ProfileEndpoint["/users/{id}/profile"]
    Tasks --> TasksEndpoint["/users/{id}/tasks"]
    Calendar --> CalendarEndpoint["/users/{id}/calendar"]
    Deals --> DealsEndpoint["/users/{id}/deals"]
    Reservations --> ReservationsEndpoint["/users/{id}/reservations"]
    Documents --> DocumentsEndpoint["/users/{id}/documents"]
  end
```

### Tab Components

| Tab | Component | Features |
|-----|-----------|----------|
| **Chat** | `PaChatComponent` | Conversational interface, message history, quick actions |
| **Profile** | `PaProfileComponent` | User preferences, goals, identity, professional info |
| **Tasks** | `PaTasksComponent` | Natural language task input, task lists, completion tracking |
| **Calendar** | `PaCalendarComponent` | Event parsing from text, date/time validation |
| **Deals** | `PaDealsComponent` | Wishlist management, deal search |
| **Reservations** | `PaReservationsComponent` | Restaurant/service reservations, natural language input |
| **Documents** | `PaDocumentsComponent` | Document generation (cover letters, emails, reports) |

### Real-Time Features

- Chat uses standard request/response (not streaming)
- Profile, tasks, and other data refresh on tab activation
- Loading states with Material spinners
- Error handling via MatSnackBar
