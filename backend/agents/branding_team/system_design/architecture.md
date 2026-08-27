# Branding Team — Architecture

## Overview

The branding team is a **5-phase enterprise brand development pipeline**
organized as a digital branding agency. Each of its specialist agents owns a
single phase of brand development, and the phases must execute in a strict
dependency order because each one consumes the outputs of its predecessors.

The team is structured around two intertwined ideas:

1. **Phase-gated pipeline** — the orchestrator sequences five phases
   (Strategic Core → Narrative & Messaging → Visual Identity →
   Channel Activation → Governance & Evolution), and a human review gate
   controls progression from one phase to the next. See
   `orchestrator.py:51-114` for the phase order and gate builder.
2. **Agency model** — a single `Client` can own many `Brand` entities; each
   `Brand` carries its own mission, status, current phase, and versioned
   run history. See `models.py:514-528` and `store.py:100-252`.

On top of this pipeline the team exposes **three concurrent API styles** —
synchronous one-shot runs, interactive Q&A sessions, and LLM-driven
conversations — so clients with very different starting points (fully
briefed vs. exploratory vs. chat-first) can all reach a consistent
`TeamOutput`.

## Architectural principles

- **Dependency order is enforced in code, not documentation.**
  `orchestrator.run()` only executes phase N+1 if phase N produced a
  non-`None` output, and the `_build_phase_gates()` helper marks earlier
  phases as `APPROVED`, the current phase as `PENDING_REVIEW` (or
  `APPROVED` if `human_review.approved`), and later phases as
  `NOT_STARTED`. See `orchestrator.py:69-81` and `orchestrator.py:184-234`.
- **Human-in-the-loop by default.** Every `TeamOutput` includes a
  `WorkflowStatus` that is only `READY_FOR_ROLLOUT` when all five phases
  completed *and* `human_review.approved` is true
  (`orchestrator.py:302-322`). Anything else returns
  `NEEDS_HUMAN_DECISION`.
- **Consolidated output shape.** Specialist agents
  (`VoicePrinciplesDrafter`, the `MoodBoardConceptualist` swarm,
  `converge_decider`, `design_system_codifier`, `brand_rules_codifier`,
  `asset_wiki_planner`) contribute nested fields on the phase outputs —
  e.g. `writing_guidelines` on `NarrativeMessagingOutput`;
  `mood_board_candidates` / `creative_refinement` / `design_system` on
  `VisualIdentityOutput`; `brand_guidelines` / `wiki_backlog` on
  `GovernanceOutput` — which `TeamOutput` surfaces via its phase-output
  fields. There is no top-level `BrandCodification` type.
- **Composable external work via thin adapters.** Market research and
  design asset generation are delegated to sibling teams through HTTP
  adapters (`adapters/market_research.py`, `adapters/design_assets.py`).
  The orchestrator never imports the other teams directly — it only calls
  the adapter functions and tolerates their absence.
- **Persistence-agnostic API surface.** The public endpoints take and
  return Pydantic models; the underlying storage is the shared Khala
  Postgres instance, accessed via `shared.postgres.get_conn`
  (`store.py:30,103`) with DDL declared in `postgres/__init__.py`.
  Swapping stores does not change the API contract.
