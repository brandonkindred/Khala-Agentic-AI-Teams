# Software Engineering Team — Flow Charts

## 1. Main Orchestrator Pipeline

```mermaid
flowchart TD
    START["run_orchestrator(job_id, repo_path)"]
    CREATE["create_job(PENDING)\nwrite workspace"]
    CANCEL1{"Cancelled?"}

    PRA["Phase 1: Product Requirements Analysis\nspec_review → communicate →\nspec_update → spec_cleanup"]
    CANCEL2{"Cancelled?"}

    PV3["Phase 2: Planning V3\nintake → discovery → requirements →\nsynthesis → document_production →\nsub_agent_provisioning"]
    ADAPT["adapt_planning_v3_result()\n→ CodingTeamPlanInput"]
    CANCEL3{"Cancelled?"}

    DECISION{"use_coding_team?\n(default: True)"}

    CT["Phase 3: Coding Team\nrun_coding_team_orchestrator()"]
    DONE["status = COMPLETED"]

    TL["Tech Lead\ngenerate TaskAssignment"]
    AE["Architecture Expert\ngenerate SystemArchitecture"]
    CONSOL["Planning Consolidation\nmaster_plan.md"]
    PREFIX["Prefix Queue\ngit_setup → devops"]
    SPAWN["Spawn parallel workers"]
    BACKEND["backend_code_v2_worker\n(daemon thread)"]
    FRONTEND["frontend_code_v2_worker\n(daemon thread)"]
    JOIN["thread.join()"]
    INTEG["Integration Agent"]
    SEC["Security Review"]
    DOCS["Documentation Agent"]
    DEVOPS["DevOps Containerization"]
    DONE2["status = COMPLETED"]

    ERR_LLM["status =\npaused_llm_connectivity"]
    ERR_LIMIT["status =\npaused_llm_limit"]
    ERR_FAIL["status = FAILED"]

    START --> CREATE --> PRA
    PRA --> CANCEL1
    CANCEL1 -->|Yes| ERR_FAIL
    CANCEL1 -->|No| PV3
    PV3 --> ADAPT --> CANCEL2
    CANCEL2 -->|Yes| ERR_FAIL
    CANCEL2 -->|No| DECISION

    DECISION -->|True| CT --> DONE
    DECISION -->|False| TL --> AE --> CONSOL --> PREFIX
    PREFIX --> SPAWN
    SPAWN --> BACKEND
    SPAWN --> FRONTEND
    BACKEND --> JOIN
    FRONTEND --> JOIN
    JOIN --> CANCEL3
    CANCEL3 -->|Yes| ERR_FAIL
    CANCEL3 -->|No| INTEG --> SEC --> DOCS --> DEVOPS --> DONE2

    PRA -.->|LLMUnreachable| ERR_LLM
    PV3 -.->|LLMRateLimit| ERR_LIMIT
    CT -.->|Exception| ERR_FAIL
```

## 2. Coding Team Swarm Loop

