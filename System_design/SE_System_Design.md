# Software Engineering Team — System Design

## 1. Core Data Model

```mermaid
classDiagram
    class ProductRequirements {
        +str title
        +str description
        +List~str~ acceptance_criteria
        +List~str~ constraints
        +str priority = "medium"
        +Dict metadata
    }

    class SystemArchitecture {
        +str overview
        +List~ArchitectureComponent~ components
        +str architecture_document
        +Dict diagrams
        +List~str~ decisions
        +str tenancy_model
        +str reliability_model
        +Dict planning_hints
    }

    class ArchitectureComponent {
        +str name
        +str type
        +str description
        +str technology
        +List~str~ dependencies
        +List~str~ interfaces
    }

    class Task {
        +str id
        +TaskType type
        +str title
        +str description
        +str user_story
        +str assignee
        +str requirements
        +List~str~ dependencies
        +List~str~ acceptance_criteria
        +TaskStatus status = PENDING
        +str feature_branch_name
        +str output
        +Dict artifacts
        +Dict metadata
    }

    class TaskAssignment {
        +List~Task~ tasks
        +List~str~ execution_order
        +str rationale
    }

    class TaskUpdate {
        +str task_id
        +str agent_type
        +str status = "completed"
        +str summary
        +List~str~ files_changed
        +bool needs_followup = false
        +str failure_reason
        +str failure_class
    }

    class TaskType {
        <<enumeration>>
        ARCHITECTURE
        GIT_SETUP
        DEVOPS
        SECURITY
        BACKEND
        FRONTEND
        QA
        DOCUMENTATION
    }

    class TaskStatus {
        <<enumeration>>
        PENDING
        IN_PROGRESS
        READY_FOR_REVIEW
        APPROVED
        MERGED
        COMPLETED
        BLOCKED
        FAILED
    }

    SystemArchitecture "1" --> "*" ArchitectureComponent
    TaskAssignment "1" --> "*" Task
    Task --> TaskType
    Task --> TaskStatus
    Task "1" --> "0..1" TaskUpdate
```

## 2. Planning Hierarchy Model

```mermaid
classDiagram
    class PlanningHierarchy {
        +List~Initiative~ initiatives
        +List~str~ execution_order
        +str rationale
    }

    class Initiative {
        +str id
        +str title
        +str description
        +List~Epic~ epics
    }

    class Epic {
        +str id
        +str title
        +str description
        +List~str~ user_stories_summary
        +List~str~ acceptance_criteria
        +List~StoryPlan~ stories
    }

    class StoryPlan {
        +str id
        +str title
        +str description
        +str user_story
        +str requirements
        +List~str~ acceptance_criteria
        +str example
        +List~TaskPlan~ tasks
        +Dict metadata
    }

    class TaskPlan {
        +str id
        +str title
        +str description
        +str user_story
        +str assignee
        +str requirements
        +List~str~ dependencies
        +List~str~ acceptance_criteria
        +int complexity_points = 2
        +Dict metadata
    }

    PlanningHierarchy "1" --> "*" Initiative
    Initiative "1" --> "*" Epic
    Epic "1" --> "*" StoryPlan
    StoryPlan "1" --> "*" TaskPlan
```

## 3. Coding Team Data Model

```mermaid
classDiagram
    class CodingTeamPlanInput {
        +str requirements_title
        +str requirements_description
        +Dict project_overview
        +Any hierarchy
        +str final_spec_content
        +str repo_path
        +str architecture_overview
        +str existing_code_summary
        +List~Dict~ resolved_questions
        +List~str~ open_questions
        +List~str~ assumptions
    }

    class CT_Task["Task (Coding Team)"] {
        +str id
        +str title
        +str description
        +List~str~ dependencies
        +CT_TaskStatus status = TO_DO
        +str assigned_agent_id
        +str feature_branch
        +datetime merged_at
        +List~str~ acceptance_criteria
        +str out_of_scope
        +str priority
        +List~Subtask~ subtasks
        +int revision_count = 0
        +List~Dict~ revision_feedback
    }

    class Subtask {
        +str id
        +str title
        +str description
        +List~str~ dependencies
        +CT_TaskStatus status = TO_DO
        +datetime completed_at
    }

    class CT_TaskStatus["TaskStatus (Coding Team)"] {
        <<enumeration>>
        TO_DO
        IN_PROGRESS
        IN_REVIEW
        MERGED
    }

    class StackSpec {
        +List~str~ tools_services
        +str name
    }

    class TaskGraphService {
        +str job_id
        +add_task()
        +update_task()
        +assign_task_to_agent()
        +get_task_for_agent()
        +mark_branch_merged()
        +set_task_in_review()
        +snapshot() Dict
        +restore(snapshot)
    }

    CodingTeamPlanInput --> CT_Task : produces
    CT_Task "1" --> "*" Subtask
    CT_Task --> CT_TaskStatus
    TaskGraphService "1" --> "*" CT_Task : manages
```

