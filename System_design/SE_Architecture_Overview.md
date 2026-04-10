# Software Engineering Team — Architecture Overview

## 1. System Context

The Software Engineering Team is one of 20 agent teams in the Khala platform. It receives project specifications from users, orchestrates multi-phase software development, and produces production-ready code repositories.

```mermaid
flowchart TD
    User["User / Client"]
    UI["Angular 19 Frontend\n(port 4200)"]
    UAPI["Unified API\nFastAPI reverse proxy\n(port 8080)"]
    SE["SE Team API\nFastAPI v0.3.0"]
    TEMP["Temporal Server\n(optional)"]
    LLM["LLM Service\nOllama / Claude"]
    JOB["Job Service\nHTTP-backed persistence"]
    PG["PostgreSQL"]
    FS["File System\nwork_path/"]

    User --> UI
    UI --> UAPI
    UAPI -->|"/api/software-engineering/*"| SE
    SE --> TEMP
    SE --> LLM
    SE --> JOB
    SE --> PG
    SE --> FS
```

## 2. Container Diagram

```mermaid
flowchart TD
    subgraph UnifiedAPI["Unified API (port 8080)"]
        GW["Security Gateway\nMiddleware"]
        PROXY["Team Proxy\nCircuit Breaker\nfailure_threshold=5\nrecovery=30s"]
        HC["Health Checks\nInterval: 30s"]
        PROM["Prometheus\n/metrics"]
    end

    subgraph SETeam["SE Team API"]
        EP1["POST /run-team"]
        EP2["GET /run-team/job_id"]
        EP3["POST /run-team/job_id/answers"]
        EP4["POST /run-team/job_id/cancel"]
        EP5["POST /run-team/job_id/retry-failed"]
        EP6["POST /run-team/job_id/resume"]
        EP7["POST /run-team/job_id/restart"]
        STALE["Stale Job Monitor\nDaemon thread, 30s poll"]
    end

    subgraph Orchestrator["Orchestrator"]
        RO["run_orchestrator()\n3476 lines"]
        RFT["run_failed_tasks()\nRetry failed tasks"]
    end

    subgraph TemporalWorker["Temporal Worker"]
        W1["RunTeamWorkflow\nTimeout: 48h"]
        W2["RetryFailedWorkflow\nTimeout: 24h"]
        W3["RunTeamWorkflowV2\nMulti-phase"]
        W4["StandaloneJobWorkflow\nTimeout: 12h"]
    end

    subgraph LLMSvc["LLM Service"]
        FAC["Factory\nget_client(agent_key)"]
        SEM["Concurrency Semaphore\nDefault: 4"]
        RETRY["Retry Policy\n6 attempts\n2-120s backoff"]
    end

    subgraph JobStore["Job Store"]
        JSC["JobServiceClient\nHTTP-backed"]
        HB["Heartbeat Thread\nInterval: 120s"]
        STL["Stale Detection\nThreshold: 1800s"]
    end

    GW --> PROXY
    PROXY --> SETeam
    EP1 --> Orchestrator
    Orchestrator --> TemporalWorker
    Orchestrator --> LLMSvc
    Orchestrator --> JobStore
```

## 3. Agent Inventory

```mermaid
flowchart LR
    subgraph OrchestratorAgents["Orchestrator-Level Agents"]
        PRA["ProductRequirements\nAnalysisAgent"]
        TL["TechLeadAgent"]
        ARCH["ArchitectureExpert"]
        INT["IntegrationAgent"]
        SEC["CybersecurityExpert"]
        DOC["DocumentationAgent"]
        REP["RepairExpertAgent"]
    end

    subgraph CodingTeam["Coding Team (Primary Path)"]
        CTL["TechLeadAgent\n(Coordinator)"]
        SWE["SeniorSWEAgent(s)\n(Workers)"]
        TGS["TaskGraphService"]
    end

    subgraph BackendV2["Backend-Code-V2 Team"]
        BTL["BackendCodeV2TeamLead"]
        BDA["BackendDevelopmentAgent"]
        BTA["10 Tool Agents"]
    end

    subgraph FrontendV2["Frontend-Code-V2 Team"]
        FTL["FrontendCodeV2TeamLead"]
        FDA["FrontendDevelopmentAgent"]
        FTA["17 Tool Agents"]
    end

    subgraph DevOps["DevOps Team"]
        DTL["DevOpsTeamLeadAgent"]
        DCA["9 Core Agents"]
        DTA["9 Tool Agents"]
    end

    subgraph ReviewAgents["Review Agents"]
        CR["CodeReviewAgent"]
        QA["QAExpertAgent"]
        AV["AcceptanceVerifierAgent"]
        BFS["BuildFixSpecialist"]
        PS["ProblemSolverAgent"]
    end

    OrchestratorAgents --> CodingTeam
    OrchestratorAgents -.->|Legacy path| BackendV2
    OrchestratorAgents -.->|Legacy path| FrontendV2
    OrchestratorAgents --> DevOps
    BackendV2 --> ReviewAgents
    FrontendV2 --> ReviewAgents
    CodingTeam --> ReviewAgents
```

