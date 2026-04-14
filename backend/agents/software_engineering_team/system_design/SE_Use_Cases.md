# Software Engineering Team — Use Cases

## UC-1: Submit New Project

A user submits a project specification and monitors progress through polling.

```mermaid
sequenceDiagram
    actor User
    participant UI as Angular Frontend
    participant UAPI as Unified API
    participant API as SE Team API
    participant JS as Job Store
    participant ORC as Orchestrator

    User->>UI: Submit project spec
    UI->>UAPI: POST /api/software-engineering/run-team
    UAPI->>API: Forward (repo_path)
    API->>JS: create_job(repo_path) → PENDING
    API->>ORC: Start background thread<br/>(or Temporal workflow)
    API->>JS: start_job_heartbeat_thread(120s)
    API-->>UI: {job_id, status: "running"}

    loop Poll every few seconds
        UI->>UAPI: GET /run-team/{job_id}
        UAPI->>API: Forward
        API->>JS: get_job(job_id)
        JS-->>API: {progress, phase, task_states, team_progress}
        API-->>UI: JobStatusResponse
        UI-->>User: Show progress
    end

    ORC->>JS: update_job(status: "completed")
    UI-->>User: Project complete
```

## UC-2: Full Pipeline — Coding Team Path (Primary)

The primary execution path using the Coding Team swarm orchestrator.

```mermaid
sequenceDiagram
    participant ORC as Orchestrator
    participant PRA as PRA Agent
    participant JS as Job Store
    participant PV3 as Planning V3
    participant ADP as Adapter
    participant ARCH as Architecture Expert
    participant CT as Coding Team
    participant TL as Tech Lead
    participant SWE as Senior SWE(s)
    participant QG as Quality Gates

    Note over ORC: Phase 1: product_analysis
    ORC->>PRA: validate spec
    PRA->>PRA: spec_review → communicate → spec_update → spec_cleanup
    PRA-->>ORC: validated_spec

    ORC->>JS: check cancellation
    Note over ORC: Phase 2: planning
    ORC->>PV3: run_workflow(validated_spec)
    PV3->>PV3: intake(5%) → discovery(15%) → requirements(25%)
    PV3->>PV3: synthesis(35%) → document_production(45%)

    Note over PV3,ARCH: Architecture callback during doc production
    PV3->>ARCH: run_architecture_fn()
    ARCH-->>PV3: architecture_overview

    PV3->>PV3: sub_agent_provisioning(90%)
    PV3-->>ORC: HandoffPackage

    ORC->>ADP: adapt_planning_v3_result()
    ADP-->>ORC: CodingTeamPlanInput

    ORC->>JS: check cancellation
    Note over ORC: Phase 3: execution (coding_team)
    ORC->>CT: run_coding_team_orchestrator(plan_input)
    CT->>TL: run_plan_to_task_graph()
    TL-->>CT: tasks[] + stacks[]
    CT->>TL: run_groom_task() per task

    loop Swarm loop (max 500 rounds)
        CT->>CT: find_ready_tasks (TO_DO + deps MERGED)
        CT->>CT: find_free_agents
        CT->>TL: run_assignments(ready, free)
        TL-->>CT: assignments[]

        CT->>SWE: run_implement(task)
        SWE-->>CT: files, feature_branch

        CT->>QG: build → lint → code_review
        alt Gates pass
            CT->>TL: run_code_review(changes)
            alt Approved
                CT->>CT: mark_branch_merged()
            else Needs revision
                CT->>CT: return_for_revision (max 3)
            end
        else Gates fail
            CT->>CT: return_for_revision
        end
    end

    CT-->>ORC: All tasks MERGED
    ORC->>JS: update_job(status: "completed")
```

## UC-3: Full Pipeline — Legacy Path

The legacy path using parallel backend/frontend worker threads.

```mermaid
sequenceDiagram
    participant ORC as Orchestrator
    participant TL as Tech Lead Agent
    participant AE as Architecture Expert
    participant BW as Backend Worker<br/>(daemon thread)
    participant FW as Frontend Worker<br/>(daemon thread)
    participant BV2 as BackendCodeV2<br/>TeamLead
    participant FV2 as FrontendCodeV2<br/>TeamLead
    participant INT as Integration Agent
    participant SEC as Security Agent
    participant DOC as Documentation Agent

    Note over ORC: After Planning V3 (same as UC-2)

    ORC->>TL: generate TaskAssignment
    TL-->>ORC: tasks[], execution_order

    ORC->>AE: generate SystemArchitecture
    AE-->>ORC: architecture

    ORC->>ORC: Planning consolidation<br/>(master_plan.md, risk_register)

    Note over ORC: Prefix queue (sequential)
    ORC->>ORC: git_setup task
    ORC->>ORC: devops task

    Note over ORC: Parallel execution
    ORC->>BW: Start thread (backend_code_v2_queue)
    ORC->>FW: Start thread (frontend_code_v2_queue)

    par Backend Worker
        loop Pop runnable tasks
            BW->>BV2: run_workflow(task)
            Note over BV2: 7 phases: setup → planning →<br/>execution → review →<br/>problem_solving → docs → deliver
            BV2-->>BW: WorkflowResult
        end
    and Frontend Worker
        loop Pop runnable tasks
            FW->>FV2: run_workflow(task)
            Note over FV2: 7 phases (same structure,<br/>17 tool agents)
            FV2-->>FW: WorkflowResult
        end
    end

    ORC->>ORC: thread.join() (both workers)

    ORC->>INT: validate backend/frontend contract
    INT-->>ORC: IntegrationOutput (passed/issues)

    ORC->>SEC: security review
    SEC-->>ORC: SecurityOutput

    ORC->>DOC: final documentation
    DOC-->>ORC: DocumentationOutput

    ORC->>ORC: DevOps containerization
    ORC->>ORC: status = COMPLETED
```