- **Graceful LLM fallback.** The conversational assistant's two stages
  (conversation + silent `structured_output=MissionUpdate` extraction, see
  `system_design.md`'s LLM integration section) are each built via
  `graphs/shared.py:build_agent()` as soon as `BrandingAssistantAgent.__init__`
  runs — construction itself isn't lazy, only the FastAPI wrapper around it
  is (`_get_assistant_agent()` defers constructing the assistant until the
  first conversation request). The two stages fail independently at
  runtime: either stage raising falls back to a hard-coded reply and
  default suggested questions, or a `degraded=True` extraction result,
  instead of surfacing an exception (`assistant/agent.py:respond()`). The
  FastAPI app also mounts even when `llm_service` is unavailable
  (`api/main.py:72-83`).

## Component diagram

```mermaid
flowchart TB
    subgraph clients [External clients]
        CLI[CLI / curl]
        UI[Angular UI]
        Agents[Other Khala teams]
    end

    subgraph gateway [Unified API gateway]
        UnifiedAPI["Unified API<br/>/api/branding/*<br/>unified_api/config.py:88-95"]
    end

    subgraph api_layer [FastAPI — branding_team/api/main.py]
        direction TB
        AgencyAPI["Agency API<br/>/clients, /brands"]
        RunAPI["Sync run<br/>POST /run<br/>POST /brands/{id}/run<br/>POST /brands/{id}/run/{phase}"]
        SessionAPI["Session API<br/>/sessions, /questions"]
        ConvAPI["Conversation API<br/>/conversations"]
        IntegAPI["Integration endpoints<br/>/request-market-research<br/>/request-design-assets"]
    end

    subgraph orch_layer [Orchestration — orchestrator.py]
        Orch["BrandingTeamOrchestrator<br/>run()  run_phase()"]
        Gates["Phase gate logic<br/>_build_phase_gates()"]
        BookBuilder["_build_brand_book()<br/>Consolidated BrandBook"]
    end

    subgraph agents_layer [Specialist agents — agents.py]
        direction TB
        P1["Phase 1<br/>StrategicCoreAgent"]
        P2["Phase 2<br/>NarrativeMessagingAgent"]
        P3["Phase 3<br/>VisualIdentityAgent"]
        P4["Phase 4<br/>ChannelActivationAgent"]
        P5["Phase 5<br/>GovernanceAgent"]
        Compliance["BrandComplianceAgent"]
        subgraph specialists [Specialist agents]
            L1["VoicePrinciplesDrafter"]
            L2["MoodBoardConceptualist swarm"]
            L3["converge_decider"]
            L4["design_system_codifier / brand_rules_codifier"]
            L5["asset_wiki_planner"]
        end
    end

    subgraph assistant_layer [Conversational assistant — assistant/]
        AssistantAgent["BrandingAssistantAgent<br/>agent.py"]
        Prompts["System + user prompts<br/>prompts.py"]
        LLM["llm_service.get_client<br/>('branding_assistant')"]
    end

    subgraph persistence [Persistence]
        BrandingStore["BrandingStore<br/>store.py (Postgres)"]
        SessionStore["BrandingSessionStore<br/>api/state.py (Postgres)"]
        ConvStore["BrandingConversationStore<br/>assistant/store.py (Postgres)"]
        PG["Postgres schema<br/>postgres/__init__.py"]
    end

    subgraph adapters [External integrations — adapters/]
        MR["request_market_research()<br/>market_research.py"]
        DA["request_design_assets()<br/>design_assets.py"]
    end

    subgraph runtime [Runtime wrappers]
        Temporal["BrandingWorkflow<br/>temporal/__init__.py<br/>task queue: branding-queue"]
    end

    CLI --> UnifiedAPI
    UI --> UnifiedAPI
    Agents --> UnifiedAPI
    UnifiedAPI --> api_layer

    AgencyAPI --> BrandingStore
    RunAPI --> Orch
    SessionAPI --> Orch
    ConvAPI --> AssistantAgent
    ConvAPI --> Orch
    IntegAPI --> MR
    IntegAPI --> DA

    Orch --> Gates
    Orch --> BookBuilder
    Orch --> P1 --> P2 --> P3 --> P4 --> P5
    Orch --> Compliance
    Orch --> L1
    Orch --> L2
    Orch --> L3
    Orch --> L4
    Orch --> L5

    Orch --> MR
    Orch --> DA

    AssistantAgent --> Prompts
    AssistantAgent --> LLM

    AgencyAPI --> BrandingStore
    SessionAPI --> SessionStore
    ConvAPI --> ConvStore
    BrandingStore --> PG
    SessionStore --> PG
    ConvStore --> PG

    Temporal -. wraps .-> Orch
```

## Key design decisions

### 1. Why five phases with gates

The five phases map onto how real brand systems are built: strategy first
(who are we?), then verbal identity (how do we talk?), then visual identity
(how do we look?), then activation (where do we show up?), then governance
(how do we stay on-brand?). Skipping ahead produces incoherent output — you
cannot define channel guidelines without a voice, and you cannot define a
voice without a positioning statement. The orchestrator encodes this as
`_PHASE_ORDER` (`orchestrator.py:52-58`) and only executes phase N+1 if
phase N's output is non-`None` (`orchestrator.py:207-234`). Gate status is
tracked explicitly in the `PhaseGate` model so the UI can show stakeholders
what they're approving (`models.py:370-375`).

### 2. Why specialist agents are retained in the pipeline

The phase-output models carry the specialist fields
(`writing_guidelines` on narrative; `mood_board_candidates` /
`creative_refinement` / `design_system` on visual identity;
`brand_guidelines` / `wiki_backlog` on governance) and existing consumers
(including `_build_brand_book`, the design adapter, and the session API)
read them through `TeamOutput`'s nested phase outputs. Removing them would
break those consumers. The specialist agents are therefore invoked as part
of the phase graphs on every run so every `TeamOutput` remains structurally
complete for the phases that executed.

### 3. Why three API styles coexist

Different entry points serve different user states:

- **Synchronous `POST /run`** (`api/main.py:685`) — used when the caller
  already has a full `RunBrandingTeamRequest`. One orchestrator call,
  one `TeamOutput` back. Fastest path for integration tests and
  programmatic callers.
- **Session API `POST /sessions`** (`api/main.py:716`) — used when the
  caller has a partial brief. The orchestrator runs in unapproved mode,
  the mission is analyzed for missing fields, and a question feed is
  published (`_build_open_questions`, `api/main.py:377-405`). Each
  answered question mutates the mission and reruns the orchestrator
  (`api/main.py:758-784`).
- **Conversation API `POST /conversations`** (`api/main.py:813`) — used
  when the caller has no structured brief at all. The
  `BrandingAssistantAgent` runs a two-stage LLM flow — a conversation agent
  for the reply, a separate `structured_output=MissionUpdate` extraction
  agent for mission fields (`assistant/agent.py:respond()`) — and reruns
  the orchestrator whenever the mission becomes complete
  (`api/main.py:360-367`). A brand is auto-created the first time a
  company name shows up (`api/main.py:846-865`).

All three produce the same `TeamOutput`, so downstream UIs and integrations
can render the result without caring which path produced it.

### 4. Why every store is Postgres-backed

Every store (`store.py`, `api/state.py`, `assistant/store.py`) persists
through `shared.postgres.get_conn`, so all worker processes share the same
Postgres-backed state without any file-based coordination.

`postgres/__init__.py` declares the full schema
(`branding_clients`, `branding_brands`, `branding_sessions`,
`branding_conversations`, `branding_conv_messages`) registered via
`shared.postgres.register_team_schemas` at FastAPI startup, via the
`postgres_schema` argument to `create_team_app()` (`api/main.py:132-139`).
Store SQL suites (`tests/test_store.py`, `tests/test_conversation_store.py`,
`tests/test_session_store.py`) run against live Postgres via
`shared.postgres.testing.real_postgres_schema`; higher-level suites use
in-memory store doubles from `tests/_memory_stores.py`.

### 5. Why market research and design are adapters, not imports

The branding team calls the Market Research team through HTTP against
`/api/market-research/market-research/run` (`adapters/market_research.py:24`),
not through a direct Python import. This keeps team boundaries crisp: the
branding team can ship, test, and be deployed without requiring the Market
Research team's Python dependencies to resolve, and failures are isolated
to a `RuntimeError` that the orchestrator silently tolerates
(`orchestrator.py:273-280`). The design adapter is a deliberate stub until
a design service contract is defined (`adapters/design_assets.py:26-37`).

### 6. Why Temporal is optional

Most branding runs complete in seconds, so the default execution model is
a normal in-process Python call. When `TEMPORAL_ADDRESS` is set,
`temporal/__init__.py:39-40` registers `BrandingWorkflow` with
`shared.temporal.start_team_worker` on the `"branding-queue"` task queue
with a 2-hour `start_to_close_timeout`. This provides durable execution
for long-running brand builds without making Temporal a hard dependency
for the common case.

## Unified API mount

The branding team is mounted under `/api/branding` with a 120-second
timeout and the `content` cell tag. The full config entry lives at
`backend/unified_api/config.py:88-95`:

```python
"branding": TeamConfig(
    name="Branding",
    prefix="/api/branding",
    description="Brand strategy, moodboards, design and writing standards",
    tags=["branding", "design"],
    cell="content",
    timeout_seconds=120.0,
),
```

## Observability

The FastAPI app initializes OpenTelemetry with
`init_otel(service_name="branding-team", team_key="branding")` and
instruments itself via `instrument_fastapi_app(app, team_key="branding")`
on import (`api/main.py:37-63`). All routes are traced with the
`branding` team key for cross-service correlation.

## Cross-references

| Claim | Source |
|---|---|
| 5-phase pipeline order | `orchestrator.py:52-58` |
| Phase gate builder | `orchestrator.py:69-81` |
| Run loop with dependency guards | `orchestrator.py:184-234` |
| Status determination | `orchestrator.py:302-322` |
| Brand book builder | `orchestrator.py:380-474` |
| Specialist agents instantiated on run | `orchestrator.py:141-145` |
| `TeamOutput` shape incl. specialist fields | `models.py:471-498` |
| `Client` / `Brand` models | `models.py:514-528` |
| Postgres schema declaration (clients, brands) | `postgres/__init__.py:18-29` |
| `shared.postgres.get_conn` usage in the store | `store.py:30,103` |
| Postgres registration at startup | `api/main.py:132-139` |
| Market research HTTP call | `adapters/market_research.py:17-50` |
| Market research result mapping | `adapters/market_research.py:53-74` |
| Design adapter stub | `adapters/design_assets.py:16-37` |
| Temporal worker registration | `temporal/__init__.py:37-40` |
| Unified API mount config | `backend/unified_api/config.py:88-95` |
| OpenTelemetry instrumentation | `api/main.py:37-63` |