```mermaid
flowchart TD
    INIT["TechLead.run_plan_to_task_graph(plan_input)"]
    GROOM["TechLead.run_groom_task() per task\n→ acceptance_criteria, priority, subtasks"]
    LOOP_START{"Round < 500?"}
    CANCEL{"Cancelled?"}

    READY["find_ready_tasks()\nTO_DO + all deps MERGED"]
    FREE["find_free_agents()\nno current task"]
    ASSIGN["TechLead.run_assignments()\nassign tasks to agents"]

    IMPL["SeniorSWE.run_implement(task)\ntool_loop, max_rounds=16"]
    QG["Quality Gates:\nbuild → lint → code_review"]
    GATES_PASS{"Gates pass?"}

    REV_COUNT{"revision_count\n< 3?"}
    REVISION["return_for_revision(task)\nincrement revision_count\nstatus = TO_DO"]
    ACCEPT["Accept as-is\nset IN_REVIEW"]

    REVIEW["TechLead.run_code_review()"]
    APPROVED{"Approved?"}
    MERGE["mark_branch_merged()\nstatus = MERGED"]

    COMPLETE{"All tasks\nMERGED?"}
    DONE["Swarm complete"]
    PERSIST["Persist task graph snapshot"]

    INIT --> GROOM --> LOOP_START
    LOOP_START -->|Yes| CANCEL
    LOOP_START -->|No| DONE
    CANCEL -->|Yes| DONE
    CANCEL -->|No| READY --> FREE --> ASSIGN --> IMPL --> QG --> GATES_PASS

    GATES_PASS -->|Yes| REVIEW --> APPROVED
    GATES_PASS -->|No| REV_COUNT

    APPROVED -->|Yes| MERGE --> PERSIST
    APPROVED -->|No| REV_COUNT

    REV_COUNT -->|Yes| REVISION --> PERSIST
    REV_COUNT -->|No| ACCEPT --> PERSIST

    PERSIST --> COMPLETE
    COMPLETE -->|Yes| DONE
    COMPLETE -->|No| LOOP_START
```

## 3. Backend/Frontend V2 Lifecycle

```mermaid
stateDiagram-v2
    [*] --> SETUP

    SETUP: Setup Phase
    note right of SETUP
        Git init, README
        Development branch
        Lint/test pre-flight
    end note

    PLANNING: Planning Phase
    note right of PLANNING
        LLM generates microtasks
        Dependency analysis
        Tool agent enrichment
    end note

    EXECUTION: Execution Phase
    note right of EXECUTION
        Per-microtask:
        Route to tool agent
        Generate code
        Write files
    end note

    REVIEW: Review Gate
    note right of REVIEW
        code_review (max 3 retries)
        → QA (max 3 retries)
        → security (max 3 retries)
        Total cap: 15 iterations
    end note

    PROBLEM_SOLVING: Problem Solving
    note right of PROBLEM_SOLVING
        Batch fix strategy
        All issues fixed together
        Then retry review gate
    end note

    DOCUMENTATION: Documentation Phase
    note right of DOCUMENTATION
        Docstrings
        README
        API docs
    end note

    DELIVER: Deliver Phase
    note right of DELIVER
        Commit changes
        Merge to development
        Handoff to orchestrator
    end note

    SETUP --> PLANNING
    PLANNING --> EXECUTION
    EXECUTION --> REVIEW
    REVIEW --> DOCUMENTATION: All gates pass
    REVIEW --> PROBLEM_SOLVING: Gate fails
    PROBLEM_SOLVING --> REVIEW: Retry
    PROBLEM_SOLVING --> [*]: Max retries exceeded\n(stop mode)
    DOCUMENTATION --> DELIVER
    DELIVER --> [*]
```

### Microtask Status Transitions

```mermaid
stateDiagram-v2
    [*] --> PENDING
    PENDING --> IN_PROGRESS: Assigned to tool agent
    IN_PROGRESS --> IN_CODE_REVIEW: Code generated
    IN_CODE_REVIEW --> IN_QA_TESTING: Code review pass
    IN_CODE_REVIEW --> REVIEW_FAILED: Code review fail (max retries)
    IN_QA_TESTING --> IN_SECURITY_TESTING: QA pass
    IN_QA_TESTING --> IN_CODE_REVIEW: QA fail → restart reviews
    IN_SECURITY_TESTING --> IN_DOCUMENTATION: Security pass
    IN_SECURITY_TESTING --> IN_CODE_REVIEW: Security fail → restart reviews
    IN_DOCUMENTATION --> COMPLETED: Docs added
    REVIEW_FAILED --> FAILED: on_failure=stop
    REVIEW_FAILED --> SKIPPED: on_failure=skip_continue
```

## 4. Microtask Review Gate Pipeline

