# Startup Advisor — Flow Charts

Sequence diagrams and state charts for every runtime path in the
startup advisor team. Each diagram is citation-linked to the source
file + line range that implements the flow.

## Flow A — Get or create conversation

Entry point: `GET /api/startup-advisor/conversation` →
`get_or_create_conversation` at `api/main.py:162-184`.

```mermaid
sequenceDiagram
    autonumber
    actor Founder
    participant API as FastAPI handler<br/>(api/main.py:162)
    participant Store as StartupAdvisorConversationStore
    participant PG as Postgres

    Founder->>API: GET /conversation
    API->>Store: get_or_create_singleton()
    Store->>PG: SELECT conversation_id FROM<br/>startup_advisor_conversations<br/>ORDER BY created_at ASC LIMIT 1
    alt Row exists
        PG-->>Store: existing conversation_id
    else No rows
        Store->>PG: INSERT conversation (uuid, {}, now, now)
        PG-->>Store: OK
    end
    Store-->>API: cid

    API->>Store: get(cid)
    Store->>PG: SELECT context_json
    Store->>PG: SELECT messages ORDER BY id
    PG-->>Store: (messages, context)
    Store-->>API: (messages, context)

    API->>Store: get_artifacts(cid)
    Store->>PG: SELECT * FROM conv_artifacts
    PG-->>Store: artifacts
    Store-->>API: artifacts

    alt Transcript is empty
        API->>Store: append_message(cid, "assistant", WELCOME)
        Store->>PG: INSERT message + UPDATE updated_at
        PG-->>Store: OK
        API->>Store: get(cid)
        Store-->>API: (messages w/ welcome, context)
    end

    API-->>Founder: ConversationStateResponse
```

The welcome-insertion branch at `api/main.py:175-180` only runs on
the very first call after the row has just been created.
`_DEFAULT_SUGGESTED` (`api/main.py:101-105`) is returned as the
`suggested_questions` array whenever the transcript has 0 or 1
entries.

---

## Flow B — Probing dialogue turn (no artifact)

Entry point: `POST /api/startup-advisor/conversation/messages` →
`send_message` at `api/main.py:187-242`. This is the hot path for
UC-2.

```mermaid
sequenceDiagram
    autonumber
    actor Founder
    participant API as FastAPI handler<br/>(api/main.py:187)
    participant Store as StartupAdvisorConversationStore
    participant Agent as StartupAdvisorAgent<br/>(assistant/agent.py:105)
    participant LLM as llm_service client
    participant PG as Postgres

    Founder->>API: POST /conversation/messages<br/>{"message": "..."}
    API->>Store: get_or_create_singleton()
    Store->>PG: SELECT oldest / INSERT if none
    PG-->>Store: cid
    Store-->>API: cid

    API->>Store: get(cid)
    Store->>PG: SELECT context_json + messages
    PG-->>Store: (messages, context)
    Store-->>API: state

    Note over API: If transcript empty, append WELCOME first<br/>(api/main.py:201-206)

    API->>Store: append_message(cid, "user", msg)
    Store->>PG: INSERT msg + UPDATE updated_at
    PG-->>Store: OK

    API->>Agent: respond(history, context, msg)
    Note over Agent: Build USER_TURN_TEMPLATE<br/>assistant/agent.py:74-84
    Agent->>LLM: complete(prompt, system_prompt,<br/>temperature=0.5, think=True)
    LLM-->>Agent: raw JSON string
    Note over Agent: _parse_response strips fences +<br/>json.loads (agent.py:87-102)
    Agent-->>API: (reply, ctx_update, suggested, artifact=None)

    opt ctx_update non-empty
        API->>API: _merge_context(existing, update)<br/>non-empty overwrite<br/>(api/main.py:148-154)
        API->>Store: update_context(cid, merged)
        Store->>PG: UPDATE context_json
        PG-->>Store: OK
    end

    API->>Store: append_message(cid, "assistant", reply)
    Store->>PG: INSERT msg + UPDATE updated_at
    PG-->>Store: OK

    API->>Store: get(cid)
    Store->>PG: SELECT context_json + messages
    PG-->>Store: refreshed state
    API->>Store: get_artifacts(cid)
    Store->>PG: SELECT artifacts
    PG-->>Store: artifacts

    API-->>Founder: ConversationStateResponse<br/>(transcript, context, artifacts,<br/>suggested_questions)
```

Key code references:

- User-turn persistence before the LLM call (`api/main.py:209`) —
  guarantees the message is stored even if the LLM call later
  raises (see Flow D).
