# System Design

> Goes **beneath** the two legacy PNGs ([`../designs/Agentic-team-architecture.png`](../designs/Agentic-team-architecture.png), [`../designs/AgenticTeamApiInteractionsArchitecture.png`](../designs/AgenticTeamApiInteractionsArchitecture.png)) with a data-and-module view neither PNG exposes:
> - module dependency graph of the team's Python package,
> - ER diagram of the persistence schema shared by SQLite and Postgres backends,
> - Pydantic model catalogue,
> - runtime-mode decision tree.
>
> Vocabulary remains aligned with the legacy PNGs: `Orchestrator Agent`, `Agents`, `Processes`, `File System`, `Database`.

## 1. Module dependency graph

```mermaid
graph LR
    main["api/main.py<br/>(FastAPI app, 27 endpoints, 933 lines)"]

    %% Orchestrator internals
    agent["assistant/agent.py<br/>(ProcessDesignerAgent — LLM chat)"]
    store["assistant/store.py<br/>(AgenticTeamStore — shared SQLite)"]
    runner["runtime/pipeline_runner.py<br/>(PipelineRunner — DAG executor)"]
    builder["runtime/agent_builder.py<br/>(build_agent / call_agent / generate_starter_prompts)"]
    roster["roster_validation.py<br/>(validate_roster)"]
    envprov["agent_env_provisioning.py<br/>(schedule_provision_step_agents)"]
    infra["infrastructure.py<br/>(TeamFormStore + JobServiceClient + provision_team)"]
    models["models.py<br/>(Pydantic models + enums)"]
    test_store["testing/store.py<br/>(AgenticTestStore — testing-mode SQLite)"]

    %% Shared / external
    pg["postgres/__init__.py<br/>(AGENTIC_POSTGRES_SCHEMA)"]
    tmp["temporal/__init__.py<br/>(AgenticTeamProvisioningWorkflow)"]
    llm["llm_service"]
    shared_pg["shared_postgres"]
    shared_tmp["shared_temporal"]
    shared_obs["shared_observability"]
    jsc["job_service_client"]
    apt["agent_provisioning_team"]

    main --> agent
    main --> store
    main --> runner
    main --> builder
    main --> roster
    main --> envprov
    main --> infra
    main --> models
    main --> test_store
    main --> pg
    main --> shared_obs
    main --> shared_pg

    agent --> llm
    agent --> models
    store --> models
    store --> shared_pg
    runner --> builder
    runner --> test_store
    runner --> models
    builder --> llm
    builder --> models
    roster --> models
    envprov --> store
    envprov --> apt
    infra --> jsc
    test_store --> shared_pg
    tmp --> main
    tmp --> shared_tmp

    classDef orchestrator fill:#fff4e5,stroke:#e8710a
    classDef external fill:#f1f3f4,stroke:#5f6368,stroke-dasharray: 3 3
    class main,agent,store,runner,builder,roster,envprov,infra,models,test_store,pg,tmp orchestrator
    class llm,shared_pg,shared_tmp,shared_obs,jsc,apt external
```

### Key files (source of truth for every other diagram)

| File | Lines | Purpose |
|---|---|---|
| [`../api/main.py`](../api/main.py) | ~933 | FastAPI app; all 27 endpoints; orchestrator entry; retroactive `provision_team` on startup (`api/main.py:108-113`) |
| [`../models.py`](../models.py) | ~460 | Pydantic enums + models: `TriggerType`, `StepType`, `ProcessStatus`, `TeamMode`, `MessageRating`, `PipelineRunStatus`, `AgenticTeam`, `AgenticTeamAgent`, `ProcessDefinition`, `ProcessStep`, `RosterValidationResult`, `ConversationStateResponse`, `TestPipelineRun`, … |
| [`../assistant/store.py`](../assistant/store.py) | ~440 | Shared SQLite store; conversation + team + process + roster + agent-env provisions |
| [`../assistant/agent.py`](../assistant/agent.py) | ~364 | `ProcessDesignerAgent` — system prompt, LLM call, JSON block parser |
| [`../runtime/pipeline_runner.py`](../runtime/pipeline_runner.py) | ~307 | Background-thread DAG walker; `WAIT`-step handling via `threading.Event` (`runtime/pipeline_runner.py:38-71`) |
| [`../infrastructure.py`](../infrastructure.py) | ~241 | Per-team `assets/` + `runs/` + `team.db`; `TeamFormStore` in WAL mode (`infrastructure.py:30-74`) |
| [`../roster_validation.py`](../roster_validation.py) | 182 | `validate_roster` → `RosterValidationResult`; gap categories in `models.py:295-316` |
| [`../runtime/agent_builder.py`](../runtime/agent_builder.py) | ~160 | Roster entry → `strands.Agent`; starter prompt generator |
| [`../agent_env_provisioning.py`](../agent_env_provisioning.py) | 134 | `make_provisioning_agent_id`, `schedule_provision_step_agents`, `_spawn_provision_thread` |
| [`../postgres/__init__.py`](../postgres/__init__.py) | ~130 | `AGENTIC_POSTGRES_SCHEMA` — 10 JSONB-backed tables |
| [`../temporal/__init__.py`](../temporal/__init__.py) | ~45 | `run_pipeline_activity`, `AgenticTeamProvisioningWorkflow`, `agentic_team_provisioning-queue` |
| [`../testing/store.py`](../testing/store.py) | ~332 | Test-mode persistence (sessions, messages, pipeline runs) |