## 4. Microtask Model (Backend/Frontend V2)

```mermaid
classDiagram
    class Microtask {
        +str id
        +str title
        +str description
        +ToolAgentKind tool_agent
        +MicrotaskStatus status
        +List~str~ depends_on
        +Dict output_files
        +str notes
    }

    class MicrotaskStatus {
        <<enumeration>>
        PENDING
        IN_PROGRESS
        IN_CODE_REVIEW
        IN_QA_TESTING
        IN_SECURITY_TESTING
        IN_REVIEW
        IN_DOCUMENTATION
        COMPLETED
        FAILED
        REVIEW_FAILED
        SKIPPED
    }

    class ToolAgentKind_Backend["ToolAgentKind (Backend, 11)"] {
        <<enumeration>>
        DATA_ENGINEERING
        API_OPENAPI
        AUTH
        CICD
        CONTAINERIZATION
        DOCUMENTATION
        TESTING_QA
        SECURITY
        GIT_BRANCH_MANAGEMENT
        BUILD_SPECIALIST
        GENERAL
    }

    class Phase {
        <<enumeration>>
        SETUP
        PLANNING
        EXECUTION
        REVIEW
        PROBLEM_SOLVING
        DOCUMENTATION
        DELIVER
    }

    class MicrotaskReviewConfig {
        +int code_review_max_retries = 3
        +int qa_max_retries = 3
        +int security_max_retries = 3
        +str on_failure = "stop"
    }

    Microtask --> MicrotaskStatus
    Microtask --> ToolAgentKind_Backend
```

## 5. DevOps Data Model

```mermaid
classDiagram
    class DevOpsTaskSpec {
        +str task_id
        +str title
        +str platform_scope
        +str repo_context
        +str goal
        +str scope
        +DevOpsConstraints constraints
        +List~str~ acceptance_criteria
        +str risk_level
        +str environment
    }

    class DevOpsCompletionPackage {
        +List~str~ files_changed
        +Dict acceptance_criteria_trace
        +Dict quality_gates
        +ReleaseReadiness release_readiness
        +GitOperationsMetadata git_operations
    }

    class ReleaseReadiness {
        +str deployment_strategy
        +bool rollback_available
        +Dict approvals
        +List~str~ verification_checklist
    }

    class QualityGates {
        <<8 Hard Gates>>
        iac_validate
        iac_validate_fmt
        policy_checks
        pipeline_lint
        pipeline_gate_check
        deployment_dry_run
        security_review
        change_review
    }

    DevOpsTaskSpec --> DevOpsCompletionPackage : produces
    DevOpsCompletionPackage --> ReleaseReadiness
    DevOpsCompletionPackage --> QualityGates
```

### Environment Policies

| Environment | auto_deploy | approval | rollback_test | policy_strictness |
|------------|-------------|----------|---------------|-------------------|
| dev | true | false | false | low |
| staging | true | false | true | medium |
| production | false | **true** | true | **high** |

## 6. Planning V3 Data Model

```mermaid
classDiagram
    class ClientContext {
        +str client_name
        +str client_domain
        +str problem_summary
        +str opportunity_statement
        +List~str~ target_users
        +List~str~ success_criteria
        +Dict constraints
        +str rpo_rto
        +str slas
        +str compliance_notes
        +List~str~ tech_constraints
        +List~str~ assumptions
    }

    class HandoffPackage {
        +ClientContext client_context
        +str client_context_document_path
        +str validated_spec_path
        +str prd_path
        +str validated_spec_content
        +str prd_content
        +Dict planning_v2_artifact_paths
        +str architecture_overview
        +Dict sub_agent_blueprint
        +str summary
    }

    class OpenQuestion {
        +str id
        +str question_text
        +str context
        +str category
        +str priority
        +List~OpenQuestionOption~ options
        +bool allow_multiple = false
        +str source = "planning_v3"
    }

    class PlanningPhase {
        <<enumeration>>
        INTAKE
        DISCOVERY
        REQUIREMENTS
        SYNTHESIS
        DOCUMENT_PRODUCTION
        SUB_AGENT_PROVISIONING
    }

    HandoffPackage --> ClientContext
    HandoffPackage --> PlanningPhase : traverses
```