- Transcript assembly (`api/main.py:212-213`) includes the new
  user message so the LLM sees it as part of history.
- Reload + artifact fetch after the LLM response
  (`api/main.py:236-240`) — the response always reflects the
  latest persisted state.

---

## Flow C — Turn that yields an artifact

Same handler (`api/main.py:187-242`), highlighting the
`add_artifact` branch at `api/main.py:229-233`.

```mermaid
sequenceDiagram
    autonumber
    actor Founder
    participant API as FastAPI handler
    participant Agent as StartupAdvisorAgent
    participant LLM as llm_service client
    participant Store as ConversationStore
    participant PG as Postgres

    Founder->>API: POST /conversation/messages<br/>"We're ready to take this to market"
    Note over API: ... same prelude as Flow B ...
    API->>Agent: respond(history, context, msg)
    Agent->>LLM: complete(prompt, think=True)
    LLM-->>Agent: {"reply": "...",<br/>"context_update": {...},<br/>"suggested_questions": [],<br/>"artifact": {<br/>  "type": "gtm_strategy",<br/>  "title": "GTM: Fintech SaaS",<br/>  "content": {...}<br/>}}
    Agent-->>API: (reply, ctx_update, [], artifact)

    API->>Store: update_context(cid, merged)
    Store->>PG: UPDATE context_json

    API->>Store: append_message(cid, "assistant", reply)
    Store->>PG: INSERT msg

    Note over API: artifact is a dict → enter branch<br/>(api/main.py:229-233)
    API->>API: artifact_type = artifact.get("type","advice")<br/>title = artifact.get("title","Untitled")<br/>content = artifact.get("content", artifact)
    API->>Store: add_artifact(cid, type, title, content)
    Store->>PG: INSERT INTO conv_artifacts RETURNING id
    PG-->>Store: new_id
    Store-->>API: new_id

    API->>Store: get(cid) + get_artifacts(cid)
    Store-->>API: state with artifact included
    API-->>Founder: ConversationStateResponse<br/>artifacts=[...new artifact]
```

Note that the artifact payload is stored as `JSONB` and is
free-form: the store accepts any dict shape
(`store.py:167-184`) and the API returns it verbatim through
`ArtifactResponse.payload`.

---

## Flow D — LLM failure fallback

Triggered inside `StartupAdvisorAgent.respond` at
`assistant/agent.py:146-164`. This is UC-5.

```mermaid
sequenceDiagram
    autonumber
    actor Founder
    participant API as FastAPI handler
    participant Store as ConversationStore
    participant Agent as StartupAdvisorAgent
    participant LLM as llm_service client
    participant PG as Postgres
    participant Logs as Service logs

    Founder->>API: POST /conversation/messages<br/>{"message": "..."}
    API->>Store: append_message(cid, "user", msg)
    Store->>PG: INSERT user msg
    Note over API: User turn is persisted<br/>BEFORE the LLM call<br/>(api/main.py:209)

    API->>Agent: respond(history, context, msg)
    Agent->>LLM: complete(prompt, think=True)
    LLM--xAgent: Exception (timeout /<br/>network / 5xx)
    Agent->>Logs: logger.exception(...)
    Note over Agent: Return hard-coded fallback<br/>(agent.py:153-164)
    Agent-->>API: (canned reply, {}, [3 canned Qs], None)

    Note over API: ctx_update is empty →<br/>no update_context call<br/>(api/main.py:221-223)

    API->>Store: append_message(cid, "assistant", canned_reply)
    Store->>PG: INSERT assistant msg

    API->>Store: get(cid) + get_artifacts(cid)
    Store-->>API: refreshed state
    API-->>Founder: ConversationStateResponse<br/>(looks like a normal reply)
```

The founder sees a normal reply with three canned probing
questions. The failure is visible only in service logs via
`logger.exception("LLM call failed for startup advisor")`.

---

## Flow E — Context accumulation state chart

Shows how the JSON `context` field on
`startup_advisor_conversations` evolves turn by turn. The merge
strategy is at `api/main.py:148-154`: non-empty values overwrite,
`None` / `""` are ignored.

