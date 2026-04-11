# Flow Charts

> These flows animate the static structures in [`../designs/Agentic-team-architecture.png`](../designs/Agentic-team-architecture.png) and [`../designs/AgenticTeamApiInteractionsArchitecture.png`](../designs/AgenticTeamApiInteractionsArchitecture.png). Each sequence diagram's participant list uses the exact labels from those PNGs (`Actor`, `UI`, `API Layer`, `Orchestrator Agent`, `Agents pool`, `Processes pool`, `File System`, `Database`, `Job Tracking`, `Question Tracking`) so the reader can map every lifeline back to a box in the PNG.

## 1. Sequence — Conversational team design (animates UC1)

Participants correspond to: `Actor` (PNG #2 stick figure), `UI` (PNG #2 monitor), `API Layer · User Requests` (PNG #2 box), `Orchestrator Agent` (PNG #1 middle box), `LLM Service` (external, dashed in our architecture diagram), `Agents pool` (PNG #1 bottom-left), `Processes pool` (PNG #1 bottom-right), `Database` (PNG #2 cylinder).

```mermaid
sequenceDiagram
    autonumber
    participant EndUser as Actor
    participant UI
    participant API as "API Layer · User Requests"
    participant Orch as "Orchestrator Agent"
    participant LLM as "LLM Service"
    participant Agents as "Agents pool"
    participant Procs as "Processes pool"
    participant DB as "Database"

    EndUser->>UI: Type - design a customer onboarding team
    UI->>API: POST /conversations/{id}/messages
    API->>Orch: send_message(conversation_id, body)
    Orch->>DB: get_messages(conversation_id)
    DB-->>Orch: history[]
    Orch->>DB: get_conversation_process_id + get_process
    DB-->>Orch: current_process (or None)
    Orch->>DB: append_message(role=user)
    Orch->>LLM: ProcessDesignerAgent.respond(history, current_process, user_message, current_agents)
    LLM-->>Orch: reply + agents_json + process_json + suggestions
    Orch->>DB: append_message(role=assistant)
    Orch->>Agents: _save_agents_from_llm → store.save_team_agents
    Orch->>Procs: store.save_process (if process_json)
    Orch->>Orch: _after_process_saved → schedule_provision_step_agents
    Orch-->>API: ConversationStateResponse
    API-->>UI: messages[], current_process, suggested_questions
    UI-->>EndUser: chat reply + refreshed roster + process diagram
```

Source: [`api/main.py:389-424`](../api/main.py), [`assistant/agent.py`](../assistant/agent.py), [`assistant/store.py`](../assistant/store.py), [`agent_env_provisioning.py:53-85`](../agent_env_provisioning.py).

## 2. Sequence — Agent environment provisioning bridge (animates UC4)

```mermaid
sequenceDiagram
    autonumber
    participant Orch as "Orchestrator Agent"
    participant Store as "Database (agent_env_provisions)"
    participant Thread as "Daemon thread<br/>(prov-{agent_id[:24]})"
    participant APT as "Agent Provisioning Team"

    Note over Orch: After every process save<br/>(_after_process_saved)
    Orch->>Orch: schedule_provision_step_agents(team_id, process, store)
    loop for each step.agents
        Orch->>Orch: stable_key = process_id:step_id:agent_name
        Orch->>Orch: prov_id = make_provisioning_agent_id(...)
        Orch->>Store: try_begin_agent_env_provision(...)
        Store-->>Orch: should_run (bool)
        alt should_run
            Orch->>Thread: _spawn_provision_thread(...)
            Thread->>APT: ProvisioningOrchestrator.run_workflow(prov_id, manifest, STANDARD)
            APT-->>Thread: result.success + optional error
            alt success
                Thread->>Store: mark_agent_env_provision_finished(success=True)
            else failure
                Thread->>Store: mark_agent_env_provision_finished(success=False, error_message=...)
            end
        else already running
            Note over Orch: deduplicated — no new thread
        end
    end
```

Source: [`agent_env_provisioning.py:53-129`](../agent_env_provisioning.py), [`api/main.py:464-471`](../api/main.py) (status read path).

## 3. Sequence — Asset upload (animates the `Assets → File System` edge from PNG #2)

```mermaid
sequenceDiagram
    autonumber
    participant EndUser as Actor
    participant UI
    participant API as "API Layer · Assets"
    participant Orch as "Orchestrator Agent"
    participant FS as "File System"

    EndUser->>UI: Select file, click upload
    UI->>API: POST /teams/{team_id}/assets (multipart)
    API->>Orch: upload_team_asset(team_id, file)
    Orch->>Orch: _get_infra_or_404(team_id)
    Orch->>Orch: _safe_asset_name(file.filename)
    Orch->>FS: dest.write_bytes(await file.read())
    FS-->>Orch: stat (size, mtime)
    Orch-->>API: AssetInfo(name, size_bytes, modified_at)
    API-->>UI: 200 AssetInfo
    UI-->>EndUser: File appears in asset list
```

Note: `FS` is rooted at `$AGENT_CACHE/provisioned_teams/{team_id}/assets/` (see `infrastructure.py`).

Source: [`api/main.py:559-612`](../api/main.py), [`infrastructure.py`](../infrastructure.py).

## 4. Sequence — Form record write (animates the `Form Information → Database` edge from PNG #2)

```mermaid
sequenceDiagram
    autonumber
    participant EndUser as Actor
    participant UI
    participant API as "API Layer · Form Information"
    participant Orch as "Orchestrator Agent"
    participant FormStore as "TeamFormStore"
    participant DB as "Database — team.db"

    EndUser->>UI: Submit form
    UI->>API: POST /teams/{team_id}/forms/{form_key}
    API->>Orch: create_team_form_record(team_id, form_key, req.data)
    Orch->>Orch: _get_infra_or_404(team_id)
    Orch->>FormStore: form_store.create_record(form_key, data)
    FormStore->>DB: INSERT INTO form_data (record_id, form_key, data_json, created_at, updated_at)
    DB-->>FormStore: OK
    FormStore-->>Orch: record dict
    Orch-->>API: FormRecord
    API-->>UI: 201 FormRecord
```

Note: `FormStore` is `TeamFormStore` from `infrastructure.py`; `DB` is the per-team SQLite at `$AGENT_CACHE/provisioned_teams/{team_id}/team.db` in WAL mode.

Source: [`api/main.py:635-640`](../api/main.py), [`infrastructure.py:30-80`](../infrastructure.py).

## 5. Flowchart — Roster validation (animates UC2)

```mermaid
flowchart TD
    Start["validate_roster(team)"]
    Start --> BuildMap["roster_map = {agent.agent_name: agent}<br/>used_agent_names = ∅"]
    BuildMap --> LoopP{"for proc in team.processes"}
    LoopP -->|next proc| CheckSteps{"proc.steps empty?"}
    CheckSteps -->|yes| GapUnstaffed1["gap: unstaffed_step (no steps)"]
    CheckSteps -->|no| LoopS{"for step in proc.steps"}
    LoopS -->|next step| StepAgents{"step.agents empty?"}
    StepAgents -->|yes| GapUnstaffed2["gap: unstaffed_step"]
    StepAgents -->|no| LoopSA{"for sa in step.agents"}
    LoopSA --> AddUsed["used_agent_names.add(sa.agent_name)"]
    AddUsed --> InRoster{"sa.agent_name in roster_map?"}
    InRoster -->|no| GapUnrostered["gap: unrostered_agent"]
    InRoster -->|yes| LoopSA
    GapUnrostered --> LoopSA
    GapUnstaffed2 --> LoopS
    LoopS -->|done| LoopP
    LoopP -->|done| UnusedCheck["_check_unused_agents<br/>(only if team.processes)"]
    UnusedCheck --> LoopUnused{"for name in sorted(roster_map)"}
    LoopUnused -->|name not in used| GapUnused["gap: unused_agent"]
    LoopUnused -->|done| DepthCheck["_check_roster_depth(team.agents)"]
    DepthCheck --> LoopDepth{"for a in agents"}
    LoopDepth -->|"missing == 4 (skills, capabilities, tools, expertise)"| GapIncomplete["gap: incomplete_profile"]
    LoopDepth -->|"missing >= 3"| GapSparse["gap: sparse_profile"]
    LoopDepth -->|done| Aggregate["is_fully_staffed = len(gaps) == 0<br/>summary = _build_summary(...)"]
    Aggregate --> Result["RosterValidationResult(is_fully_staffed, agent_count, process_count, gaps, summary)"]

    classDef good fill:#e6f4ea,stroke:#188038
    classDef bad fill:#fce8e6,stroke:#c5221f
    class Result good
    class GapUnstaffed1,GapUnstaffed2,GapUnrostered,GapUnused,GapIncomplete,GapSparse bad
```

Source: [`roster_validation.py:23-151`](../roster_validation.py).

## 6. Flowchart — Pipeline test run (animates UC9)

> **Important — this flowchart documents what the code actually does, not what the `StepType` enum advertises.** `runtime/pipeline_runner.py` pre-computes a single topological order of `process.steps` (`_topological_sort`, line 254-293) and walks it linearly. Only two step types have dedicated handlers: `WAIT` (blocks on a `threading.Event` until `POST .../input` or `cancel`) and `DECISION` (runs the agent against `step.condition`, records the decision string as the step's output, and advances to the next topologically-sorted step). **Every other `StepType` — `ACTION`, `PARALLEL_SPLIT`, `PARALLEL_JOIN`, `SUBPROCESS` — falls through to `_handle_action_step` and is treated as a plain action.** The runner does **not** fan out parallel splits, synchronize joins, branch on decision results, or recurse into subprocesses. If you design a test pipeline that depends on those semantics, it will silently run as a linear sequence of actions instead. See the "Unimplemented semantics" note below the diagram.

```mermaid
flowchart TD
    Start["POST /teams/{id}/test-pipeline/runs"]
    Start --> Locate["Locate ProcessDefinition in team.processes<br/>(api/main.py:858-877)"]
    Locate --> Create["_test_store.create_pipeline_run<br/>(status=RUNNING, initial_input)"]
    Create --> Spawn["PipelineRunner.start_run<br/>→ daemon thread: pipeline-{run_id[:16]}"]
    Spawn --> Execute["_execute(run_id, team_agents, process, resume_event)"]
    Execute --> Sort["step_order = _topological_sort(process.steps)<br/>prev_output = run.initial_input or ''"]
    Sort --> Walk{"for step in step_order"}

    Walk --> CancelCheck{"status == 'cancelled'?"}
    CancelCheck -->|yes| End["End"]
    CancelCheck -->|no| SetCurrent["update_pipeline_run<br/>(current_step_id, status=running)"]

    SetCurrent --> StepKind{"step.step_type"}
    StepKind -->|"WAIT"| Pause["_handle_wait_step:<br/>status=WAITING_FOR_INPUT<br/>human_prompt=step.description<br/>resume_event.wait()"]
    StepKind -->|"DECISION"| DecisionHandler["_handle_decision_step:<br/>build_agent → call_agent(condition_prompt)<br/>record decision as prev_output<br/><b>does not alter traversal</b>"]
    StepKind -->|"ACTION / PARALLEL_SPLIT /<br/>PARALLEL_JOIN / SUBPROCESS<br/>(everything else)"| ActionHandler["_handle_action_step:<br/>agent_name = step.agents[0]<br/>build_agent → call_agent(step_input)<br/>append PipelineStepResult"]

    Pause --> ResumeSignal{"resume_event.set() by..."}
    ResumeSignal -->|"submit_human_input"| Resumed["human_inputs[run_id]=input<br/>status=running<br/>prev_output = input"]
    ResumeSignal -->|"cancel_run"| Cancelled["status=CANCELLED<br/>return"]

    Resumed --> Walk
    DecisionHandler --> Walk
    ActionHandler --> Walk

    Walk -->|done| Finish["status=COMPLETED<br/>step_results=[...], finished_at=now"]
    Walk -.->|exception| Fail["status=FAILED<br/>error=str(exc)"]
    Cancelled --> End
    Finish --> End
    Fail --> End

    classDef impl fill:#e6f4ea,stroke:#188038
    classDef gap  fill:#fef7e0,stroke:#f9ab00
    class Pause,DecisionHandler,ActionHandler impl
    class StepKind gap
```

### Unimplemented semantics (design intent vs. runtime reality)

| `StepType` | Enum (`models.py:24-32`) | `ProcessDesignerAgent` prompt (`assistant/agent.py:73`) | `PipelineRunner` behaviour |
|---|---|---|---|
| `ACTION`        | ✔ defined | advertised | `_handle_action_step` — runs assigned agent on prev output |
| `DECISION`      | ✔ defined | advertised as branching | `_handle_decision_step` runs the agent, records the decision string, but **the loop ignores the return value and advances to the next topologically-sorted step**. Decision results are visible in `step_results` for human inspection only. |
| `WAIT`          | ✔ defined | advertised | `_handle_wait_step` — pauses on `threading.Event`, resumes via `submit_human_input` |
| `PARALLEL_SPLIT`| ✔ defined | advertised as fan-out | falls through to `_handle_action_step`; **no fan-out** |
| `PARALLEL_JOIN` | ✔ defined | advertised as barrier | falls through to `_handle_action_step`; **no synchronization** |
| `SUBPROCESS`    | ✔ defined | advertised as nested process | falls through to `_handle_action_step`; **no recursion into a sub-DAG** |

If any of these semantics are needed, they must be added to `PipelineRunner._execute`. The author of a new test should treat the current runner as **"walk the DAG topologically and run one agent per step; pause on WAIT; record decisions as strings."**

Source: [`runtime/pipeline_runner.py:73-293`](../runtime/pipeline_runner.py) (especially `_execute` at line 73, `_handle_action_step` at line 132, `_handle_wait_step` at line 171, `_handle_decision_step` at line 209, `_topological_sort` at line 254), [`api/main.py:858-933`](../api/main.py), `StepType` in [`models.py:24-32`](../models.py), `PipelineRunStatus` in [`models.py:57-64`](../models.py).

## 7. State — `PipelineRunStatus`

```mermaid
stateDiagram-v2
    [*] --> RUNNING : start_run
    RUNNING --> WAITING_FOR_INPUT : WAIT step reached
    WAITING_FOR_INPUT --> RUNNING : submit_human_input
    RUNNING --> COMPLETED : DAG exhausted
    RUNNING --> FAILED : exception
    RUNNING --> CANCELLED : cancel_run
    WAITING_FOR_INPUT --> CANCELLED : cancel_run
    COMPLETED --> [*]
    FAILED --> [*]
    CANCELLED --> [*]
```

Source: [`models.py:57-64`](../models.py), [`runtime/pipeline_runner.py:58-71`](../runtime/pipeline_runner.py).

## 8. State — `TeamMode` (advisory metadata only)

> `TeamMode` is **metadata**, not a server-side gate. `PUT /teams/{id}/mode` (`api/main.py:670-677`) writes the mode via `_test_store.set_team_mode`, but **none** of the test-chat or test-pipeline handlers read it — `create_test_chat_session` (`:694`), `send_test_chat_message` (`:760`), and `start_pipeline_run` (`:858`) only check team/session/agent existence. A team in `DEVELOPMENT` mode can still accept test-chat sessions and pipeline runs; a team in `TESTING` mode still accepts design-mode conversation endpoints. Mode is a **UI hint**, not a security boundary.

```mermaid
stateDiagram-v2
    [*] --> DEVELOPMENT : POST /teams
    DEVELOPMENT --> TESTING : PUT /teams/{id}/mode (mode=testing)
    TESTING --> DEVELOPMENT : PUT /teams/{id}/mode (mode=development)

    note right of DEVELOPMENT
      Advisory label.
      Every endpoint is reachable in both modes —
      design-mode chat, test chat, and pipeline runs
      all work regardless of the current value.
    end note
```

Source: [`models.py:43-47`](../models.py), [`api/main.py:670-677`](../api/main.py); absence of mode checks in `api/main.py:694, 760, 858`.

---

### PNG-box → participant cross-reference

| PNG box (legacy) | Appears in these diagrams as |
|---|---|
| PNG #2 `Actor` (stick figure) | §1, §3, §4 lifeline `Actor` |
| PNG #2 `UI` (monitor) | §1, §3, §4 lifeline `UI` |
| PNG #2 `API Layer` | §1 `API Layer · User Requests`, §3 `API Layer · Assets`, §4 `API Layer · Form Information` |
| PNG #2 `Agentic Team` = PNG #1 `Orchestrator Agent` | §1-§4 lifeline `Orchestrator Agent` |
| PNG #2 `File System` | §3 `File System` |
| PNG #2 `Database` | §1, §4 `Database` |
| PNG #1 `Agents` pool | §1 `Agents pool` |
| PNG #1 `Processes` pool | §1 `Processes pool` |
| PNG #1 `Job Tracking` / `Question Tracking` | Referenced in UC5 (use_cases.md) — no dedicated sequence here since they're simple `JobServiceClient` wrappers |