## 7. Job Lifecycle State Machine

```mermaid
stateDiagram-v2
    [*] --> pending : create_job()

    pending --> running : start orchestrator

    running --> completed : all tasks done
    running --> failed : unhandled exception
    running --> cancelled : user cancel
    running --> agent_crash : REPAIRABLE_EXCEPTION unresolved
    running --> paused_llm_connectivity : LLMUnreachableAfterRetries
    running --> paused_llm_limit : LLMRateLimitError

    failed --> running : POST /resume or /restart
    agent_crash --> running : POST /resume
    paused_llm_connectivity --> running : POST /resume-after-llm-check
    cancelled --> running : POST /restart
    completed --> running : POST /restart

    note right of running
        Heartbeat: 120s interval
        Stale detection: 1800s
        Cancellation: cooperative flag
    end note
```

## 8. Job Data Structure

| Field | Type | Purpose |
|-------|------|---------|
| `repo_path` | str | Workspace directory |
| `progress` | int (0-100) | Overall completion percentage |
| `current_task` | str | Active task description |
| `status_text` | str | Human-readable status |
| `task_results` | List[Dict] | Completed task summaries |
| `execution_order` | List[str] | Task ID ordering |
| `error` | str | Error message if failed |
| `architecture_overview` | str | Architecture Expert output |
| `requirements_title` | str | Project title from PRA |
| `pending_questions` | List[Dict] | Structured clarification questions |
| `waiting_for_answers` | bool | Blocks orchestrator when true |
| `submitted_answers` | List[Dict] | User responses |
| `cancel_requested` | bool | Cooperative cancellation flag |
| `events` | List[Dict] | Orchestration event log |
| `task_states` | Dict[str, Dict] | Per-task status, assignee, errors |
| `team_progress` | Dict[str, Dict] | Per-team phase, progress, microtask info |
| `failed_tasks` | List[Dict] | Failed task IDs with reasons |
| `_all_tasks` | Dict[str, Dict] | Serialized Task objects (for retry) |
| `_spec_content` | str | Original spec (for retry) |
| `_architecture_overview` | str | Architecture (for retry) |

## 9. LLM Service Architecture

```mermaid
flowchart TD
    CALLER["Agent Code\nget_client(agent_key)"]
    FAC["LLM Factory"]
    OVERRIDE{"LLM_MODEL_{key}\nenv var?"}
    DEFAULT["AGENT_DEFAULT_MODELS\nqwen3.5:397b-cloud"]
    CACHE["Client Cache\n(model, base_url, timeout)"]

    CALLER --> FAC
    FAC --> OVERRIDE
    OVERRIDE -->|yes| CACHE
    OVERRIDE -->|no| DEFAULT
    DEFAULT --> CACHE

    subgraph OllamaClient["OllamaLLMClient"]
        SEM["BoundedSemaphore(4)\nConcurrency limit"]
        RETRY["Retry: 6 attempts\nBackoff: 2-120s\nJitter: random"]
        CTX["Context: 262144 tokens\nMax output: 32768"]
        CONT["Continuation: 10 cycles\nContext chars: 150"]
        METHODS["complete_json()\ncomplete()\nget_max_context_tokens()"]
    end

    CACHE --> OllamaClient

    subgraph ErrorTypes["Error Hierarchy"]
        E1["LLMError (base)"]
        E2["LLMRateLimitError"]
        E3["LLMTemporaryError"]
        E4["LLMPermanentError"]
        E5["LLMUnreachableAfterRetries"]
    end
```

## 10. Temporal Workflow Architecture