```mermaid
stateDiagram-v2
    [*] --> Empty : row created\nvia create()

    Empty --> PartiallyKnown : turn 1\nLLM emits\n{"industry": "B2B SaaS"}
    PartiallyKnown --> StageKnown : turn 2\n{"stage": "idea_validation",\n"target_audience": "..."}
    StageKnown --> TeamKnown : turn 3\n{"team_size": 3,\n"runway_months": 9}
    TeamKnown --> GoalKnown : turn 4\n{"primary_challenge":\n"ICP definition"}
    GoalKnown --> ArtifactReady : turn N\nagent decides it\nhas enough context

    ArtifactReady --> GoalKnown : further probing /\nnew challenge

    note right of PartiallyKnown
      Every transition is a
      _merge_context call
      (api/main.py:148-154).
      Missing or empty
      values are dropped.
    end note

    note right of ArtifactReady
      Non-null artifact persisted
      via store.add_artifact
      (api/main.py:229-233).
      Context is preserved.
    end note
```

The context JSON is replaced, not patched, at the database level —
`update_context` writes the full merged dict in one statement
(`store.py:156-165`). The non-empty filter lives in the handler,
not the store, so tests can drive the store with full-overwrite
semantics directly.

---

## Flow F — Temporal workflow path (optional)

Only active when `is_temporal_enabled()` is true. Code lives in
`temporal/__init__.py:11-41`.

```mermaid
sequenceDiagram
    autonumber
    participant Client as External Temporal client
    participant TemporalSvc as Temporal service
    participant Worker as startup_advisor-queue worker
    participant WF as StartupAdvisorWorkflow
    participant Act as run_pipeline_activity
    participant Handler as send_message(api/main.py:187)
    participant Store as ConversationStore
    participant PG as Postgres
    participant LLM as llm_service

    Client->>TemporalSvc: start_workflow(<br/>StartupAdvisorWorkflow,<br/>request_dict)
    TemporalSvc->>Worker: dispatch task
    Worker->>WF: run(request_dict)
    WF->>TemporalSvc: execute_activity(<br/>run_pipeline_activity,<br/>timeout=30m)
    TemporalSvc->>Worker: dispatch activity
    Worker->>Act: run_pipeline_activity(request_dict)
    Note over Act: Deferred import of<br/>SendMessageRequest + send_message<br/>(temporal/__init__.py:13)
    Act->>Handler: send_message(SendMessageRequest(**request))
    Handler->>Store: ... Flow B / Flow C ...
    Store->>PG: ... same SQL ...
    Handler->>LLM: ... same complete(...) call ...
    Handler-->>Act: ConversationStateResponse
    Act->>Act: result.model_dump()
    Act-->>WF: dict
    WF-->>TemporalSvc: workflow result
    TemporalSvc-->>Client: dict
```

The Temporal path is a thin wrapper — no alternate SQL, no
alternate agent, no alternate context merge. If you need to trace a
Temporal-triggered message, the breakpoints are the same ones used
for Flow B / Flow C.

---

## Flow G — Startup & shutdown lifespan

`_lifespan` at `api/main.py:20-34` wraps the FastAPI app.

```mermaid
sequenceDiagram
    autonumber
    participant Uvicorn
    participant App as FastAPI app
    participant Lifespan as _lifespan()<br/>api/main.py:20-34
    participant Registry as shared_postgres.register_team_schemas
    participant Pool as shared_postgres pool
    participant PG as Postgres

    Uvicorn->>App: startup
    App->>Lifespan: __aenter__
    Lifespan->>Registry: register_team_schemas(<br/>STARTUP_ADVISOR_POSTGRES_SCHEMA)
    Registry->>Pool: get_conn()
    Pool->>PG: connect
    Registry->>PG: execute SCHEMA.statements<br/>(3x CREATE TABLE + 2x CREATE INDEX)
    PG-->>Registry: OK
    Registry-->>Lifespan: registered
    Lifespan-->>App: ready

    Note over App: serve requests (Flows A-F)

    Uvicorn->>App: shutdown
    App->>Lifespan: __aexit__
    Lifespan->>Pool: close_pool()
    Pool->>PG: close all connections
    PG-->>Pool: OK
    Lifespan-->>App: clean shutdown
```

Both the registration and the pool close are wrapped in
`try/except`: registration failure logs via `logger.exception` but
still yields so the app starts (`api/main.py:26-28`), and pool
close failure is logged at `warning` level only
(`api/main.py:33-34`). The team never crashes its own lifespan.

Separately, at module import time, `init_otel` is called at
`api/main.py:17` and `instrument_fastapi_app(app,
team_key="startup_advisor")` at `api/main.py:43` — these happen
before the lifespan runs and do not block on Postgres.

If Temporal is enabled, the `startup_advisor-queue` worker also
starts at import time via
`start_team_worker("startup_advisor", WORKFLOWS, ACTIVITIES,
task_queue="startup_advisor-queue")` in
`temporal/__init__.py:38-41`. This is independent of the FastAPI
lifespan.