```mermaid
flowchart LR
    INPUT["Tool Agent\nOutput"]
    CR["Code Review\nAgent"]
    CR_PASS{"Pass?"}
    QA["QA Expert\nAgent"]
    QA_PASS{"Pass?"}
    SEC["Security\nAgent"]
    SEC_PASS{"Pass?"}
    DOC["Documentation\nAgent"]
    DONE["COMPLETED"]

    PS["Problem Solving\nBatch Fix"]
    CR_COUNT{"Retries\n< 3?"}
    QA_COUNT{"Retries\n< 3?"}
    SEC_COUNT{"Retries\n< 3?"}
    FAIL["REVIEW_FAILED"]

    TOTAL{"Total cycles\n< 15?"}

    INPUT --> CR --> CR_PASS
    CR_PASS -->|Yes| QA --> QA_PASS
    CR_PASS -->|No| CR_COUNT
    CR_COUNT -->|Yes| PS --> TOTAL
    CR_COUNT -->|No| FAIL

    QA_PASS -->|Yes| SEC --> SEC_PASS
    QA_PASS -->|No| QA_COUNT
    QA_COUNT -->|Yes| PS
    QA_COUNT -->|No| FAIL

    SEC_PASS -->|Yes| DOC --> DONE
    SEC_PASS -->|No| SEC_COUNT
    SEC_COUNT -->|Yes| PS
    SEC_COUNT -->|No| FAIL

    TOTAL -->|Yes| CR
    TOTAL -->|No| FAIL

    style PS fill:#fff3cd
    style FAIL fill:#f8d7da
    style DONE fill:#d4edda
```

## 5. DevOps 5-Phase Pipeline

```mermaid
flowchart TD
    subgraph Phase1["Phase 1: Intake"]
        TC["TaskClarifier Agent"]
        EP["Environment Policy Lookup\ndev/staging/production"]
        CONTRACT["Build SubtaskContracts"]
    end

    subgraph Phase2["Phase 2: Change Design"]
        IAC["IaC Agent\nTerraform/CDK"]
        CICD["CI/CD Pipeline Agent\nGitHub Actions/GitLab"]
        DEPLOY["Deployment Strategy\nBlue-green/Canary/Rolling"]
    end

    subgraph Phase3["Phase 3: Implementation"]
        GIT["Git Branch +\nArtifact Writing"]
    end

    subgraph Phase4["Phase 4: Validation"]
        G1["iac_validate"]
        G2["iac_validate_fmt"]
        G3["policy_checks"]
        G4["pipeline_lint"]
        G5["pipeline_gate_check"]
        G6["deployment_dry_run"]
        G7["security_review"]
        G8["change_review"]
    end

    subgraph Phase45["Phase 4.5: Execution"]
        TF["Terraform\ninit/validate/plan"]
        CDK["CDK synth"]
        DC["Docker Compose\nconfig validate"]
        HELM["Helm lint"]
    end

    subgraph Phase46["Phase 4.6: Debug-Patch"]
        DBG["InfraDebug Agent"]
        PATCH["InfraPatch Agent"]
        RETRY46{"Iterations\n< 3?"}
    end

    subgraph Phase5["Phase 5: Completion"]
        DOCR["Documentation +\nRunbook Agent"]
        RR["Release Readiness\nAssessment"]
        PKG["DevOpsCompletionPackage"]
    end

    Phase1 --> Phase2
    IAC --> Phase3
    CICD --> Phase3
    DEPLOY --> Phase3
    Phase3 --> Phase4

    G1 --> G2 --> G3 --> G4 --> G5 --> G6 --> G7 --> G8

    Phase4 -->|All pass| Phase45
    Phase4 -->|Any fail| Phase46

    Phase45 --> Phase5
    Phase46 --> DBG --> PATCH --> RETRY46
    RETRY46 -->|Yes| Phase4
    RETRY46 -->|No| Phase5

    Phase5 --> PKG
```

### DevOps Environment Decision

