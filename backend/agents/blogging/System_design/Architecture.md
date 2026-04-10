# Blogging Team — System Architecture

This document describes the high-level architecture of the Blogging Agent Suite: how it fits within the Strands Agents platform, its internal component structure, deployment topology, shared infrastructure dependencies, and agent responsibilities.

---

## 1. System Context

The blogging team operates as one of 20 agent teams within the Strands Agents platform. It is mounted at `/api/blogging` by the Unified API and accessed through the Angular 19 frontend.

```mermaid
graph TD
    subgraph External
        User["Human Author / Browser"]
        Medium["Medium.com"]
        ArXiv["arXiv.org"]
        OllamaCloud["Ollama Cloud API"]
    end

    subgraph Strands Platform
        UI["Angular 19 Frontend<br/>(port 4200)"]
        UnifiedAPI["Unified FastAPI Server<br/>(port 8080)"]
        SecurityGW["Security Gateway"]

        subgraph Blogging Team
            BlogAPI["Blogging FastAPI App<br/>(/api/blogging)"]
            Pipeline["Pipeline Orchestrator<br/>(blog_writing_process_v2)"]
            Agents["9 Specialized Agents"]
        end

        Temporal["Temporal Server<br/>(port 7233)"]
        Postgres["PostgreSQL<br/>(port 5432)"]
        LLMService["LLM Service<br/>(Ollama / Claude)"]
    end

    User -->|HTTP / SSE| UI
    UI -->|REST API| UnifiedAPI
    UnifiedAPI --> SecurityGW --> BlogAPI
    BlogAPI -->|async jobs| Pipeline
    Pipeline --> Agents
    Agents -->|inference| LLMService
    LLMService -->|API calls| OllamaCloud
    Agents -->|web search| OllamaCloud
    Agents -->|paper search| ArXiv
    Agents -->|stats scraping| Medium
    BlogAPI -->|durable workflows| Temporal
    BlogAPI -->|job state, stories| Postgres
    Pipeline -.->|SSE events| UI
```

---

## 2. Component Architecture

The pipeline orchestrator (`run_pipeline()`) coordinates specialized agents across 5 functional groups: Research, Planning, Content Production, Quality Gates, and Finalization. The Publication Agent and Medium Stats Agent are invoked independently by the API layer, not by the pipeline orchestrator.

```mermaid
graph LR
    subgraph Orchestrator
        RP["run_pipeline()<br/><i>blog_writing_process_v2.py</i>"]
    end

    subgraph "Research & Planning"
        RA["Research Agent<br/><i>blog_research_agent/</i>"]
        PA["Planning Agent<br/><i>blog_planning_agent/</i>"]
    end

    subgraph "Content Production"
        WA["Writer Agent<br/><i>blog_writer_agent/</i>"]
        CEA["Copy Editor Agent<br/><i>blog_copy_editor_agent/</i>"]
        GWA["Ghost Writer Agent<br/><i>ghost_writer_agent/</i>"]
    end

    subgraph "Quality Gates"
        VA["Validators<br/><i>validators/runner.py</i>"]
        FCA["Fact Check Agent<br/><i>blog_fact_check_agent/</i>"]
        CMA["Compliance Agent<br/><i>blog_compliance_agent/</i>"]
    end

    subgraph "API-Level (outside pipeline)"
        PUA["Publication Agent<br/><i>blog_publication_agent/</i>"]
        MSA["Medium Stats Agent<br/><i>blog_medium_stats_agent/</i>"]
    end

    RP --> RA
    RP --> PA
    RP --> WA
    RP --> CEA
    RP --> GWA
    RP --> VA
    RP --> FCA
    RP --> CMA

    RA -->|ResearchAgentOutput| PA
    PA -->|ContentPlan| WA
    WA -->|draft| CEA
    CEA -->|FeedbackItems| WA
    GWA -->|stories| WA
    WA -->|final draft| VA
    VA -->|ValidatorReport| CMA
    FCA -->|FactCheckReport| RP
    CMA -->|ComplianceReport| RP
    RP -->|PublishingPack| RP
```

> **Note:** `run_pipeline()` produces a `PublishingPack` artifact but does **not** invoke the Publication Agent. Publication (approve/unapprove) and Medium stats collection are handled by separate API endpoints outside the pipeline.

### Pipeline Phase Mapping

