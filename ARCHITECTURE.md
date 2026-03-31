# Architecture

This document describes the architecture of the Software Engineering Team — a multi-agent system that takes a product specification and produces a fully implemented, tested, and documented codebase. The diagrams below cover system entry points, the end-to-end pipeline, every agent and its role, execution mechanics, and sub-team orchestration.

## Table of Contents

- [1. System Context and Entry Points](#1-system-context-and-entry-points)
  - [Temporal (durable execution)](#temporal-durable-execution)
- [2. End-to-End Pipeline](#2-end-to-end-pipeline)
- [3. Agent Registry and Roles](#3-agent-registry-and-roles)
- [4. Task Execution Model](#4-task-execution-model)
- [5. Backend Worker Workflow](#5-backend-worker-workflow)
- [5b. Backend-Code-V2 Team Workflow](#5b-backend-code-v2-team-workflow)
- [5c. Frontend-Code-V2 Team Workflow](#5c-frontend-code-v2-team-workflow)
- [6. Frontend Worker Workflow](#6-frontend-worker-workflow)
- [7. Frontend Team Full Pipeline](#7-frontend-team-full-pipeline)
- [8. DevOps Team Pipeline](#8-devops-team-pipeline)
- [9. Planning Loop](#9-planning-loop)
- [10. Plan Folder and Artifacts](#10-plan-folder-and-artifacts)
- [11. Repo Layout](#11-repo-layout)

---

## 1. System Context and Entry Points

Users invoke the system through either a FastAPI HTTP API or a CLI script. When Temporal is not configured, the API starts `run_orchestrator` in a background thread; when `TEMPORAL_ADDRESS` is set, it starts a Temporal workflow that runs the same logic via activities. The orchestrator reads `initial_spec.md` from the provided work path and writes planning artifacts to `plan/`, backend code to `backend/`, and frontend code to `frontend/`.

```mermaid
flowchart LR
    User["User / Client"]

    subgraph entry [Entry Points]
        API["FastAPI Server\napi/main.py"]
        CLI["CLI Script\nrun_team.py"]
    end

    Orch["run_orchestrator\n(background thread)"]
    JobStore["Job Store\n(status, progress, results)"]

    subgraph workPath ["Work Path (repo_path/)"]
        Spec["initial_spec.md\n(input)"]
        PlanDir["plan/\n(planning artifacts)"]
        BackendDir["backend/\n(git repo)"]
        FrontendDir["frontend/\n(git repo)"]
    end

    User -->|"HTTP"| API
    User -->|"CLI"| CLI
    API -->|"POST /run-team"| Orch
    CLI --> Orch
    Orch -->|"reads"| Spec
    Orch -->|"updates"| JobStore
    Orch -->|"writes"| PlanDir
    Orch -->|"writes"| BackendDir
    Orch -->|"writes"| FrontendDir
```

The API also exposes `GET /run-team/{job_id}` for polling job status, `POST /run-team/{job_id}/retry-failed` for retrying failed tasks, and clarification endpoints for interactive spec refinement.

### Temporal (durable execution)

When `TEMPORAL_ADDRESS` is set (e.g. in Docker), the SE team uses **Temporal** instead of background threads:

- **Workflows**: `RunTeamWorkflow`, `RetryFailedWorkflow`, `StandaloneJobWorkflow` (for frontend-code-v2, backend-code-v2, planning-v2, product-analysis).
- **Activities**: Each workflow runs activities that call the same logic as the former thread targets (`run_orchestrator`, `run_failed_tasks`, and the standalone runners). Activities update the **job store** so the API and UI continue to poll status from the store.
- **Worker**: A Temporal worker runs in-process (started from the unified API lifespan or when the SE API runs standalone), using task queue `software-engineering` (override with `TEMPORAL_TASK_QUEUE`).
- **Resilience**: Progress is durable in Temporal; after a server restart, the worker reconnects and in-progress workflows continue. **Resume** is allowed for `failed` jobs as well as `pending`, `running`, and `agent_crash`, so jobs marked failed (e.g. by the stale-heartbeat monitor) can be resumed via `POST /run-team/{job_id}/resume`.
- **Env**: `TEMPORAL_ADDRESS` (required for Temporal), optional `TEMPORAL_NAMESPACE` (default `default`), `TEMPORAL_TASK_QUEUE` (default `software-engineering`). When `TEMPORAL_ADDRESS` is unset, the API falls back to thread-based execution for local development.

---

## 2. End-to-End Pipeline

A single run goes through four major phases: Discovery, Design, Execution, and Integration. The orchestrator (`orchestrator.py`) drives this pipeline sequentially. Planning is handled by **planning_v2_team** (6-phase workflow); its output is adapted by **planning_v2_adapter** for Tech Lead and Architecture Expert.

```mermaid
flowchart TB
    subgraph discovery ["1 - Discovery"]
        LoadSpec["Load Spec\n(initial_spec.md or override)"]
        ParseSpec["Parse Spec with LLM\n(ProductRequirements)"]
        PlanningV2["Planning (v2)\n6-phase workflow"]
        Adapter["planning_v2_adapter\n(ProductRequirements, project_overview)"]
        LoadSpec --> ParseSpec --> PlanningV2 --> Adapter
    end

    subgraph design ["2 - Design"]
        PlanLoop["Tech Lead + Architecture Expert"]
        MasterPlan["Planning Consolidation\n(master_plan.md)"]
        PlanLoop --> MasterPlan
    end

    subgraph execution ["3 - Execution"]
        PrefixTasks["Prefix Tasks\n(git_setup, devops)\nsequential"]
        BackendWorker["Backend Worker Thread"]
        FrontendWorker["Frontend Worker Thread"]
        PrefixTasks --> BackendWorker
        PrefixTasks --> FrontendWorker
    end

    subgraph integrationPhase ["4 - Integration and Release"]
        IntAgent["Integration Agent\n(backend-frontend contract check)"]
        DevOpsTrigger["DevOps Trigger\n(containerize repos)"]
        FinalSecurity["Final Security Pass\n(full codebase)"]
        DocUpdate["Documentation Update"]
        IntAgent --> DevOpsTrigger --> FinalSecurity --> DocUpdate
    end

    discovery --> design --> execution --> integrationPhase
```

Each phase produces artifacts that feed the next. Planning artifacts are written to `plan/`. Backend and frontend workers operate on separate git repositories under the work path.

---

## 3. Agent Registry and Roles

The orchestrator instantiates agents via `_get_agents()`. The main pipeline uses **planning_v2_team** (PlanningV2TeamLead) for discovery/planning, with **planning_v2_adapter** mapping its result to ProductRequirements and project_overview for Tech Lead and Architecture. Legacy planning_team agents (Spec Intake, Project Planning, domain planning) are not in the main flow; clarification sessions still use Spec Intake.

```mermaid
flowchart TB
    Orch["Orchestrator"]

    subgraph planning [Planning - main pipeline]
        planningV2["Planning (v2)\n6-phase workflow"]
        adapter["planning_v2_adapter"]
        archExpert["Architecture Expert"]
        techLead["Tech Lead"]
    end

    subgraph setupGroup [Setup]
        gitSetup["Git Setup"]
    end

    subgraph execGroup [Execution]
        backendAgent["Backend Expert"]
        backendV2Team["Backend-Code-V2 Team\n(standalone 5-phase)"]
        frontendAgent["Frontend Expert"]
        devopsTeam["DevOps Team Lead"]
    end

    subgraph qualityGroup [Quality Gates]
        codeReview["Code Review"]
        qaAgent["QA Expert"]
        secAgent["Cybersecurity Expert"]
        a11yAgent["Accessibility Expert"]
        acceptV["Acceptance Verifier"]
        dbcAgent["DbC Comments"]
        lintAgent["Linting Tool"]
    end

    subgraph postGroup ["Integration / Release"]
        intAgent["Integration Agent"]
        docAgent["Documentation Agent"]
    end

    subgraph supportGroup [Recovery]
        repairAgent["Repair Agent"]
        buildFixAgent["Build Fix Specialist"]
    end

    Orch --> planning
    Orch --> domain
    Orch --> setupGroup
    Orch --> execGroup
    execGroup -.->|"per-task gates"| qualityGroup
    Orch --> postGroup
    execGroup -.->|"on crash"| supportGroup
```

Quality gate agents (code review, QA, security, accessibility, acceptance verifier, DbC, linting) are not task assignees — they are invoked inside backend and frontend workflows for every task. The repair agent and build fix specialist handle agent crashes and persistent build failures respectively.

---

## 4. Task Execution Model

After planning, the Tech Lead produces a `TaskAssignment` with an ordered list of tasks. The orchestrator partitions tasks by assignee into three queues, then runs them in the sequence shown below. Tasks with dependency edges (`blocks`/`blocked_by`) are scheduled so blocked tasks wait until their prerequisites complete.

```mermaid
flowchart TB
    ExecOrder["TaskAssignment.execution_order"]

    subgraph partition [Partition by Assignee]
        PrefixQ["Prefix Queue\n(git_setup + devops tasks)"]
        BackendQ["Backend Queue"]
        BV2Q["Backend-Code-V2 Queue"]
        FrontendQ["Frontend Queue"]
    end

    ExecOrder -->|"split by type/assignee"| partition

    PrefixQ -->|"sequential, one at a time"| PrefixRun["Run Prefix Tasks\n(Git Setup Agent, DevOps Team)"]

    PrefixRun --> parallelBlock

    subgraph parallelBlock ["Parallel Worker Threads"]
        BThread["Backend Worker\npops from Backend Queue\n1 task at a time"]
        BV2Thread["Backend-Code-V2 Worker\npops from BV2 Queue\n1 task at a time"]
        FThread["Frontend Worker\npops from Frontend Queue\n1 task at a time"]
    end

    BThread --> BWorkflow["BackendExpertAgent\n.run_workflow()"]
    BV2Thread --> BV2Workflow["BackendCodeV2TeamLead\n.run_workflow()"]
    FThread --> FWorkflow["FrontendExpertAgent\n.run_workflow()"]

    BWorkflow --> TaskDone["Task Completed / Failed"]
    BV2Workflow --> TaskDone
    FWorkflow --> TaskDone
```

Backend and frontend workers run as concurrent threads (`threading.Thread`). Each worker processes one task at a time from its queue. On task failure, the orchestrator may attempt repair (agent crash) or contract repair (incomplete task metadata) before re-queuing.

---

## 5. Backend Worker Workflow

Each backend task follows this pipeline inside `BackendExpertAgent.run_workflow`. The orchestrator creates a feature branch, runs the workflow, and merges to `development` on success.

```mermaid
flowchart TB
    Branch["Create Feature Branch\n(feature/{task_id})"]
    TaskPlan["Per-Task Planning\n(review codebase, produce plan)"]
    CodeGen["Generate Code\n(LLM, with clarification loop)"]
    WriteCode["Write Files to Repo"]
    Lint["Lint Verification\n(Linting Tool Agent)"]
    Build["Build Verification\n(pytest)"]
    CR["Code Review"]
    AV["Acceptance Verifier"]
    Sec["Security Review"]
    QA["QA Review\n(bugs + tests + README)"]
    Dbc["DbC Comments\n(pre/postconditions)"]
    TLReview["Tech Lead Review"]
    Doc["Documentation Update"]
    Merge["Merge to development"]

    Branch --> TaskPlan --> CodeGen --> WriteCode --> Lint --> Build
    Build --> CR --> AV --> Sec --> QA
    QA --> Dbc --> TLReview --> Doc --> Merge

    Build -->|"failure"| BuildFix["Build Fix Specialist\n(targeted fix)"]
    BuildFix -->|"retry"| Build

    CR -->|"issues found"| CodeGen
    Sec -->|"issues found"| CodeGen
    QA -->|"issues found"| CodeGen
```

On agent crash, the Repair Agent analyzes the traceback and applies fixes. If the task contract is incomplete (missing required fields like goal, scope, constraints), the orchestrator invokes contract repair via the planning agents and `tech_lead.refine_task`, then re-queues the task.

---

## 5b. Backend-Code-V2 Team Workflow

The **backend-code-v2** agent team is a standalone, experimental backend development team that operates independently from `BackendExpertAgent`. It uses a **three-layer architecture**: a Backend Tech Lead Agent runs Setup then delegates to a Backend Development Agent, which runs the 5-phase cycle and consults **tool agents in every phase**. No code from `backend_agent/` is imported or reused.

```mermaid
flowchart TB
    subgraph techLead ["Backend Tech Lead Agent"]
        Setup["Setup\n(git init, README, dev branch)"]
        TLPlanning["Planning"]
        TLExecution["Execution"]
        TLReview["Review"]
        TLProblemSolving["Problem-solving"]
        TLDeliver["Deliver"]
        Setup --> TLPlanning --> TLExecution --> TLReview --> TLProblemSolving --> TLDeliver
    end

    subgraph devAgent ["Backend Development Agent"]
        DAPlanning["Planning\n(microtask decomposition)"]
        DAExecution["Execution\n(delegate to tool agents)"]
        DAReview["Review\n(build, lint, coverage, UAT)"]
        DAProblemSolving["Problem-solving\n(root-cause, fix loop)"]
        DADeliver["Deliver\n(commit to branch)"]
        DAPlanning --> DAExecution --> DAReview --> DAProblemSolving --> DADeliver
    end

    subgraph toolGrid ["Tool Agents (participate in all phases)"]
        direction LR
        DataEng["DataEng"]
        Auth["Auth"]
        ApiOA["API/OpenAPI"]
        CICD["CI/CD"]
        Container["Container"]
        GitBranch["Git branch mgmt"]
        BuildSpec["Build Specialist"]
    end

    techLead -->|"delegates"| devAgent
    devAgent -->|"each phase consults"| toolGrid
```

- **Layer 1 — Backend Tech Lead Agent**: Runs the **Setup** phase (git init if needed, README with project title, rename master→main, create `development` branch), then delegates the 5-phase development cycle to the Backend Development Agent.
- **Layer 2 — Backend Development Agent**: Owns Planning (microtask decomposition, language detection), Execution (tool agents + LLM fallback), Review (build, lint, QA, security, code review), Problem-solving (fix loop), and Deliver (feature branch, commit, merge to `development`). The review/fix loop runs up to 5 iterations.
- **Layer 3 — Tool agents**: Data Engineering, API/OpenAPI, Auth, CI/CD, Containerization, **Git branch management**, and **Build Specialist** agents each implement `plan()`, `execute()`, `review()`, `problem_solve()`, and `deliver()`, so they participate in every phase. The **Git branch management** agent creates a feature branch off `development` at the start of Execution, commits changes after each iteration ("commit along the way"), and in Deliver merges the feature branch back into `development`. The **Build Specialist** (stub) is intended to assist when the project doesn't build; it can be wired to the existing build verifier or a dedicated build-fix flow.

The team supports both Python and Java (auto-detected). Quality gate agents (QA, Security, Code Review) are passed in by the main orchestrator and invoked during Review.

**API endpoints:**
- `POST /backend-code-v2/run` — Submit a task and repo path; starts Setup then the 5-phase workflow in a background thread.
- `GET /backend-code-v2/status/{job_id}` — Returns current phase (including `setup`), completed phases, progress percentage, and microtask status.

---

## 5c. Frontend-Code-V2 Team Workflow

The **frontend-code-v2** agent team is a standalone, experimental frontend development team that does **not** import or reuse any code from `frontend_team/` or `feature_agent/`. It mirrors the backend-code-v2 **three-layer architecture**: a Frontend Tech Lead Agent runs Setup then delegates to a Frontend Development Agent, which runs the 5-phase cycle and consults **tool agents in every phase**.

- **Layer 1 — Frontend Tech Lead Agent**: Runs **Setup** (git init if needed, README, development branch), then delegates the 5-phase cycle to the Frontend Development Agent.
- **Layer 2 — Frontend Development Agent**: Planning (microtask decomposition; stack inferred as Angular/React/TypeScript/JavaScript), Execution (tool agents + LLM fallback), Review (build, lint, QA, security, code review), Problem-solving (fix loop), Deliver (feature branch, commit, merge to `development`). Review/fix loop runs up to 5 iterations.
- **Layer 3 — Tool agents**: State Management, Auth, API/OpenAPI, CI/CD, Containerization, Documentation, Testing/QA, Security, **Git branch management**, UI Design, Branding/Theme, UX/Usability, Accessibility, **Build Specialist**, Linter. Each participates in plan, execute, review, problem_solve, and deliver. Git branch management creates a feature branch off `development`, commits along the way, and merges in Deliver.

**API endpoints:**
- `POST /frontend-code-v2/run` — Submit a task and repo path; starts Setup then the 6-phase workflow (setup + 5-phase cycle) in a background thread.
- `GET /frontend-code-v2/status/{job_id}` — Returns current phase (including `setup`), completed phases, progress percentage, and microtask status.

The Software Engineering UI dashboard includes a **Frontend Developer (v2)** tab with a run form and job-status panel; the main orchestrator supports assignee **frontend-code-v2** (task_parsing and a dedicated frontend_code_v2_queue + worker).

---

## 6. Frontend Worker Workflow

The frontend per-task workflow is structurally similar to backend, with the addition of an accessibility gate and `ng build` for build verification.

```mermaid
flowchart TB
    Branch["Create Feature Branch"]
    InstallDeps["Install Frontend Dependencies\n(npm install)"]
    TaskPlan["Per-Task Planning"]
    CodeGen["Generate Code\n(FrontendExpertAgent)"]
    WriteCode["Write Files to Repo"]
    NpmPkgs["Install npm Packages\n(if agent requested)"]
    Lint["Lint Verification"]
    Build["Build Verification\n(ng build)"]
    CR["Code Review"]
    AV["Acceptance Verifier"]
    Sec["Security Review"]
    QA["QA Review"]
    A11y["Accessibility Review\n(WCAG 2.2)"]
    Dbc["DbC Comments"]
    TLReview["Tech Lead Review"]
    Doc["Documentation Update"]
    Merge["Merge to development"]

    Branch --> InstallDeps --> TaskPlan --> CodeGen --> WriteCode --> NpmPkgs
    NpmPkgs --> Lint --> Build
    Build --> CR --> AV --> Sec --> QA
    QA --> A11y --> Dbc --> TLReview --> Doc --> Merge

    Build -->|"failure"| BuildFix["Build Fix Specialist"]
    BuildFix -->|"retry"| Build

    CR -->|"issues found"| CodeGen
    Sec -->|"issues found"| CodeGen
    QA -->|"issues found"| CodeGen
    A11y -->|"issues found"| CodeGen
```

The same crash recovery (Repair Agent) and contract repair mechanisms apply as in the backend workflow.

---

## 7. Frontend Team Full Pipeline

The `FrontendOrchestratorAgent` provides an extended pipeline that wraps `FrontendExpertAgent` with a full design phase. This pipeline runs UX, UI, and design system agents before implementation. The main orchestrator currently uses `FrontendExpertAgent` directly; this diagram documents the alternative full-team pipeline available via `FrontendOrchestratorAgent`.

```mermaid
flowchart TB
    subgraph designPhase ["Design Phase (skipped for lightweight tasks)"]
        UXDesigner["UX Designer"]
        UIDesigner["UI Designer"]
        DesignSys["Design System Agent"]
        UXDesigner --> UIDesigner --> DesignSys
    end

    subgraph archPhase [Architecture Phase]
        FEArchitect["Frontend Architect"]
    end

    subgraph implPhase [Implementation Phase]
        FeatureAgent["Feature Agent\n(FrontendExpertAgent)"]
        QualityLoop["Quality Gate Loop\n(lint, build, code review, QA,\naccessibility, security,\nacceptance verifier, DbC)"]
        FeatureAgent --> QualityLoop
    end

    subgraph polishPhase [Polish Phase]
        UXEngineer["UX Engineer"]
        PerfEngineer["Performance Engineer"]
        UXEngineer --> PerfEngineer
    end

    subgraph releasePhase [Release Phase]
        BuildRelease["Build / Release Agent"]
        MergeBranch["Merge to development"]
        BuildRelease --> MergeBranch
    end

    designPhase --> archPhase --> implPhase --> polishPhase --> releasePhase
```

The design phase produces UX wireframes, UI specifications, and design system tokens that feed into the Frontend Architect's component structure, which in turn enriches the implementation context for the Feature Agent. Lightweight tasks (fixes, patches, small updates) skip the design phase entirely.

---

## 8. DevOps Team Pipeline

The `DevOpsTeamLeadAgent` orchestrates a contract-first, multi-agent DevOps pipeline with hard gates. It replaces the legacy monolithic `devops_agent/` with role-separated agents and independent review gates.

```mermaid
flowchart TB
    subgraph phase1 ["Phase 1: Intake"]
        EnvPolicy["Environment Policy Check\n(dev / staging / production)"]
        TaskClarifier["Task Clarifier\n(validate spec completeness)"]
        EnvPolicy --> TaskClarifier
    end

    subgraph phase2 ["Phase 2: Change Design"]
        RepoNav["Repo Navigator\n(discover IaC/pipeline paths)"]
        IaCAgent["Infrastructure as Code Agent"]
        CICDAgent["CI/CD Pipeline Agent"]
        DeployAgent["Deployment Strategy Agent"]
        RepoNav --> IaCAgent
        RepoNav --> CICDAgent
        RepoNav --> DeployAgent
    end

    subgraph phase3 ["Phase 3: Write Artifacts"]
        WriteArtifacts["Write aggregated artifacts\nto repository"]
    end

    subgraph phase4 ["Phase 4: Validation and Review"]
        subgraph toolVal [Tool Validation]
            IaCVal["IaC Validation"]
            PolicyCheck["Policy as Code\n(checkov / tfsec)"]
            CICDLint["CI/CD Lint"]
            DryRun["Deployment Dry Run\n(helm lint / template)"]
        end

        subgraph execVerify [Execution Verification]
            TfExec["Terraform\n(if .tf files)"]
            CdkExec["CDK\n(if cdk.json)"]
            ComposeExec["Docker Compose\n(if compose.yml)"]
            HelmExec["Helm\n(if Chart.yaml)"]
        end

        subgraph debugLoop [Debug-Patch Loop]
            InfraDebug["Infra Debug Agent\n(analyze failure)"]
            InfraPatch["Infra Patch Agent\n(apply fix)"]
            InfraDebug -->|"fixable"| InfraPatch
            InfraPatch -->|"re-validate"| execVerify
        end

        subgraph reviewGates [Independent Reviews]
            DevSecOps["DevSecOps Review"]
            ChangeReview["Change Review"]
            TestVal["Test Validation\n(gate aggregation)"]
        end

        toolVal --> execVerify --> debugLoop --> reviewGates
    end

    subgraph phase5 ["Phase 5: Completion"]
        DocRunbook["Documentation and Runbook Agent"]
        CompletionPkg["Completion Package\n(acceptance trace, quality gates,\nrelease readiness, git ops, handoff)"]
        DocRunbook --> CompletionPkg
    end

    phase1 -->|"approved"| phase2 --> phase3 --> phase4
    phase4 -->|"all gates pass"| phase5
    phase4 -->|"gate failure"| Blocked["Return: blocked"]
```

Hard gates that must pass: `iac_validate`, `iac_validate_fmt`, `policy_checks`, `pipeline_lint`, `pipeline_gate_check`, `deployment_dry_run`, `security_review`, `change_review`. The environment policy matrix enforces stricter requirements for production (approval required, rollback test required, high policy strictness) versus dev (auto-deploy allowed, no approval, low strictness).

---

## 9. Planning Loop

The Tech Lead and Architecture Expert run after **Planning (v2)** and **planning_v2_adapter** produce ProductRequirements and project_overview. An optional planning cache short-circuits when the spec, architecture, and project overview are unchanged from a previous run.

```mermaid
flowchart TB
    StartPlan["Start Planning\n(after Planning v2 + adapter)"]
    TechLeadRun["Tech Lead\nGenerate task assignment"]
    ArchRun["Architecture Expert\nDesign architecture"]
    CacheHit{"Planning\ncache hit?"}
    AlignCheck{"Tasks and architecture\naligned?"}
    ConformCheck{"Conforms to\ninitial_spec?"}
    SaveCache["Save to\nplanning cache"]
    ProceedExec["Proceed to\nExecution"]

    StartPlan --> TechLeadRun --> ArchRun --> CacheHit
    CacheHit -->|"yes"| ProceedExec
    CacheHit -->|"no"| AlignCheck
    AlignCheck -->|"no: alignment_feedback"| TechLeadRun
    AlignCheck -->|"yes"| ConformCheck
    ConformCheck -->|"no: conformance_issues"| TechLeadRun
    ConformCheck -->|"yes"| SaveCache --> ProceedExec
```

The alignment inner loop runs up to `SW_MAX_ALIGNMENT_ITERATIONS` (default 20) and the conformance outer loop runs up to `SW_MAX_CONFORMANCE_RETRIES` (default 20). Early exit thresholds allow proceeding when only minor non-critical issues remain. During alignment re-runs, both the Tech Lead and Architecture Expert are re-invoked with the feedback from the previous iteration.

---

## 10. Plan Folder and Artifacts

Planning (v2) writes to `planning_v2/` under the repo path. The rest of planning outputs are written to `plan/` at the work path root.

```mermaid
flowchart LR
    PlanDir["plan/"]
    P2Dir["planning_v2/"]

    P2Dir --> P2Art["Planning (v2)\nplanning_artifacts.md"]

    PlanDir --> ArchArt["Architecture\narchitecture.md"]

    PlanDir --> ConsolArt["Consolidation\ntech_lead.md\nmaster_plan.md"]

    PlanDir --> PerTaskArt["Per-Task Plans\nbackend_task_ID.md\nfrontend_task_ID.md"]
```

The `master_plan.md` consolidation includes a risk register and ship checklist.

---

## 11. Repo Layout

The repository contains two independent agent systems. The software engineering team is the primary system documented above; a separate blogging agent system exists under `agents/blogging/`.

```mermaid
flowchart TB
    Root["strands-agents/"]

    Root --> SWTeam["agents/software_engineering_team/"]
    Root --> BlogTeam["agents/blogging/"]

    SWTeam --> swOrch["orchestrator.py"]
    SWTeam --> swAPI["api/"]
    SWTeam --> swCLI["agent_implementations/"]
    SWTeam --> swPlanningV2["planning_v2_team/\n(6-phase workflow)\nplanning_v2_adapter"]
    SWTeam --> swPlanning["planning_team/\n(legacy; clarification)"]
    SWTeam --> swBackend["backend_agent/"]
    SWTeam --> swBackendV2["backend_code_v2_team/\n(standalone 5-phase team,\n3 tool agents)"]
    SWTeam --> swFrontend["frontend_team/\n(12 agents)"]
    SWTeam --> swDevops["devops_team/\n(9 agents + 5 tool agents)"]
    SWTeam --> swQuality["quality_gates/"]
    SWTeam --> swIntegration["integration_team/"]
    SWTeam --> swShared["shared/\n(LLM, models, git, utils)"]
    SWTeam --> swTests["tests/"]

    BlogTeam --> blogAgents["research, writer, review,\ncopy_editor, publication"]
```

Each agent directory follows a consistent structure: `agent.py` (core logic), `models.py` (Pydantic input/output contracts), and `prompts.py` (LLM prompt templates). Shared utilities (LLM client, git operations, repo I/O, logging) live in `shared/`.