## 2. Persistence — ER diagram

Both backends share the same logical schema. The shared SQLite instance at `$AGENT_CACHE/agentic_team_provisioning.db` is authoritative when `POSTGRES_HOST` is unset; otherwise Postgres (JSONB columns) registered via `shared_postgres.register_team_schemas(AGENTIC_POSTGRES_SCHEMA)` in the FastAPI lifespan (`api/main.py:77-92`) takes over.

```mermaid
erDiagram
    teams ||--o{ processes : "has"
    teams ||--o{ team_agents : "roster"
    teams ||--o{ conversations : "has"
    teams ||--o{ agent_env_provisions : "per-step envs"
    teams ||--o{ test_chat_sessions : "testing mode"
    teams ||--o{ test_pipeline_runs : "testing mode"
    conversations ||--o{ conv_messages : "history"
    test_chat_sessions ||--o{ test_chat_messages : "history"
    test_pipeline_runs ||--|| processes : "runs"
    teams ||--o{ form_data : "per-team db"

    teams {
        text team_id PK
        text name
        text description
        text mode
        text created_at
        text updated_at
    }
    processes {
        text process_id PK
        text team_id FK
        json  definition
        text status
        text created_at
        text updated_at
    }
    team_agents {
        text team_id FK
        text agent_name
        json  profile
    }
    conversations {
        text conversation_id PK
        text team_id FK
        text process_id
        text created_at
        text updated_at
    }
    conv_messages {
        text message_id PK
        text conversation_id FK
        text role
        text content
        text timestamp
    }
    agent_env_provisions {
        text team_id FK
        text stable_key PK
        text process_id
        text step_id
        text agent_name
        text provisioning_agent_id
        text status
        text error_message
        text created_at
        text updated_at
    }
    test_chat_sessions {
        text session_id PK
        text team_id FK
        text agent_name
        text session_name
        text created_at
        text updated_at
    }
    test_chat_messages {
        text message_id PK
        text session_id FK
        text role
        text content
        text rating
        text created_at
    }
    test_pipeline_runs {
        text run_id PK
        text team_id FK
        text process_id
        text status
        text current_step_id
        json  step_results
        text human_prompt
        text error
        text started_at
        text finished_at
    }
    form_data {
        text record_id PK
        text form_key
        json  data_json
        text created_at
        text updated_at
    }
```

### Backing resources

| Resource | Backing | Path / config |
|---|---|---|
| Shared team / process / conversation store | SQLite (default) or Postgres | `$AGENT_CACHE/agentic_team_provisioning.db` / `AGENTIC_POSTGRES_SCHEMA` |
| Per-team `File System` (matches legacy PNG) | Filesystem | `$AGENT_CACHE/provisioned_teams/{team_id}/assets/` (`infrastructure.py:1-9`) |
| Per-team job runs | Filesystem | `$AGENT_CACHE/provisioned_teams/{team_id}/runs/` |
| Per-team `Database` (matches legacy PNG) | SQLite WAL mode | `$AGENT_CACHE/provisioned_teams/{team_id}/team.db` (`infrastructure.py:59-74`) |
| Job lifecycle tracking | `JobServiceClient(team="provisioned_{team_id}")` | see `infrastructure.py` |
| Testing-mode store | SQLite or Postgres | `testing/store.py` |

## 3. Pydantic model catalogue

Enums (`models.py:15-64`):

| Enum | Values |
|---|---|
| `TriggerType` | `MESSAGE`, `EVENT`, `SCHEDULE`, `MANUAL` |
| `StepType` | `ACTION`, `DECISION`, `PARALLEL_SPLIT`, `PARALLEL_JOIN`, `WAIT`, `SUBPROCESS` |
| `ProcessStatus` | `DRAFT`, `COMPLETE`, `ARCHIVED` |
| `TeamMode` | `DEVELOPMENT`, `TESTING` |
| `MessageRating` | `THUMBS_UP`, `THUMBS_DOWN` |
| `PipelineRunStatus` | `RUNNING`, `WAITING_FOR_INPUT`, `COMPLETED`, `FAILED`, `CANCELLED` |

Domain models (`models.py`):