```mermaid
flowchart LR
    ENV{"Environment?"}
    DEV["dev\nauto_deploy=true\napproval=false\nstrictness=low"]
    STG["staging\nauto_deploy=true\nrollback_test=true\nstrictness=medium"]
    PROD["production\nauto_deploy=false\napproval=REQUIRED\nrollback_test=true\nstrictness=high"]

    ENV -->|dev| DEV
    ENV -->|staging| STG
    ENV -->|production| PROD
```

## 6. Planning V3 Workflow

```mermaid
flowchart TD
    START["Planning V3 run_workflow()"]

    INTAKE["INTAKE (5%)\nParse client_name, brief, spec\n→ initial ClientContext"]

    DISC["DISCOVERY (15%)\nLLM extracts:\nproblem_summary\nopportunity_statement\ntarget_users\nsuccess_criteria"]

    REQ["REQUIREMENTS (25%)\nGenerate OpenQuestions:\nRPO/RTO options\nDeployment options\nCompliance notes"]

    SYN["SYNTHESIS (35%)\nMerge market research\nevidence if available"]

    DOCPROD["DOCUMENT PRODUCTION (45%)\nWrite client_context.md\nWrite initial_spec.md"]

    PRA_OPT{"use_product\n_analysis?"}
    PRA_RUN["Run PRA Agent\nvalidate + refine spec"]

    PV2_OPT{"use_planning\n_v2?"}
    PV2_RUN["Run Planning V2\nlegacy 6-phase planning"]

    ARCH_OPT{"run_architecture\n_fn?"}
    ARCH_RUN["Architecture Expert\ngenerate architecture_overview"]

    HANDOFF["Build HandoffPackage\nclient_context + validated_spec +\nPRD + architecture + artifacts"]

    SUB["SUB_AGENT PROVISIONING (90%)"]
    GAP{"capability_gap\nprovided?"}
    BUILD["Write sub_agent_spec.md\nAI Systems build"]

    DONE["COMPLETE (100%)\nReturn HandoffPackage"]

    START --> INTAKE --> DISC --> REQ --> SYN --> DOCPROD

    DOCPROD --> PRA_OPT
    PRA_OPT -->|Yes| PRA_RUN --> PV2_OPT
    PRA_OPT -->|No| PV2_OPT

    PV2_OPT -->|Yes| PV2_RUN --> ARCH_OPT
    PV2_OPT -->|No| ARCH_OPT

    ARCH_OPT -->|Yes| ARCH_RUN --> HANDOFF
    ARCH_OPT -->|No| HANDOFF

    HANDOFF --> SUB --> GAP
    GAP -->|Yes| BUILD --> DONE
    GAP -->|No| DONE
```

## 7. Threading Model — Legacy Path

```mermaid
flowchart LR
    subgraph MainThread["Main Thread"]
        M1["PRA"]
        M2["Planning V3"]
        M3["Tech Lead"]
        M4["Architecture Expert"]
        M5["Consolidation"]
        M6["Prefix Queue\n(sequential)"]
        M7["Spawn Workers"]
        M8["thread.join()"]
        M9["Integration"]
        M10["Security"]
        M11["Documentation"]
        M12["DevOps"]
    end

    subgraph BackendThread["Backend Worker Thread (daemon)"]
        B1["pop_runnable_task()"]
        B2["BackendCodeV2TeamLead\n.run_workflow()"]
        B3["Update completed/failed"]
        B4{"Queue\nempty?"}
        B5["DEP_WAIT_SLEEP\n0.5s"]
    end

    subgraph FrontendThread["Frontend Worker Thread (daemon)"]
        F1["pop_runnable_task()"]
        F2["FrontendCodeV2TeamLead\n.run_workflow()"]
        F3["Update completed/failed"]
        F4{"Queue\nempty?"}
        F5["DEP_WAIT_SLEEP\n0.5s"]
    end

    subgraph SharedState["state_lock (threading.Lock)"]
        S1["llm_limit_exceeded"]
        S2["llm_connectivity_failed"]
        S3["repaired_tasks"]
        S4["completed set"]
        S5["failed dict"]
        S6["queues"]
    end

    M1 --> M2 --> M3 --> M4 --> M5 --> M6 --> M7
    M7 --> BackendThread
    M7 --> FrontendThread
    M8 --> M9 --> M10 --> M11 --> M12

    B1 --> B2 --> B3 --> B4
    B4 -->|No| B1
    B4 -->|Yes, deps pending| B5 --> B1

    F1 --> F2 --> F3 --> F4
    F4 -->|No| F1
    F4 -->|Yes, deps pending| F5 --> F1

    BackendThread -.->|acquires| SharedState
    FrontendThread -.->|acquires| SharedState
    BackendThread --> M8
    FrontendThread --> M8
```