## UC-4: User Clarification Flow

When agents need user input to proceed.

```mermaid
sequenceDiagram
    actor User
    participant UI as Frontend
    participant API as SE Team API
    participant JS as Job Store
    participant ORC as Orchestrator
    participant AGENT as Any Agent

    AGENT->>AGENT: Detects unclear requirement
    AGENT->>ORC: Return questions

    ORC->>ORC: _convert_to_structured_questions()
    Note over ORC: Each question gets:<br/>- unique id<br/>- question_text<br/>- options: [Yes, No, Not sure]<br/>- recommendation

    ORC->>JS: add_pending_questions(job_id, questions)
    Note over JS: waiting_for_answers = true

    ORC->>ORC: _wait_for_user_answers()<br/>Poll interval: 5s<br/>Timeout: 3600s (1 hour)

    loop User polls and sees questions
        UI->>API: GET /run-team/{job_id}
        API->>JS: get_job()
        JS-->>API: {pending_questions: [...], waiting_for_answers: true}
        API-->>UI: Show questions to user
    end

    User->>UI: Select answers
    UI->>API: POST /run-team/{job_id}/answers
    API->>JS: submit_answers(job_id, answers)
    Note over JS: waiting_for_answers = false<br/>pending_questions = []<br/>submitted_answers += answers

    ORC->>JS: Check waiting_for_answers
    Note over ORC: Returns false → resume

    ORC->>AGENT: Continue with answers
```

## UC-5: Error Recovery

Multiple error recovery paths available in the system.

```mermaid
sequenceDiagram
    participant ORC as Orchestrator
    participant WORKER as Worker Thread
    participant REP as Repair Agent
    participant JS as Job Store
    participant BFS as BuildFix Specialist
    actor User

    Note over WORKER: Scenario A: Agent Crash
    WORKER->>WORKER: Catches REPAIRABLE_EXCEPTION<br/>(NameError, SyntaxError, ImportError,<br/>AttributeError, IndentationError,<br/>ModuleNotFoundError)
    WORKER->>WORKER: _parse_traceback_for_crash()
    WORKER->>REP: RepairExpertAgent(traceback)
    alt Repair succeeds
        REP-->>WORKER: Fix applied
        WORKER->>WORKER: Re-queue task
        WORKER->>JS: status = RUNNING
    else Repair fails
        WORKER->>JS: status = AGENT_CRASH
    end

    Note over ORC: Scenario B: LLM Connectivity
    ORC->>ORC: LLMUnreachableAfterRetriesError
    ORC->>JS: status = paused_llm_connectivity
    User->>JS: POST /resume-after-llm-check
    JS->>ORC: Resume orchestration

    Note over ORC: Scenario C: LLM Rate Limit
    ORC->>ORC: LLMRateLimitError
    ORC->>JS: status = paused_llm_limit
    Note over ORC: Job pauses until limit resets

    Note over WORKER: Scenario D: Build Failure
    WORKER->>WORKER: _run_build_verification() fails
    WORKER->>BFS: _try_build_fix_one_at_a_time()
    loop Max 15 attempts
        BFS->>BFS: Get next issue
        BFS->>BFS: LLM generates fix
        BFS->>BFS: Write files, rebuild
        alt Build passes
            BFS-->>WORKER: Success
        else Same error 6+ times
            BFS-->>WORKER: Abort
        end
    end

    Note over User: Scenario E: Retry Failed Tasks
    User->>JS: POST /retry-failed
    JS->>ORC: run_failed_tasks(job_id)
    ORC->>ORC: Reconstruct failed tasks<br/>from _all_tasks
    ORC->>ORC: Partition into queues
    ORC->>ORC: Re-run pipeline
```

## UC-6: Planning Cache

Short-circuits the Design phase when inputs are unchanged.

```mermaid
sequenceDiagram
    participant ORC as Orchestrator
    participant PC as Planning Cache
    participant TL as Tech Lead Agent
    participant FS as File System

    ORC->>PC: compute_planning_cache_key()
    Note over PC: SHA256(spec + architecture +<br/>project_overview[:2000])[:24]

    PC->>FS: Check plan_dir/planning_cache/{key}.json

    alt Cache HIT
        FS-->>PC: Cached data
        PC-->>ORC: {assignment: TaskAssignment,<br/>requirement_task_mapping,<br/>summary}
        Note over ORC: Skip Tech Lead alignment<br/>and conformance loops
        ORC->>ORC: Use cached TaskAssignment directly
    else Cache MISS
        PC-->>ORC: None
        ORC->>TL: Generate TaskAssignment
        TL-->>ORC: TaskAssignment
        ORC->>PC: set_cached_plan(key, assignment)
        PC->>FS: Write plan_dir/planning_cache/{key}.json
    end
```