```mermaid
flowchart TD
    subgraph Workflows["Workflow Types"]
        W1["RunTeamWorkflow\nTimeout: 48h\nSingle activity"]
        W2["RetryFailedWorkflow\nTimeout: 24h\nSingle activity"]
        W3["RunTeamWorkflowV2\nMulti-phase"]
        W4["StandaloneJobWorkflow\nTimeout: 12h"]
    end

    subgraph V2Phases["RunTeamWorkflowV2 Phases"]
        P1["parse_spec_activity\nTimeout: 4h\nHeartbeat: 5min"]
        P2["plan_project_activity\nTimeout: 4h\nHeartbeat: 5min"]
        P3["execute_coding_team_activity\nTimeout: 36h\nHeartbeat: 10min"]
    end

    subgraph Standalone["StandaloneJobWorkflow Types"]
        S1["frontend-code-v2"]
        S2["backend-code-v2"]
        S3["product-analysis"]
    end

    subgraph RetryPolicy["Default Retry Policy"]
        RP["max_attempts: 3\ninitial_interval: 30s\nmaximum_interval: 2min\nbackoff_coefficient: 2.0"]
    end

    W3 --> P1 --> P2 --> P3
    W4 --> Standalone
    Workflows --> RetryPolicy

    subgraph PhaseModels["Inter-Phase Data"]
        PM1["SpecParseResult\nspec_content\nvalidated_spec\nrequirements_title"]
        PM2["PlanResult\nadapter_result_dict\nspec_content_for_planning"]
        PM3["ExecutionResult\ncompleted_task_ids\nfailed_tasks\nmerged_count"]
    end

    P1 -->|produces| PM1
    PM1 -->|consumed by| P2
    P2 -->|produces| PM2
    PM2 -->|consumed by| P3
    P3 -->|produces| PM3
```

## 11. API Design

### SE Team API Endpoints

| Method | Path | Request | Response | Purpose |
|--------|------|---------|----------|---------|
| POST | `/run-team` | `RunTeamRequest(repo_path)` | `RunTeamResponse(job_id, status)` | Start new job |
| POST | `/run-team/upload` | Multipart (project_name + spec_file) | `RunTeamResponse` | Upload spec and start |
| GET | `/run-team/jobs` | - | `RunningJobsResponse` | List active jobs |
| GET | `/run-team/{job_id}` | - | `JobStatusResponse` (30+ fields) | Poll job status |
| POST | `/run-team/{job_id}/cancel` | - | `CancelJobResponse` | Cancel running job |
| DELETE | `/run-team/{job_id}` | - | `DeleteJobResponse` | Delete job record |
| POST | `/run-team/{job_id}/resume` | - | `RunTeamResponse` | Resume paused/failed job |
| POST | `/run-team/{job_id}/restart` | - | `RunTeamResponse` | Restart from scratch |
| POST | `/run-team/{job_id}/retry-failed` | - | `RetryResponse` | Re-run failed tasks only |
| POST | `/run-team/{job_id}/resume-after-llm-check` | - | `RetryResponse` | Resume after LLM outage |
| POST | `/run-team/{job_id}/answers` | `SubmitAnswersRequest` | `JobStatusResponse` | Answer clarification questions |

### Planning V3 API Endpoints

| Method | Path | Request | Response | Purpose |
|--------|------|---------|----------|---------|
| POST | `/run` | `PlanningV3RunRequest` | `PlanningV3RunResponse` | Start planning |
| GET | `/status/{job_id}` | - | `PlanningV3StatusResponse` | Poll status |
| GET | `/result/{job_id}` | - | `PlanningV3ResultResponse` | Get handoff package |
| GET | `/jobs` | - | Job list | List active jobs |
| POST | `/{job_id}/answers` | Answers | `PlanningV3StatusResponse` | Submit answers |

### Coding Team API Endpoints

| Method | Path | Request | Response | Purpose |
|--------|------|---------|----------|---------|
| POST | `/run` | `RunRequest(repo_path, plan_input)` | `RunResponse(job_id)` | Start coding |
| GET | `/status/{job_id}` | - | `StatusResponse` | Poll status with task graph |
| GET | `/jobs` | - | Job list | List active jobs |

### Key Response Models

**JobStatusResponse** includes: job_id, status, repo_path, requirements_title, architecture_overview, current_task, status_text, task_results[], task_ids[], progress (0-100), error, failed_tasks[], phase, task_states{}, team_progress{}, pending_questions[], waiting_for_answers, planning_subprocess, planning_completed_phases[], analysis_subprocess, analysis_completed_phases[], planning_hierarchy.

**TeamProgressEntry** includes: current_phase, progress (0-100), current_task_id, current_microtask, current_microtask_phase, phase_detail, current_microtask_index, microtasks_completed, microtasks_total.

**PendingQuestion** includes: id, question_text, context, recommendation, options[QuestionOption], required, allow_multiple, source. Default options: Yes / No / Not sure.