## 8. Build Fix Loop

```mermaid
flowchart TD
    BUILD["Run Build Verification"]
    TYPE{"Build type?"}

    FE["Frontend:\nng build\n(with nvm fallback)"]
    BE["Backend:\npython syntax check\n+ pytest (if tests exist)"]
    DO["DevOps:\nYAML validation\n+ docker build\n(if Dockerfile exists)"]

    PASS{"Build\npasses?"}
    DONE["Return success"]

    IDENTIFY["Identify issues\nfrom error output"]
    ATTEMPT{"Attempt\n< 15?"}
    SAME{"Same error\n6+ times?"}
    ABORT["Abort:\nreturn failure"]

    NEXT["Get next issue"]
    LLM["LLM generates fix\n(FIX_PROMPT)"]
    PARSE["Parse fix output\nextract file edits"]
    WRITE["Write fixed files"]
    REBUILD["Re-run build"]
    REPASS{"Build\npasses?"}

    BUILD --> TYPE
    TYPE -->|frontend| FE --> PASS
    TYPE -->|backend| BE --> PASS
    TYPE -->|devops| DO --> PASS

    PASS -->|Yes| DONE
    PASS -->|No| IDENTIFY --> ATTEMPT

    ATTEMPT -->|Yes| SAME
    ATTEMPT -->|No| ABORT

    SAME -->|Yes| ABORT
    SAME -->|No| NEXT --> LLM --> PARSE --> WRITE --> REBUILD --> REPASS

    REPASS -->|Yes| DONE
    REPASS -->|No| IDENTIFY

    style DONE fill:#d4edda
    style ABORT fill:#f8d7da
```

## 9. Planning V3 → Coding Team Data Flow

End-to-end data transformation from planning to execution.

```mermaid
flowchart TD
    subgraph PV3Output["Planning V3 Output"]
        HP["HandoffPackage"]
        CC["ClientContext"]
        VS["validated_spec_content"]
        PRD["prd_content"]
        AO["architecture_overview"]
    end

    ADAPT["adapt_planning_v3_result()"]

    subgraph AdapterResult["PlanningV2AdapterResult"]
        REQ["ProductRequirements\ntitle, description,\nacceptance_criteria"]
        PO["project_overview\nfeatures_and_functionality_doc\ngoals"]
        HIER["PlanningHierarchy\ninitiatives → epics → stories → tasks"]
        SPEC["final_spec_content"]
    end

    subgraph CTInput["CodingTeamPlanInput"]
        RT["requirements_title"]
        RD["requirements_description"]
        POV["project_overview"]
        RP["repo_path"]
        AOV["architecture_overview"]
        ECS["existing_code_summary"]
    end

    subgraph TaskGraph["TaskGraphService"]
        TG_T["Task[]\nid, title, deps, acceptance_criteria"]
        TG_S["StackSpec[]\ntools_services, name"]
        TG_ST["Subtask[]\nper task decomposition"]
    end

    PV3Output --> ADAPT
    ADAPT --> AdapterResult
    AdapterResult --> CTInput
    CTInput -->|TechLead.run_plan_to_task_graph| TaskGraph
```