| Phase | Progress | Agent(s) Involved |
|-------|----------|-------------------|
| Planning | 0–15% | Research Agent, Planning Agent |
| Draft Initial | 15–30% | Writer Agent, Ghost Writer Agent |
| Draft Review | 30–45% | Writer Agent (human feedback loop) |
| Copy Edit | 45–60% | Copy Editor Agent, Writer Agent |
| Fact Check | 60–70% | Fact Check Agent |
| Compliance | 70–82% | Compliance Agent, Validators |
| Rewrite Loop | 82–90% | Writer Agent (gate-driven rewrites) |
| Title Selection | 90–96% | Human choice (via job store) |
| Finalize | 96–100% | Pipeline (publishing pack generation) |

---

## 3. Infrastructure & Deployment

```mermaid
graph TD
    subgraph "Docker Compose Stack"
        subgraph "Application Layer"
            AgentsContainer["Agents Container<br/>port 8080<br/>Unified API + all teams"]
            UIContainer["UI Container<br/>port 4200<br/>Angular 19"]
        end

        subgraph "Infrastructure Layer"
            PG["PostgreSQL<br/>port 5432"]
            TemporalSvc["Temporal Server<br/>port 7233"]
            TemporalUI["Temporal UI<br/>port 8088"]
            Ollama["Ollama Server<br/>port 11434"]
        end

        subgraph "Shared Volume"
            Vol["agents_data<br/>/data/agents"]
        end
    end

    subgraph "External Services"
        OllamaCloudSvc["Ollama Cloud API<br/>(OLLAMA_API_KEY)"]
        MediumSvc["Medium.com<br/>(Playwright scraping)"]
        ArXivSvc["arXiv API<br/>(paper search)"]
    end

    UIContainer -->|REST / SSE| AgentsContainer
    AgentsContainer -->|SQL| PG
    AgentsContainer -->|gRPC| TemporalSvc
    AgentsContainer -->|HTTP| Ollama
    Ollama -->|API| OllamaCloudSvc
    AgentsContainer -->|Playwright| MediumSvc
    AgentsContainer -->|HTTP| ArXivSvc
    AgentsContainer --- Vol
    UIContainer --- Vol

    style Vol fill:#e6f3ff,stroke:#4a90d9
```

### Volume & Storage Layout

All blogging team artifacts persist under the shared `agents_data` Docker volume:

```
/data/agents/
  blogging_team/                    # AGENT_CACHE/blogging_team
    medium_stats_runs/              # BLOGGING_MEDIUM_STATS_ROOT
  blogging/runs/                    # BLOGGING_RUN_ARTIFACTS_ROOT (per-job)
    {job_id}/
      research_packet.md
      content_plan.json
      draft_v1.md
      ...
      publishing_pack.json
```

### Port Allocation

| Service | Port | Protocol |
|---------|------|----------|
| Unified API (all teams) | 8080 | HTTP |
| Angular UI | 4200 | HTTP |
| PostgreSQL | 5432 | TCP |
| Temporal Server | 7233 | gRPC |
| Temporal UI | 8088 | HTTP |
| Ollama | 11434 | HTTP |

---

## 4. Shared Infrastructure Dependencies

The blogging team relies on four shared infrastructure modules provided by the platform.

```mermaid
graph TD
    Blog["Blogging Team"]

    subgraph "Shared Infrastructure"
        SP["shared_postgres/<br/>Schema registry + connection pool"]
        ST["shared_temporal/<br/>Workflow orchestration + checkpoints"]
        LLM["llm_service/<br/>Unified LLM client (Ollama / Claude)"]
        EB["event_bus/<br/>SSE pub/sub per job"]
    end

    Blog -->|"register_team_schemas(SCHEMA)"| SP
    Blog -->|"BlogFullPipelineWorkflow"| ST
    Blog -->|"get_client() → complete_json()"| LLM
    Blog -->|"publish() / subscribe()"| EB

    SP -->|"get_conn() + timed_query()"| PG["PostgreSQL"]
    ST -->|"workflow.execute_activity()"| TMP["Temporal Server"]
    LLM -->|"HTTP inference"| OLL["Ollama / Claude"]
```

| Module | Blogging Team Usage |
|--------|---------------------|
| **shared_postgres** | `blogging_stories` table for story bank persistence; schema registered at FastAPI lifespan startup via `register_team_schemas(SCHEMA)` |
| **shared_temporal** | `BlogFullPipelineWorkflow` wraps the full pipeline as a single long-lived activity (12h timeout, 3 retries, 30s heartbeat); checkpoints for human-in-the-loop pauses |
| **llm_service** | `OllamaLLMClient` singleton created at API startup; agents call `complete_json()` for structured output and `complete()` for text generation; factory resolves model via env chain |
| **event_bus** | Thread-safe SSE pub/sub: pipeline publishes progress events per job; Angular UI subscribes via `/job/{job_id}/stream` endpoint for real-time updates |