| Model | Lines | Purpose |
|---|---|---|
| `AgenticTeam` | 161-173 | Top-level team: roster + processes + mode |
| `AgenticTeamAgent` | 128-153 | Roster entry: `agent_name`, `role`, `skills`, `capabilities`, `tools`, `expertise` |
| `ProcessDefinition` | 111-120 | Process: `trigger`, `steps`, `output`, `status` |
| `ProcessStep` | 79-94 | Step: `step_type`, `agents`, `next_steps`, `condition` |
| `ProcessStepAgent` | 72-77 | Agent assignment to a specific step |
| `ProcessTrigger` | 97-101 | Trigger metadata |
| `ProcessOutput` | 104-108 | Deliverable metadata |
| `RosterValidationResult` | 309-316 | `is_fully_staffed`, `gaps`, `summary`, counts |
| `RosterGap` | 295-306 | Category, detail, process/step/agent refs |
| `ConversationStateResponse` | 226-231 | Chat history + current process + suggested questions |
| `RecommendedAgent` / `RecommendAgentsResponse` | 319-338 | Step → roster-agent matching |
| `AgentEnvProvisionSummary` | 341-355 | Provisioning status row for the API |
| `TestChatSession` / `TestChatMessage` / `TestChatSessionDetail` | 363-390 | Testing-mode chat entities |
| `AgentQualityScore` | 393-400 | Aggregated thumbs-up/down score per agent |
| `PipelineStepResult` | 403-411 | Per-step output inside a pipeline run |
| `TestPipelineRun` | 414-427 | End-to-end pipeline test record |

## 4. Runtime-mode decision tree

```mermaid
flowchart TD
    Start["Module import / request"]
    Start --> Q1{"POSTGRES_HOST set?"}
    Q1 -->|yes| PG["Use Postgres via<br/>shared_postgres<br/>(JSONB schema)"]
    Q1 -->|no| SQLite["Use local SQLite<br/>$AGENT_CACHE/agentic_team_provisioning.db"]

    Start --> Q2{"TEMPORAL_ADDRESS set<br/>and is_temporal_enabled()?"}
    Q2 -->|yes| T["start_team_worker('agentic_team_provisioning', …)<br/>task_queue='agentic_team_provisioning-queue'<br/>workflow=AgenticTeamProvisioningWorkflow"]
    Q2 -->|no| Thread["Daemon threads for PipelineRunner<br/>and AgentEnvProvisioner"]

    Start --> Q3{"AGENTIC_TEAM_AGENT_PROVISIONING_ENABLED<br/>!= false?"}
    Q3 -->|"enabled (default)"| Bridge["schedule_provision_step_agents<br/>→ spawn provisioning threads<br/>→ agent_provisioning_team"]
    Q3 -->|disabled| Skip["No-op: log debug,<br/>no sandboxed envs created"]

    classDef external fill:#f1f3f4,stroke:#5f6368,stroke-dasharray: 3 3
    class PG,T,Bridge external
```

### Relevant environment variables

| Variable | Default | Effect |
|---|---|---|
| `POSTGRES_HOST` (+ `_PORT`/`_USER`/`_PASSWORD`/`_DB`) | unset | Enables Postgres-backed stores via `shared_postgres.register_team_schemas` |
| `TEMPORAL_ADDRESS` / `TEMPORAL_NAMESPACE` / `TEMPORAL_TASK_QUEUE` | unset | Enables Temporal worker bootstrap in `temporal/__init__.py:38-44` |
| `AGENTIC_TEAM_AGENT_PROVISIONING_ENABLED` | `true` | Toggles the Agent Provisioning bridge (`agent_env_provisioning.py:25-29`) |
| `AGENTIC_TEAM_AGENT_PROVISIONING_MANIFEST` | `minimal.yaml` | Manifest passed to `ProvisioningOrchestrator.run_workflow` (`agent_env_provisioning.py:30`) |
| `AGENT_CACHE` | `~/.agent_cache` | Root for `$AGENT_CACHE/provisioned_teams/{team_id}/` |

## 5. Integration contract — Agent Provisioning bridge

Each `(team_id, process_id, step_id, agent_name)` tuple maps to a stable, sanitized `agent_id` via `make_provisioning_agent_id` (`agent_env_provisioning.py:38-50`):

```text
at-{team_id[:12]}-{process_id[:10]}-{slug(step_id,28)}-{slug(agent_name,36)}
```

`schedule_provision_step_agents` (`agent_env_provisioning.py:53-85`) iterates every step agent in a `ProcessDefinition`, calls `store.try_begin_agent_env_provision` to deduplicate, and — if the row is new — dispatches `_spawn_provision_thread`. The background thread calls:

```python
orch = ProvisioningOrchestrator()
result = orch.run_workflow(
    agent_id=provisioning_agent_id,
    manifest_path=_MANIFEST,            # default "minimal.yaml"
    access_tier=AccessTier.STANDARD,
    job_updater=None,
)
```

On completion it calls `store.mark_agent_env_provision_finished(..., success=..., error_message=...)`, updating the `agent_env_provisions` row consumed by `GET /teams/{team_id}/agent-environments` (`api/main.py:464-471`).