### Backend-Code-V2 Tool Agents (10)

| # | Agent | Responsibility |
|---|-------|---------------|
| 1 | DataEngineering | Database models, schemas, migrations, ORM |
| 2 | API/OpenAPI | REST endpoints, OpenAPI specs |
| 3 | Auth | Authentication, JWT/OAuth, authorization |
| 4 | CI/CD | Pipeline config (GitHub Actions, etc.) |
| 5 | Containerization | Dockerfile, docker-compose |
| 6 | Git | Branch creation, commits, merges |
| 7 | Build Specialist | Build scripts, dependency management |
| 8 | Testing/QA | Unit tests, integration tests |
| 9 | Security | Security scanning, vulnerability checks |
| 10 | Documentation | Docstrings, README, API docs |

### Frontend-Code-V2 Tool Agents (17)

| # | Agent | Responsibility |
|---|-------|---------------|
| 1 | StateManagement | Redux/Vuex/Context setup |
| 2 | API/OpenAPI | API client generation |
| 3 | Auth | Authentication UI/integration |
| 4 | Architecture | Frontend architecture patterns |
| 5 | UIDesign | UI component design |
| 6 | BrandingTheme | Theming and branding |
| 7 | UXUsability | UX review and improvements |
| 8 | Accessibility | WCAG 2.2 compliance |
| 9 | Testing/QA | Unit, E2E, component tests |
| 10 | Security | Frontend security review |
| 11 | Performance | Performance optimization |
| 12 | Linter | ESLint/Prettier enforcement |
| 13 | Build Specialist | Build/webpack fixes |
| 14 | CI/CD | Frontend-specific CI/CD |
| 15 | Containerization | Frontend containerization |
| 16 | Git | Branch operations |
| 17 | Documentation | Component docs, Storybook |

### DevOps Team Agents (18)

**9 Core Agents:** TaskClarifier, InfrastructureAsCode, CICDPipeline, DeploymentStrategy, DevSecOpsReview, ChangeReview, TestValidation, DocumentationRunbook, InfraDebug, InfraPatch

**9 Tool Agents:** IaCValidation, PolicyAsCode, CICDLint, DeploymentDryRun, RepoNavigator, Terraform, CDK, DockerCompose, Helm

## 4. Technology Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.10+, TypeScript (Angular 19) |
| API Framework | FastAPI, Pydantic v2 |
| Workflow Engine | Temporal Python SDK (optional) |
| HTTP Client | httpx |
| Database | PostgreSQL (opt-in via `POSTGRES_HOST`) |
| LLM Provider | Ollama (qwen3.5:397b-cloud default), Claude |
| Observability | OpenTelemetry, Prometheus |
| Frontend | Angular 19, Angular Material, Vitest |
| Container | Docker, docker-compose |

## 5. Two Execution Paths

The orchestrator supports two execution paths. The **Coding Team** path is the current default (`use_coding_team = True`).

```mermaid
flowchart TD
    START["run_orchestrator()"] --> PRA["Product Requirements\nAnalysis"]
    PRA --> PV3["Planning V3\n6-phase workflow"]
    PV3 --> DECISION{use_coding_team?}

    DECISION -->|"True (default)"| CT["Coding Team\nSwarm Orchestrator"]
    CT --> DONE["COMPLETED"]

    DECISION -->|"False (legacy)"| TL["Tech Lead\nTask Assignment"]
    TL --> AE["Architecture Expert"]
    AE --> CONSOL["Planning\nConsolidation"]
    CONSOL --> PREFIX["Prefix Queue\ngit_setup, devops"]
    PREFIX --> PARALLEL["Parallel Workers"]

    subgraph PARALLEL["Parallel Execution"]
        BW["Backend-V2 Worker\n(daemon thread)"]
        FW["Frontend-V2 Worker\n(daemon thread)"]
    end

    PARALLEL --> JOIN["Thread Join"]
    JOIN --> INTEG["Integration Agent"]
    INTEG --> SECR["Security Review"]
    SECR --> DOCS["Documentation"]
    DOCS --> DEVOPS["DevOps\nContainerization"]
    DEVOPS --> DONE2["COMPLETED"]
```