---

## 5. Agent Responsibility Matrix

| Agent | Module | Role | Input Model | Output Model | Key Behavior |
|-------|--------|------|-------------|--------------|--------------|
| **Research Agent** | `blog_research_agent/` | Web + arXiv search, source ranking, document synthesis | `ResearchBriefInput` | `ResearchAgentOutput` | Stateless; parallel search queries; relevance scoring |
| **Planning Agent** | `blog_planning_agent/` | Structured content plan with refine-until-done loop | `PlanningInput` | `PlanningPhaseResult` | Self-critique via `RequirementsAnalysis`; max 5 iterations |
| **Writer Agent** | `blog_writer_agent/` | Draft generation and revision from feedback | `WriterInput` / `ReviseWriterInput` | `WriterOutput` | Dual role: initial draft + feedback-driven revision; detects uncertainty questions |
| **Copy Editor Agent** | `blog_copy_editor_agent/` | Draft quality, style, and length feedback | `CopyEditorInput` | `CopyEditorOutput` | Read-only feedback; staleness detection; escalation after 10 iterations |
| **Ghost Writer Agent** | `ghost_writer_agent/` | Personal story elicitation via multi-turn interview | `StoryGap` | `StoryElicitationResult` | Conversational interview; stories saved to bank for reuse |
| **Validators** | `validators/` | Deterministic content checks (no LLM) | Draft text | `ValidatorReport` | Banned phrases, reading level, paragraph length, required sections |
| **Fact Check Agent** | `blog_fact_check_agent/` | Claims verification and risk flagging | Draft + allowed claims | `FactCheckReport` | Flags unverified claims; identifies required disclaimers |
| **Compliance Agent** | `blog_compliance_agent/` | Brand/style enforcement with veto power | Draft + brand spec + validator report | `ComplianceReport` | FAIL status blocks publication; triggers rewrite loop |
| **Publication Agent** | `blog_publication_agent/` | Draft submission, approval, platform formatting | `SubmitDraftInput` | `ApprovalResult` | Formats for Medium, DevTo, Substack; writes to `blog_posts/`; invoked by API, not by pipeline |
| **Medium Stats Agent** | `blog_medium_stats_agent/` | Medium.com dashboard stats scraping | `MediumStatsRunConfig` | `MediumStatsReport` | Playwright automation; Google browser login integration |

---

## 6. API Surface

The blogging team mounts at `/api/blogging` with these endpoint groups:

| Category | Endpoints | Purpose |
|----------|-----------|---------|
| **Pipeline** | `POST /full-pipeline` (sync), `POST /full-pipeline-async`, `POST /research-and-review` | Trigger pipeline execution |
| **Jobs** | `GET /job/{job_id}`, `DELETE /job/{job_id}`, `POST /job/{job_id}/restart`, `POST /job/{job_id}/resume`, `POST /job/{job_id}/cancel` | Async job lifecycle management |
| **Streaming** | `GET /job/{job_id}/stream` (SSE) | Real-time progress events |
| **Collaboration** | `POST /job/{job_id}/select-title`, `POST /job/{job_id}/rate-titles`, `POST /job/{job_id}/draft-feedback`, `POST /job/{job_id}/story-response`, `POST /job/{job_id}/skip-story-gap`, `POST /job/{job_id}/answers` | Human-in-the-loop inputs |
| **Approval** | `POST /job/{job_id}/approve`, `POST /job/{job_id}/unapprove` | Final draft approval |
| **Artifacts** | `GET /job/{job_id}/artifacts`, `GET /job/{job_id}/artifacts/{name}` | Browse and download pipeline artifacts |
| **Analytics** | `POST /medium-stats`, `POST /medium-stats-async` | Medium.com stats collection |
| **Health** | `GET /health` | Brand spec configuration status |

---

## 7. Execution Modes

The blogging team supports two runtime modes:

| Mode | Trigger | Characteristics |
|------|---------|-----------------|
| **Thread Mode** (default) | `TEMPORAL_ADDRESS` not set | Agents run as Python threads; pipeline executes in-process; state in memory + job store |
| **Temporal Mode** | `TEMPORAL_ADDRESS` is set | `BlogFullPipelineWorkflow` wraps pipeline as durable activity; 12h timeout; 30s heartbeat; state survives server restarts; 3 retries with exponential backoff |

Both modes use the same `run_blog_full_pipeline_job()` entry point and produce identical artifacts.
